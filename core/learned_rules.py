"""
Learned Rules Engine for Saturn
Enables continuous self-improvement by capturing user corrections, preferred behaviors,
and habits, then injecting them dynamically into the core LLM prompt without modifying source code.
"""

from __future__ import annotations
from core.user_paths import get_user_data_dir

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("LearnedRules")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = get_user_data_dir() / "config"
RULES_FILE = CONFIG_DIR / "learned_rules.json"


class LearnedRulesEngine:
    """Manages persistent behavioral rules and directives learned from the user."""

    @staticmethod
    def _load_raw() -> List[Dict[str, Any]]:
        if not RULES_FILE.exists():
            return []
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"[LearnedRules] Failed to load rules: {e}")
            return []

    @staticmethod
    def _save_raw(rules: List[Dict[str, Any]]) -> bool:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(RULES_FILE, "w", encoding="utf-8") as f:
                json.dump(rules, f, indent=4)
            return True
        except Exception as e:
            logger.error(f"[LearnedRules] Failed to save rules: {e}")
            return False

    @classmethod
    def add_rule(cls, rule_text: str, category: str = "general", origin: str = "user_correction") -> Dict[str, Any]:
        """Adds a new learned rule to the database."""
        clean_text = rule_text.strip()
        if not clean_text:
            return {"success": False, "message": "Rule text cannot be empty."}

        rules = cls._load_raw()

        # Deduplicate
        for r in rules:
            if r.get("rule", "").lower() == clean_text.lower():
                r["active"] = True
                r["updated_at"] = time.time()
                cls._save_raw(rules)
                return {"success": True, "rule": r, "message": "Rule already exists and is active."}

        new_rule = {
            "id": str(uuid.uuid4())[:8],
            "rule": clean_text,
            "category": category,
            "origin": origin,
            "active": True,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        rules.append(new_rule)
        cls._save_raw(rules)
        return {"success": True, "rule": new_rule, "message": f"Learned new rule: '{clean_text}'"}

    @classmethod
    def list_rules(cls, active_only: bool = False) -> List[Dict[str, Any]]:
        """Returns all stored rules."""
        rules = cls._load_raw()
        if active_only:
            return [r for r in rules if r.get("active", True)]
        return rules

    @classmethod
    def toggle_rule(cls, rule_id: str) -> Dict[str, Any]:
        """Toggles a rule on or off."""
        rules = cls._load_raw()
        for r in rules:
            if r.get("id") == rule_id:
                r["active"] = not r.get("active", True)
                r["updated_at"] = time.time()
                cls._save_raw(rules)
                status = "activated" if r["active"] else "deactivated"
                return {"success": True, "message": f"Rule {rule_id} {status}."}
        return {"success": False, "message": f"Rule ID '{rule_id}' not found."}

    @classmethod
    def delete_rule(cls, rule_id: str) -> Dict[str, Any]:
        """Deletes a rule permanently."""
        rules = cls._load_raw()
        initial_len = len(rules)
        rules = [r for r in rules if r.get("id") != rule_id]
        if len(rules) < initial_len:
            cls._save_raw(rules)
            return {"success": True, "message": f"Rule {rule_id} deleted."}
        return {"success": False, "message": f"Rule ID '{rule_id}' not found."}

    @classmethod
    def get_prompt_injections(cls) -> str:
        """Formats all active rules for runtime system prompt injection."""
        active = [r.get("rule", "").strip() for r in cls.list_rules(active_only=True) if r.get("rule")]
        if not active:
            return ""

        lines = ["\nUSER PREFERENCES & LEARNED BEHAVIORAL DIRECTIVES:"]
        for i, rule in enumerate(active, 1):
            lines.append(f"{i}. {rule}")
        return "\n".join(lines) + "\n"

    @classmethod
    def detect_rule(cls, text: str) -> Optional[str]:
        """
        Returns the rule a sentence is teaching, or None - and writes nothing.

        Detection and storage used to be one call that stored as a side effect,
        which made the lesson impossible to refuse: by the time anything could
        ask "should this be learned?" the file was already written. The two are
        separate now so the classifier can recognise a lesson (and give it a
        trust-gated intent) without performing it.

        e.g.:
        - 'no, don't open edge, use chrome' -> 'Prefer use chrome instead of open edge'
        - 'from now on, keep volume at 50' -> 'Keep volume at 50'
        - 'always save documents to desktop' -> 'Always save documents to desktop'
        """
        import re
        raw = text.strip()
        low = raw.lower()

        rule_text = None

        # Pattern A: "no, don't ... / don't ..., use/do ..."
        m_neg_pos = re.search(r"(?:no,?\s+)?(?:don't|do not)\s+([^,;]+)[,;]\s*(?:use|do|prefer|always)?\s*(.+)", low)
        if m_neg_pos and len(m_neg_pos.group(1)) > 2 and len(m_neg_pos.group(2)) > 2:
            bad = m_neg_pos.group(1).strip()
            good = m_neg_pos.group(2).strip()
            rule_text = f"Prefer {good} instead of {bad}"

        # Pattern B: "from now on, ..."
        elif low.startswith("from now on, ") or low.startswith("from now on "):
            rule_text = raw[12:].strip().lstrip(",").strip()

        # Pattern C: "always / never" directives with substance
        elif (low.startswith("always ") or low.startswith("never ")) and len(low.split()) >= 3:
            if not any(low.startswith(p) for p in ("always open ", "always play ")):
                rule_text = raw.strip()

        # Pattern D: "remember that ...", "remember to ..."
        elif low.startswith("remember that ") or low.startswith("remember to "):
            prefix_len = 14 if low.startswith("remember that ") else 12
            rule_text = raw[prefix_len:].strip()

        # Pattern D2: "new rule: ..." - this phrasing used to be caught by a
        # keyword branch in main.py, which stored the sentence lowercased. It is
        # asked here now, so the learner is the only place a rule is recognised.
        elif low.startswith("new rule") and ":" in raw:
            rule_text = raw.split(":", 1)[1].strip()

        # Pattern E: "i prefer X over Y" / "i prefer X instead of Y"
        elif "i prefer " in low and (" over " in low or " instead of " in low):
            rule_text = raw.strip()

        if rule_text and len(rule_text) >= 5:
            return rule_text[0].upper() + rule_text[1:]

        return None

    @classmethod
    def detect_and_learn_conversational_rule(cls, text: str) -> Optional[Dict[str, Any]]:
        """Detect and store in one call, for callers that want both.

        The turn pipeline no longer uses this (it detects at classification time
        and stores through a trust-gated handler); it is kept because "what did
        Saturn learn from that sentence" is a reasonable single question.
        """
        rule = cls.detect_rule(text)
        if not rule:
            return None
        res = cls.add_rule(rule, category="conversational_preference", origin="autonomous_dialogue")
        return res.get("rule")

