"""
core/memory_cortex.py
=====================
Saturn - Long-Term Episodic Semantic Memory & Habit Cortex.

Provides JARVIS-level continuity across sessions:
1. Persistent episodic & factual memory (stored in SQLite with WAL mode).
2. Lexical and semantic scoring using TF-IDF / BM25 term weighting, recency decay, and importance factors.
3. Autonomous fact harvesting from natural conversational turns (identifying preferences, identity, credentials/targets, ongoing projects).
4. System prompt memory synthesis (injecting only high-relevance memories to keep token budgets lean).
5. Habit graph tracking (correlating time-of-day/triggers with user actions).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.user_paths import get_user_data_dir

# The Saturn-aware persistence layer. Importing cortex_saturn is safe: it never
# imports this module back, so there is no cycle. memory_manager is imported
# lazily inside harvest_turn because it may itself reach for the cortex.
from memory.saturn_mode import resolve_saturn_mode
from memory import cortex_saturn as cu

logger = logging.getLogger("MemoryCortex")
logger.setLevel(logging.INFO)

# Lightweight stopword list for pure Python lexical matching (zero external deps)
STOPWORDS: Set[str] = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "aren't",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can't",
    "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't", "doing", "don't", "down", "during",
    "each", "few", "for", "from", "further", "had", "hadn't", "has", "hasn't", "have", "haven't", "having",
    "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself", "him", "himself", "his", "how",
    "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself",
    "let's", "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off", "on", "once",
    "only", "or", "other", "ought", "our", "ours", "ourselves", "out", "over", "own", "same", "shan't", "she",
    "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such", "than", "that", "that's", "the",
    "their", "theirs", "them", "themselves", "then", "there", "there's", "these", "they", "they'd", "they'll",
    "they're", "they've", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was",
    "wasn't", "we", "we'd", "we'll", "we're", "we've", "were", "weren't", "what", "what's", "when", "when's",
    "where", "where's", "which", "while", "who", "who's", "whom", "why", "why's", "with", "won't", "would",
    "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your", "yours", "yourself", "yourselves",
    "please", "saturn", "hey", "can", "could", "would", "like"
}

FACT_HARVEST_PATTERNS = [
    # Identity & Personal facts
    (r"\bmy name is ([a-zA-Z0-9_\- ]+)", "identity", 1.0),
    (r"\bcall me ([a-zA-Z0-9_\- ]+)", "identity", 0.95),
    (r"\bi am ([0-9]{1,3} years old)", "identity", 0.8),
    # Preferences
    (r"\bmy favou?rite\s+([^.,!?]+?)\s+(?:is|are)\s+([^.,!?]+)", "preference", 0.9),
    (r"\bmy favou?rite (?:is|are) ([^.,!?]+)", "preference", 0.9),
    (r"\bi (?:prefer|love|enjoy) ([^.,!?]+)", "preference", 0.85),
    (r"\bi (?:hate|dislike|don't like) ([^.,!?]+)", "preference", 0.85),
    # Directives & Standing rules
    (r"\balways remember (?:that )?([^.,!?]+)", "directive", 1.0),
    (r"\bnever (?:ever )?([^.,!?]+)", "directive", 0.95),
    (r"\bfrom now on ([^.,!?]+)", "directive", 0.95),
    # Technical credentials, IPs, devices
    (r"\bmy (?:router|server|pc|laptop|ip|device) is ([^.,!?]+)", "hardware", 0.9),
    (r"\bmy wifi (?:is|password is|ssid is) ([^.,!?]+)", "network", 0.9),
    # Projects & Focus
    (r"\bi(?:'m| am) (?:working on|building|studying|preparing for) ([^.,!?]+)", "project", 0.9),
]


class MemoryCortex:
    """Manages long-term semantic memory storage, retrieval, and habit correlation."""

    _instance: Optional[MemoryCortex] = None

    def __init__(self, db_path: Optional[Path] = None):
        self._lock = threading.Lock()
        if db_path is None:
            config_dir = get_user_data_dir() / "config"
            self.db_path = config_dir / "memory_cortex.db"
        else:
            self.db_path = Path(db_path)

        # ``unified`` mode retires the private SQLite database: the cortex
        # becomes a facade over the shared Saturn store. Any other mode keeps
        # the local database exactly as before.
        try:
            self._native = bool(resolve_saturn_mode().cortex_native)
        except Exception:
            self._native = False

        self._store: Optional[cu.CortexNativeStore] = None
        if self._native:
            # Never touch SQLite in unified mode: no directory, no file, no
            # _init_db. The store translates to the sidecar on first use.
            self._store = cu.CortexNativeStore(db_path=self.db_path)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    @classmethod
    def get_instance(cls, db_path: Optional[Path] = None) -> MemoryCortex:
        if cls._instance is None:
            cls._instance = MemoryCortex(db_path=db_path)
        return cls._instance

    def backend(self) -> str:
        """Which persistence surface this cortex is bound to right now."""
        return "saturn" if getattr(self, "_native", False) else "sqlite"

    def _get_connection(self) -> sqlite3.Connection:
        if getattr(self, "_native", False):
            raise RuntimeError("SQLite is not used in unified mode")
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    importance REAL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    access_count INTEGER DEFAULT 0
                );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_cat ON memories(category);")
                conn.execute("""
                CREATE TABLE IF NOT EXISTS habits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_event TEXT NOT NULL UNIQUE,
                    suggested_action TEXT NOT NULL,
                    frequency INTEGER DEFAULT 1,
                    last_triggered REAL NOT NULL
                );
                """)
                conn.commit()

    # =========================================================================
    # TOKENIZATION & KEYWORD EXTRACTION
    # =========================================================================

    @staticmethod
    def extract_keywords(text: str) -> List[str]:
        """Extracts cleaned unique non-stopword tokens for indexing and matching."""
        tokens = re.findall(r"[a-zA-Z0-9_\-\.]{2,}", (text or "").lower())
        meaningful = [t for t in tokens if t not in STOPWORDS and not t.isdigit()]
        return sorted(list(set(meaningful)))

    # =========================================================================
    # CORE MEMORY CRUD
    # =========================================================================

    def remember(
        self,
        content: str,
        category: str = "fact",
        importance: float = 1.0
    ) -> int:
        """Stores a new factual memory or reinforces an existing one."""
        if self._native:
            return self._store.remember(content, category=category, importance=importance)
        clean_content = content.strip()
        if not clean_content:
            return -1

        keywords = self.extract_keywords(clean_content)
        kw_str = " ".join(keywords)
        now = time.time()

        with self._lock:
            with self._get_connection() as conn:
                # Check for near-duplicate memory in same category
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, content, importance, access_count FROM memories WHERE category = ? AND content LIKE ?",
                    (category, f"%{clean_content[:30]}%")
                )
                existing = cursor.fetchone()

                if existing:
                    # Reinforce existing memory
                    mem_id = existing["id"]
                    new_importance = min(1.0, max(existing["importance"], importance) + 0.1)
                    cursor.execute(
                        "UPDATE memories SET importance = ?, last_accessed_at = ?, access_count = access_count + 1, content = ? WHERE id = ?",
                        (new_importance, now, clean_content, mem_id)
                    )
                    conn.commit()
                    logger.debug(f"Reinforced existing memory #{mem_id}: {clean_content[:50]}")
                    return mem_id
                else:
                    cursor.execute(
                        "INSERT INTO memories (category, content, keywords, importance, created_at, last_accessed_at, access_count) VALUES (?, ?, ?, ?, ?, ?, 1)",
                        (category, clean_content, kw_str, float(importance), now, now)
                    )
                    conn.commit()
                    mem_id = cursor.lastrowid
                    logger.info(f"Stored new memory #{mem_id} [{category}]: {clean_content[:60]}")
                    return mem_id

    def forget(self, memory_id: int) -> bool:
        """Removes a specific memory by ID."""
        if self._native:
            return self._store.forget(memory_id)
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
                conn.commit()
                return cursor.rowcount > 0

    def clear_all_memories(self) -> None:
        """Wipes all stored memories (useful for test resets)."""
        if self._native:
            self._store.clear_all_memories()
            return
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM memories;")
                conn.execute("DELETE FROM habits;")
                conn.commit()

    # =========================================================================
    # RECALL & SEMANTIC SCORING
    # =========================================================================

    def recall(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.12
    ) -> List[Dict[str, Any]]:
        """
        Retrieves top-K most relevant memories based on:
        1. Query keyword overlap (Jaccard / Term frequency)
        2. Inherent memory importance (0.0 - 1.0)
        3. Recency decay (half-life of ~14 days)
        4. Usage frequency reinforcement
        """
        if self._native:
            return self._store.recall(query, top_k=top_k, min_score=min_score)
        q_keywords = set(self.extract_keywords(query))
        if not q_keywords:
            return []

        now = time.time()
        scored_memories: List[Tuple[float, Dict[str, Any]]] = []

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM memories")
                rows = cursor.fetchall()

                for row in rows:
                    mem_kw = set(row["keywords"].split())
                    if not mem_kw:
                        continue

                    # Overlap ratio
                    overlap = q_keywords.intersection(mem_kw)
                    if not overlap:
                        # Also check direct substring match in content
                        content_low = row["content"].lower()
                        direct_hits = sum(1 for qk in q_keywords if qk in content_low)
                        if direct_hits == 0:
                            continue
                        overlap_score = (direct_hits / len(q_keywords)) * 0.7
                    else:
                        overlap_score = len(overlap) / (math.sqrt(len(q_keywords)) * math.sqrt(len(mem_kw)))

                    # Recency weight (exponential decay)
                    age_days = (now - row["last_accessed_at"]) / 86400.0
                    recency_weight = math.exp(-0.05 * age_days)

                    # Access count booster (logarithmic)
                    frequency_boost = 1.0 + 0.1 * math.log(row["access_count"] + 1)

                    # Composite score
                    score = overlap_score * row["importance"] * recency_weight * frequency_boost

                    if score >= min_score:
                        scored_memories.append((
                            score,
                            {
                                "id": row["id"],
                                "category": row["category"],
                                "content": row["content"],
                                "score": round(score, 3),
                                "importance": row["importance"],
                                "created_at": row["created_at"],
                                "access_count": row["access_count"],
                            }
                        ))

        # Sort descending by composite score
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        top_results = [item[1] for item in scored_memories[:top_k]]

        # Update last_accessed_at for recalled items
        if top_results:
            recalled_ids = [m["id"] for m in top_results]
            with self._lock:
                with self._get_connection() as conn:
                    conn.executemany(
                        "UPDATE memories SET last_accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                        [(now, mid) for mid in recalled_ids]
                    )
                    conn.commit()

        return top_results

    # =========================================================================
    # SYSTEM PROMPT INJECTION
    # =========================================================================

    def build_context_prompt(self, user_query: str, max_items: int = 4) -> str:
        """
        Generates a concise system prompt block with memories pertinent to the user's query.
        Returns empty string if no relevant memories exist.
        """
        if self._native:
            return self._store.build_context_prompt(user_query, max_items=max_items)
        recalled = self.recall(user_query, top_k=max_items, min_score=0.15)
        if not recalled:
            return ""

        lines = ["[RECALLED LONG-TERM MEMORY & USER CONTEXT]:"]
        for r in recalled:
            lines.append(f"- ({r['category'].capitalize()}) {r['content']}")
        lines.append("Use this background knowledge naturally when responding.")
        return "\n".join(lines)

    # =========================================================================
    # AUTONOMOUS FACT HARVESTING
    # =========================================================================

    def _deterministic_facts(self, text: str) -> List[Tuple[str, str, float]]:
        """Pattern-extract ``(content, category, importance)`` triples from a turn.

        Pure: it stores nothing. Both the SQLite and the native harvesters run
        these same patterns, so the guaranteed capture path stays in Python and
        an optional model is never required.
        """
        facts: List[Tuple[str, str, float]] = []
        clean = (text or "").strip()
        if not clean:
            return facts
        clauses = re.split(r"(?:\s+(?:and|also|plus|moreover)\s+|[.,;!?\n]+)", clean)

        for clause in clauses:
            clause_str = clause.strip()
            if not clause_str:
                continue
            for pattern, cat, imp in FACT_HARVEST_PATTERNS:
                match = re.search(pattern, clause_str, re.IGNORECASE)
                if match:
                    # If multiple capture groups, take the last non-empty one (e.g. favorite editor is Neovim)
                    groups = [g for g in match.groups() if g]
                    fact_body = groups[-1].strip() if groups else ""
                    if len(fact_body) >= 2:
                        if cat == "identity":
                            content = f"User name (identity): {fact_body}"
                        elif cat == "preference" and len(groups) >= 2:
                            content = f"User preferred {groups[0].strip()}: {groups[1].strip()}"
                        else:
                            content = f"User {cat}: {fact_body}"
                        facts.append((content, cat, imp))
                        break

        return facts

    def auto_harvest_facts(self, text: str) -> List[str]:
        """Scans conversational input and automatically extracts facts to store."""
        harvested: List[str] = []
        for content, cat, imp in self._deterministic_facts(text):
            self.remember(content, category=cat, importance=imp)
            harvested.append(content)
        return harvested

    def harvest_turn(self, text: str) -> List[str]:
        """Harvest one conversational turn into memory, deterministically.

        In unified mode the profile harvester runs first so a statement it owns
        (a name, a stated preference) is captured as profile memory and the
        cortex pass merely reinforces that entry instead of duplicating it.
        Statements only the cortex patterns understand land as cortex records.
        The SQLite path stores the deterministic facts directly.
        """
        facts = self._deterministic_facts(text)
        if self._native:
            try:
                import memory.memory_manager as mm
                mm.harvest_turn(text)
            except Exception:
                pass
            return self._store.harvest(text, facts)
        harvested: List[str] = []
        for content, cat, imp in facts:
            self.remember(content, category=cat, importance=imp)
            harvested.append(content)
        return harvested

    # =========================================================================
    # HABIT GRAPH TRACKING
    # =========================================================================

    def record_habit_event(self, trigger_event: str, action_taken: str) -> None:
        """Records a correlation between a context trigger and an action."""
        if self._native:
            self._store.record_habit(trigger_event, action_taken)
            return
        clean_trig = trigger_event.strip().lower()
        clean_act = action_taken.strip().lower()
        if not clean_trig or not clean_act:
            return

        now = time.time()
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO habits (trigger_event, suggested_action, frequency, last_triggered)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(trigger_event) DO UPDATE SET
                        suggested_action = excluded.suggested_action,
                        frequency = frequency + 1,
                        last_triggered = excluded.last_triggered;
                    """,
                    (clean_trig, clean_act, now)
                )
                conn.commit()

    def get_habit_suggestion(self, trigger_event: str, min_frequency: int = 2) -> Optional[str]:
        """Returns the suggested action for a given trigger if it has occurred >= min_frequency times."""
        if self._native:
            return self._store.habit_suggestion(trigger_event, min_frequency=min_frequency)
        clean_trig = trigger_event.strip().lower()
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT suggested_action, frequency FROM habits WHERE trigger_event = ?",
                    (clean_trig,)
                )
                row = cursor.fetchone()
                if row and row["frequency"] >= min_frequency:
                    return row["suggested_action"]
        return None

    def get_stats(self) -> Dict[str, Any]:
        """Returns memory metrics for diagnostics & HUD."""
        if self._native:
            return self._store.get_stats()
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) as total FROM memories;")
                total_mem = cursor.fetchone()["total"]
                cursor.execute("SELECT COUNT(*) as total FROM habits;")
                total_habits = cursor.fetchone()["total"]
                cursor.execute("SELECT category, COUNT(*) as cnt FROM memories GROUP BY category;")
                cat_counts = {row["category"]: row["cnt"] for row in cursor.fetchall()}

        return {
            "total_memories": total_mem,
            "total_habits": total_habits,
            "categories": cat_counts,
            "db_path": str(self.db_path)
        }


# Singleton accessor
def get_memory_cortex() -> MemoryCortex:
    return MemoryCortex.get_instance()
