"""
core/capability_health.py
=========================
Saturn's capability immune system.

Every MCP tool call (native or forged) is recorded here with its outcome,
latency, and error text. When a tool keeps failing, the manager decides the
server is "unwell" and Saturn can either (a) tell the user it needs a
reforge or (b) trigger a reforge autonomously with the failure evidence
baked into the repair prompt.

The state is intentionally in-memory. Restarting Saturn resets the record,
which is exactly what we want: after a re-launch, the pipeline has to earn
trust again. Persistence is a follow-on.

Design notes:
- Cheap to record. No LLM, no I/O.
- Thresholds are conservative: 3 consecutive failures OR 50% failures over
  the last 10 calls, with a floor of at least 5 total calls so a brand-new
  server does not immediately get flagged from a single flaky first call.
- Cooldown: once a server is flagged and re-forge is offered, we will not
  nag again for the same tool for ``_cooldown`` seconds even if it keeps
  failing (the reforge itself takes a minute; two nags in that window are
  noise).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

logger = logging.getLogger("CapabilityHealth")


@dataclass
class ToolRecord:
    """Rolling window of outcomes for one MCP tool."""

    name: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    recent: Deque[bool] = field(default_factory=lambda: deque(maxlen=10))
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_error_at: float = 0.0
    avg_latency_ms: float = 0.0
    _last_latency_count: int = 0
    flagged_at: float = 0.0
    last_reforge_prompt_at: float = 0.0

    def record(self, ok: bool, latency_ms: float, error: Optional[str]) -> None:
        self.calls += 1
        self.recent.append(ok)
        if ok:
            self.successes += 1
            self.consecutive_failures = 0
        else:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = (error or "")[:400]
            self.last_error_at = time.time()
        # Rolling mean, kept cheap. No need for a Welford chain here since
        # we only ever display this as a "server X is slow" hint.
        self._last_latency_count += 1
        self.avg_latency_ms = (
            self.avg_latency_ms + (latency_ms - self.avg_latency_ms)
            / self._last_latency_count
        )

    def is_unhealthy(self, min_calls: int, consecutive_threshold: int,
                     failure_rate_threshold: float) -> bool:
        if self.calls < min_calls:
            return False
        if self.consecutive_failures >= consecutive_threshold:
            return True
        recent_ok = sum(1 for r in self.recent if r)
        recent_fail = len(self.recent) - recent_ok
        if len(self.recent) >= min_calls and recent_fail / max(1, len(self.recent)) >= failure_rate_threshold:
            return True
        return False


class CapabilityHealth:
    """Class-level store of per-tool outcome records.

    The MCP manager writes here on every call and reads here when planning
    whether a server is worth invoking or should be reforged.
    """

    _lock = threading.RLock()
    _records: Dict[str, ToolRecord] = {}

    # Tuning knobs (settable in tests).
    MIN_CALLS = 5
    CONSECUTIVE_THRESHOLD = 3
    FAILURE_RATE_THRESHOLD = 0.5
    COOLDOWN_S = 300.0        # do not nag about the same tool twice in 5 min
    NAG_COOLDOWN_S = 600.0    # do not offer reforge more than every 10 min

    # ── Recording ────────────────────────────────────────────────────────────

    @classmethod
    def record(cls, tool_name: str, ok: bool, latency_ms: float,
               error: Optional[str] = None) -> None:
        with cls._lock:
            rec = cls._records.get(tool_name)
            if rec is None:
                rec = ToolRecord(name=tool_name)
                cls._records[tool_name] = rec
            rec.record(ok, latency_ms, error)

    # ── Reading ──────────────────────────────────────────────────────────────

    @classmethod
    def stats(cls, tool_name: str) -> Optional[ToolRecord]:
        with cls._lock:
            return cls._records.get(tool_name)

    @classmethod
    def is_unhealthy(cls, tool_name: str) -> bool:
        with cls._lock:
            rec = cls._records.get(tool_name)
            if rec is None:
                return False
            return rec.is_unhealthy(
                min_calls=cls.MIN_CALLS,
                consecutive_threshold=cls.CONSECUTIVE_THRESHOLD,
                failure_rate_threshold=cls.FAILURE_RATE_THRESHOLD,
            )

    @classmethod
    def unhealthy_tools(cls) -> List[ToolRecord]:
        """Every currently-flagged tool. Saturn reads this once per turn to
        decide whether to speak up about a broken capability."""
        with cls._lock:
            return [
                rec for rec in cls._records.values()
                if rec.is_unhealthy(
                    min_calls=cls.MIN_CALLS,
                    consecutive_threshold=cls.CONSECUTIVE_THRESHOLD,
                    failure_rate_threshold=cls.FAILURE_RATE_THRESHOLD,
                )
            ]

    @classmethod
    def server_summary(cls, server_name: str) -> Dict[str, int]:
        """Aggregate counts for one MCP server: how many of its tools are
        healthy, unhealthy, and total. Used by the HUD and the Tasks page."""
        prefix = f"mcp_{server_name}_"
        out = {"total": 0, "healthy": 0, "unhealthy": 0}
        with cls._lock:
            for name, rec in cls._records.items():
                if not name.startswith(prefix):
                    continue
                out["total"] += 1
                if rec.is_unhealthy(
                    min_calls=cls.MIN_CALLS,
                    consecutive_threshold=cls.CONSECUTIVE_THRESHOLD,
                    failure_rate_threshold=cls.FAILURE_RATE_THRESHOLD,
                ):
                    out["unhealthy"] += 1
                else:
                    out["healthy"] += 1
        return out

    # ── Cooldown / marking ───────────────────────────────────────────────────

    @classmethod
    def should_offer_reforge(cls, tool_name: str) -> bool:
        """True if Saturn has not nagged about this tool within the cooldown
        window. Call this right before speaking an "unwell" message or kicking
        off a reforge; on True, also call `mark_reforge_offered`."""
        with cls._lock:
            rec = cls._records.get(tool_name)
            if rec is None:
                return False
            now = time.time()
            return (now - rec.last_reforge_prompt_at) >= cls.NAG_COOLDOWN_S

    @classmethod
    def mark_reforge_offered(cls, tool_name: str) -> None:
        with cls._lock:
            rec = cls._records.get(tool_name)
            if rec is None:
                return
            rec.last_reforge_prompt_at = time.time()

    # ── Failure evidence for the reforge loop ────────────────────────────────

    @classmethod
    def failure_evidence(cls, tool_name: str) -> str:
        """Human/LLM-readable digest of what has been going wrong with a tool.
        Fed into SkillForge / MCPForge as extra context when Saturn decides to
        re-forge a drifting capability."""
        with cls._lock:
            rec = cls._records.get(tool_name)
            if rec is None:
                return ""
            recent_lines: List[str] = []
            for i, ok in enumerate(rec.recent):
                recent_lines.append(f"call {i + 1}: {'ok' if ok else 'FAIL'}")
            return (
                f"Tool '{tool_name}' has failed {rec.consecutive_failures} time(s) "
                f"in a row ({rec.failures}/{rec.calls} overall, avg latency "
                f"{rec.avg_latency_ms:.0f} ms). Last error: {rec.last_error or '(none recorded)'}. "
                f"Recent history: {' | '.join(recent_lines)}."
            )

    # ── Testing / reset ─────────────────────────────────────────────────────

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._records.clear()

    # ── Autonomous reforge loop ───────────────────────────────────────────

    @classmethod
    def sweep_and_reforge(cls, *, speak: Optional[Callable[..., None]] = None,
                          ui: Optional[object] = None,
                          limit: int = 1) -> List[str]:
        """Scan for unhealthy forged MCP servers and, when the failing tool
        belongs to a server Saturn itself forged, trigger a re-forge with
        the failure evidence baked into the repair context.

        Deliberately conservative:
        - Only re-forge servers whose manifest says ``tier == 'portable'``.
          Third-party servers are the user's problem; we do not silently
          overwrite them.
        - Only re-forge ``limit`` servers per sweep (default 1). Reforges
          take 30-60s; running them back-to-back would be worse than the
          original bug.
        - Cooldown via ``NAG_COOLDOWN_S`` prevents re-nagging about the
          same drifting tool.
        """
        unhealthy = cls.unhealthy_tools()
        if not unhealthy:
            return []
        try:
            from core.mcp_forge import MCPForge
            known = {m.get("name"): m for m in MCPForge.list_forged()}
        except Exception:
            known = {}
        if not known:
            return []

        reforged: List[str] = []
        for rec in unhealthy:
            if len(reforged) >= limit:
                break
            if not rec.name.startswith("mcp_"):
                continue
            parts = rec.name[4:].split("_", 1)
            if len(parts) < 2:
                continue
            server = parts[0]
            manifest = known.get(server)
            if not manifest or manifest.get("tier") != "portable":
                continue
            if not cls.should_offer_reforge(rec.name):
                continue
            cls.mark_reforge_offered(rec.name)
            evidence = cls.failure_evidence(rec.name)
            goal = manifest.get("description") or f"Reforge {server}"
            hint = (
                "Auto-reforge triggered by Saturn's capability health "
                f"monitor.\n{evidence}"
            )
            if speak:
                try:
                    speak(f"Sir, {server} has been failing. I am re-forging it.")
                except Exception:
                    pass
            if ui is not None and hasattr(ui, "write_log"):
                try:
                    ui.write_log(f"[Health] Reforging '{server}' - {evidence}")
                except Exception:
                    pass
            threading.Thread(
                target=_run_reforge,
                args=(server, goal, hint),
                name=f"Reforge-{server}",
                daemon=True,
            ).start()
            reforged.append(server)
        return reforged


def _run_reforge(server_name: str, goal: str, hint: str) -> None:
    """Worker body for a self-heal reforge. Isolated so a crash does not
    take down the sweep loop."""
    try:
        from core.mcp_forge import MCPForge
        MCPForge.forge_mcp_server(goal=goal, skill_name=server_name,
                                  context_hints=hint, max_repair_attempts=1)
        # Reset stats for the server so the fresh copy starts clean.
        with CapabilityHealth._lock:
            for k in list(CapabilityHealth._records.keys()):
                if k.startswith(f"mcp_{server_name}_"):
                    CapabilityHealth._records.pop(k, None)
    except Exception as exc:
        logger.warning(f"[Health] reforge of '{server_name}' failed: {exc}")
