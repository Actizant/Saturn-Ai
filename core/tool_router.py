"""
core/tool_router.py
===================
Saturn's tool selector.

Once Saturn starts forging portable MCP servers, the number of available
tools grows fast. Every MCP server adds 1-N tools; every forged skill
adds another function. Dumping all of them into the planner prompt has two
problems:

1. It blows up token cost (both Gemini and OpenRouter bill per prompt token).
2. Beyond ~40 tools the LLM's routing quality drops sharply - it starts
   picking plausible-sounding tools that do not fit the goal.

The Tool Router picks the top-K tools most relevant to the current goal
using a lightweight TF-IDF scoring over tool descriptions. No embedding
model, no external API call, no numpy dep. Runs in microseconds.

Design:
- Corpus = every tool name + description currently live (native + dynamic
  + MCP). Rebuilt lazily per turn; the number of tools is small enough
  that rebuilding is cheap and always reflects the live state.
- Score = sum over goal tokens of (idf(token) * contains(token in tool_text)).
  We treat each tool as a binary bag of tokens; document length is not
  normalised because tool descriptions are already short and similar.
- Result = top-K by score, plus every tool with score above a floor so a
  perfect single match does not get dropped by K. Native always-available
  tools are marked ``pinned=True`` and always included.

Public surface:

    router = ToolRouter.get_instance()
    router.register_source("mcp", lambda: mgr.get_all_tool_declarations())
    selected = router.select(goal="check bitcoin price", top_k=12)
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Tuple


_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")

# Which sources have been auto-registered for planner use. Module-level so
# repeated planner calls never re-wire the same providers.
_STATE = {"planner_sources_registered": False}


def _tokenize(text: str) -> List[str]:
    """Lowercased alnum tokens. Underscores inside identifiers like
    ``read_resource`` are treated as separators so the sub-words still
    match natural-language goals."""
    if not text:
        return []
    # Split snake_case first, then tokenize.
    expanded = re.sub(r"_", " ", str(text)).lower()
    return _TOKEN_RE.findall(expanded)


class ToolRouter:
    """Selects the top-K most relevant tools for a goal."""

    _instance: Optional["ToolRouter"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # Each source is (label, callable returning list of tool dicts).
        # A tool dict has at minimum {"name": str, "description": str}.
        self._sources: List[Tuple[str, Callable[[], List[Dict]]]] = []
        # Pinned tool names always appear in every result (e.g. `skill_forge`,
        # `web_search` - the "you must never be blind to this" list).
        self._pinned: set[str] = set()
        # Cache of the last-built corpus + IDF map.
        self._corpus_cache: Optional[List[Tuple[str, str, Counter]]] = None
        self._idf_cache: Optional[Dict[str, float]] = None
        self._cache_built_at: float = 0.0
        self._cache_ttl_s: float = 5.0
        self._lock = threading.RLock()

    # ── Singleton ────────────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "ToolRouter":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ToolRouter()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Only for tests. Never call from runtime paths."""
        with cls._instance_lock:
            cls._instance = None

    # ── Registration ─────────────────────────────────────────────────────────

    def register_source(self, label: str, provider: Callable[[], List[Dict]]) -> None:
        """Bind a live tool provider. Called every rebuild; provider must be
        fast (returns a list of ``{name, description, ...}`` dicts)."""
        with self._lock:
            # Replace any existing source under the same label so a re-wire
            # is idempotent.
            self._sources = [s for s in self._sources if s[0] != label]
            self._sources.append((label, provider))
            self._corpus_cache = None

    def pin(self, *tool_names: str) -> None:
        """Tools that appear in every selection regardless of score."""
        with self._lock:
            for n in tool_names:
                self._pinned.add(n)

    def unpin(self, *tool_names: str) -> None:
        with self._lock:
            for n in tool_names:
                self._pinned.discard(n)

    # ── Corpus build ─────────────────────────────────────────────────────────

    def _build_corpus(self) -> Tuple[List[Tuple[str, str, Counter]], Dict[str, float]]:
        """Rebuild the tool corpus if the cache has expired.

        Corpus row = (name, description, token_counter). IDF is computed
        across the corpus with the usual ``log((N + 1) / (df + 1)) + 1``
        smoothing so unseen tokens get a low but nonzero weight.
        """
        now = time.time()
        if (
            self._corpus_cache is not None
            and self._idf_cache is not None
            and (now - self._cache_built_at) < self._cache_ttl_s
        ):
            return self._corpus_cache, self._idf_cache

        rows: List[Tuple[str, str, Counter]] = []
        seen_names: set = set()
        for _label, provider in self._sources:
            try:
                tools = provider() or []
            except Exception:
                tools = []
            for tool in tools:
                name = str(tool.get("name") or "").strip()
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                desc = str(tool.get("description") or "")
                toks = _tokenize(name) + _tokenize(desc)
                rows.append((name, desc, Counter(toks)))

        N = max(1, len(rows))
        df: Dict[str, int] = defaultdict(int)
        for _n, _d, counter in rows:
            for tok in counter:
                df[tok] += 1
        idf: Dict[str, float] = {
            tok: math.log((N + 1) / (c + 1)) + 1.0
            for tok, c in df.items()
        }

        self._corpus_cache = rows
        self._idf_cache = idf
        self._cache_built_at = now
        return rows, idf

    # ── Selection ────────────────────────────────────────────────────────────

    def score(self, goal: str) -> List[Tuple[float, str, str]]:
        """Return every tool with its TF-IDF score for the goal. Sorted
        descending. Exposed for debugging / tests."""
        goal_toks = set(_tokenize(goal))
        if not goal_toks:
            return []
        rows, idf = self._build_corpus()
        out: List[Tuple[float, str, str]] = []
        for name, desc, counter in rows:
            # Every distinct goal token that appears in the tool contributes
            # idf(token) * (log(1+tf) so very-common tokens inside one long
            # description do not crowd out short matches).
            s = 0.0
            for tok in goal_toks:
                tf = counter.get(tok, 0)
                if tf:
                    s += idf.get(tok, 1.0) * (1.0 + math.log(tf))
            out.append((s, name, desc))
        out.sort(reverse=True, key=lambda r: (r[0], r[1]))
        return out

    def select(
        self,
        goal: str,
        top_k: int = 12,
        min_score: float = 1.2,
        always_include: Optional[Iterable[str]] = None,
    ) -> List[Dict]:
        """Pick the tool declarations the planner should see for this goal.

        Returns the full tool dict each source registered (so callers keep
        ``parameters``, ``inputSchema``, etc.). Order = descending relevance.

        - ``top_k`` caps how many are returned (never includes pinned extras).
        - ``min_score`` filters out weak keyword matches so a goal about
          cricket does not drag in "read_resource" just because both have
          the word "the" in the description.
        - ``always_include`` adds specific names back regardless of score
          (used to keep the skill_forge / web_search / dynamic_skill trio
          visible even when the router thinks they are irrelevant).
        """
        rows, _ = self._build_corpus()
        by_name = {name: (desc) for name, desc, _c in rows}
        ranked = self.score(goal)

        picked_names: List[str] = []
        seen: set = set()
        non_pinned_added = 0

        # 1. Pinned names first (they must always appear).
        for name in self._pinned:
            if name in by_name and name not in seen:
                picked_names.append(name)
                seen.add(name)

        # 2. always_include from the caller.
        for name in always_include or ():
            if name in by_name and name not in seen:
                picked_names.append(name)
                seen.add(name)

        # 3. Then rank-ordered matches above the floor, capped at top_k so
        # pinned and always_include extras do not consume a relevance slot.
        for s, name, _desc in ranked:
            if non_pinned_added >= top_k:
                break
            if s < min_score:
                continue
            if name in seen:
                continue
            picked_names.append(name)
            seen.add(name)
            non_pinned_added += 1

        # Look up the full tool dicts by name across all sources.
        full: Dict[str, Dict] = {}
        for _label, provider in self._sources:
            try:
                for tool in provider() or []:
                    n = str(tool.get("name") or "")
                    if n and n not in full:
                        full[n] = tool
            except Exception:
                continue

        return [full[n] for n in picked_names if n in full]

    # ── Planner prompt injection ─────────────────────────────────────────────

    def _ensure_mcp_source(self) -> None:
        """Make sure the live MCP tool list is registered, even when the
        planner runs in a process that never went through Saturn's boot
        wiring in main.py (e.g. agent/executor standalone)."""
        if _STATE["planner_sources_registered"]:
            return
        _STATE["planner_sources_registered"] = True
        try:
            from core.mcp_manager import UniversalMCPManager
            self.register_source(
                "mcp",
                lambda: UniversalMCPManager.get_instance().get_all_tool_declarations(),
            )
        except Exception:
            pass

    def planner_lines(self, goal: str, top_k: int = 8,
                      min_score: float = 1.0) -> List[str]:
        """Router-selected MCP tools rendered as planner text lines:
        ``name: description Parameters: {json}`` - the same shape the
        planner prompt already uses for dynamic feature tools. Returns []
        when there is no goal or nothing scores above the floor, so the
        planner prompt simply stays unchanged."""
        if not (goal or "").strip():
            return []
        self._ensure_mcp_source()
        lines: List[str] = []
        for tool in self.select(goal=goal, top_k=top_k, min_score=min_score):
            name = str(tool.get("name") or "")
            if not name:
                continue
            schema = tool.get("parameters") or tool.get("inputSchema") or {}
            params = json.dumps(schema, ensure_ascii=True)[:300]
            desc = str(tool.get("description") or "").strip()
            lines.append(f"{name}: {desc} Parameters: {params}")
        return lines
