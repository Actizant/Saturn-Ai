"""
core/cognition_loop.py
======================
Saturn - the closed cognition loop.

One seam that turns the separate engines into a single cycle::

    perceive -> recall -> act -> reflect -> learn

- ``perceive`` harvests durable facts from a real conversational turn into
  ``MemoryCortex``, so the advanced brain is written to at runtime instead of
  being read once at startup with a hardcoded query.
- ``recall_context`` assembles the per-turn context block from the cortex
  (query-specific) merged with the profile store, deduplicated so the prompt
  never carries the same fact twice.
- ``observe_action`` runs ``ExecutionReflectionEngine`` over every tool result
  and, when the action verified, records the habit so Saturn notices its own
  recurring patterns.

Every entry point degrades to a neutral value instead of raising: one broken
engine must never take down a conversational turn.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional

# Words that carry no signal for a habit trigger (pronouns, filler, politeness).
_TRIGGER_STOP_WORDS = frozenset({
    "a", "about", "an", "and", "are", "at", "be", "saturn", "but", "by", "can",
    "could", "did", "do", "does", "for", "from", "hey", "how", "i", "if", "in",
    "is", "it", "its", "just", "me", "my", "now", "of", "ok", "okay", "on", "or",
    "please", "sir", "so", "thank", "thanks", "that", "the", "then", "this", "to",
    "was", "were", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
})

# Signals that a tool result dict, which often carries only a plain string,
# actually describes a failure.
_FAILURE_MARKERS = (
    "failed:",
    "tool execution failed",
    "traceback (most recent call last)",
    "unknown tool",
)

_WARM_QUERY = "user identity preferences projects hardware environment"


def _log(message: str) -> None:
    """Report a degraded cognition step without ever interrupting a turn."""
    try:
        print(f"[CognitionLoop] {message}")
    except Exception:
        pass


def _line_key(line: str) -> str:
    """Identity of a memory line, ignoring its bullet and category prefix."""
    stripped = re.sub(r"^-\s*(?:\([^)]*\)\s*)?", "", line)
    return re.sub(r"\s+", " ", stripped.strip().lower())


def _merge_blocks(blocks: List[str]) -> str:
    """Merge context blocks from several memory stores into one prompt block.

    Headers are kept once and any memory line repeated across stores is
    dropped, so the same profile fact is never sent to the model twice.
    """
    headers: List[str] = []
    body: List[str] = []
    notes: List[str] = []
    seen: set = set()
    for block in blocks:
        for raw in (block or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.endswith(":") and not line.startswith("-"):
                if line not in headers:
                    headers.append(line)
                continue
            if not line.startswith("-"):
                # A prose instruction ("Use this background knowledge...")
                # belongs after the facts, never between two memory stores.
                if line not in notes:
                    notes.append(line)
                continue
            key = _line_key(line)
            if key in seen:
                continue
            seen.add(key)
            body.append(line)
    if not body:
        return ""
    return "\n".join(headers + body + notes)


class CognitionLoop:
    """Singleton owner of the live perceive/recall/reflect wiring."""

    _instance: Optional["CognitionLoop"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_perceived = ""
        self._last_user_text = ""
        self.stats: Dict[str, int] = {
            "recall": 0,
            "perceive": 0,
            "facts": 0,
            "observed": 0,
            "failed": 0,
            "habits": 0,
        }

    @classmethod
    def get_instance(cls) -> "CognitionLoop":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = CognitionLoop()
        return cls._instance

    # ── Perception ───────────────────────────────────────────────────────

    def perceive(self, user_text: str, saturn_text: str = "") -> List[str]:
        """Harvest durable facts from one real turn into the memory cortex.

        Idempotent and length-guarded: the same utterance is harvested once, so
        a retried or mirrored turn cannot inflate a fact's importance.
        """
        text = (user_text or "").strip()
        if len(text) < 4:
            return []
        with self._lock:
            if text == self._last_perceived:
                return []
            self._last_perceived = text
            self._last_user_text = text
        try:
            from core.memory_cortex import MemoryCortex

            harvested = MemoryCortex.get_instance().harvest_turn(text) or []
        except Exception as exc:
            _log(f"perceive skipped: {exc}")
            return []
        if harvested:
            with self._lock:
                self.stats["perceive"] += 1
                self.stats["facts"] += len(harvested)
        return list(harvested)

    # ── Recall ───────────────────────────────────────────────────────────

    def recall_context(self, user_query: str, max_items: int = 4, limit: int = 5) -> str:
        """Per-turn, query-specific context from the cortex plus the profile store."""
        query = (user_query or "").strip()
        if not query:
            return ""
        blocks: List[str] = []
        try:
            from core.memory_cortex import MemoryCortex

            cortex_block = MemoryCortex.get_instance().build_context_prompt(query, max_items=max_items)
            if cortex_block:
                blocks.append(cortex_block)
        except Exception as exc:
            _log(f"cortex recall skipped: {exc}")
        try:
            from workspace_store import store as workspace_store

            profile_block = workspace_store().memory_context(query, limit=limit)
            if profile_block:
                blocks.append(profile_block)
        except Exception as exc:
            _log(f"profile recall skipped: {exc}")
        habit_hint = self._habit_hint(query)
        if habit_hint:
            blocks.append(habit_hint)
        merged = _merge_blocks(blocks)
        if merged:
            with self._lock:
                self.stats["recall"] += 1
            self._note_memory(merged)
        return merged

    @staticmethod
    def _note_memory(merged: str) -> None:
        """Publish what was recalled so every other engine sees the same context."""
        try:
            from core.world_model import world_model

            world_model.note_memory(merged)
        except Exception as exc:
            _log(f"world model not updated with memory: {exc}")

    def _habit_hint(self, query: str) -> str:
        """Turn a learned pattern into context, so learning feeds back into recall.

        A habit is only surfaced after three repetitions; one accidental pairing
        must never become a standing instruction.
        """
        trigger = _normalize_trigger(query)
        if not trigger:
            return ""
        try:
            from core.memory_cortex import MemoryCortex

            suggestion = MemoryCortex.get_instance().get_habit_suggestion(trigger, min_frequency=3)
        except Exception as exc:
            _log(f"habit lookup skipped: {exc}")
            return ""
        if not suggestion:
            return ""
        return (
            "[LEARNED HABIT]:\n"
            f"- When asked about {trigger}, this user usually wants {suggestion}."
        )

    def warm_context(self) -> str:
        """Cortex-only pre-load used once at startup, before any turn exists."""
        try:
            from core.memory_cortex import MemoryCortex

            return MemoryCortex.get_instance().build_context_prompt(_WARM_QUERY) or ""
        except Exception as exc:
            _log(f"warm context skipped: {exc}")
            return ""

    # ── Reflection ───────────────────────────────────────────────────────

    def observe_action(
        self,
        action_name: str,
        args: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
        trigger: str = "",
    ):
        """Verify one executed action and learn the habit it belongs to.

        Returns the ``ReflectionResult`` so the caller can phrase a diagnosis in
        Saturn's own voice, or ``None`` when reflection itself is unavailable.
        """
        name = (action_name or "").strip()
        if not name:
            return None
        payload = dict(result or {})
        if "success" not in payload:
            failed = _looks_like_failure(payload)
            payload["success"] = not failed
            if failed and not payload.get("error"):
                payload["error"] = str(
                    payload.get("result") or payload.get("message") or payload.get("output") or ""
                ).strip()
        try:
            from core.execution_reflection import get_reflection_engine

            reflection = get_reflection_engine().reflect_and_verify(
                name, payload, context=dict(args or {})
            )
        except Exception as exc:
            _log(f"reflection skipped for {name}: {exc}")
            return None

        with self._lock:
            self.stats["observed"] += 1
        if reflection is None:
            return None
        self._note_action(name, reflection)
        if not reflection.verified_success:
            with self._lock:
                self.stats["failed"] += 1
            return reflection

        habit_trigger = (trigger or "").strip() or self._habit_trigger()
        if habit_trigger:
            try:
                from core.memory_cortex import MemoryCortex

                MemoryCortex.get_instance().record_habit_event(habit_trigger, name)
                with self._lock:
                    self.stats["habits"] += 1
            except Exception as exc:
                _log(f"habit not recorded for {name}: {exc}")
        return reflection

    @staticmethod
    def _note_action(name: str, reflection) -> None:
        """Publish the action and its verdict to the shared situation."""
        try:
            from core.world_model import world_model

            world_model.note_action(
                name,
                ok=bool(getattr(reflection, "verified_success", False)),
                diagnosis=str(getattr(reflection, "diagnosis", "") or ""),
            )
        except Exception as exc:
            _log(f"world model not updated with action: {exc}")

    def _habit_trigger(self) -> str:
        """The recurring situation a habit is keyed on.

        A dispatched intent is preferred over the raw utterance: live data showed
        habits keyed on "see whast going ide" - speech-to-text noise that will
        never repeat identically, so the habit could never fire again. The
        intent is already normalized, which makes the key stable.
        """
        try:
            from core.world_model import world_model

            intent = world_model.last_intent()
            if intent:
                return intent
        except Exception as exc:
            _log(f"intent trigger unavailable: {exc}")
        with self._lock:
            text = self._last_user_text
        return _normalize_trigger(text)


def _normalize_trigger(text: str) -> str:
    """Reduce a query to the content words a recurring habit is keyed on."""
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    keep = [w for w in words if w not in _TRIGGER_STOP_WORDS and len(w) > 1]
    return " ".join(keep[:6])


def _looks_like_failure(payload: Dict[str, Any]) -> bool:
    """Infer failure from a result dict that carries only human-readable text."""
    if payload.get("error"):
        return True
    if payload.get("ok") is False or payload.get("status") == "error":
        return True
    blob = " ".join(str(payload.get(key, "")) for key in ("result", "message", "output"))
    low = blob.strip().lower()
    if not low:
        return False
    return any(marker in low for marker in _FAILURE_MARKERS)


cognition_loop = CognitionLoop.get_instance()
