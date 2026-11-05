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
    # Timestamped outcomes for the Creators page telemetry: (epoch_s, ok,
    # latency_ms, error_or_None). Bounded so a long-lived session cannot
    # grow memory without limit; 400 events cover the 14-day chart easily
    # for human-scale traffic.
    events: Deque[tuple] = field(default_factory=lambda: deque(maxlen=400))
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
        self.events.append((time.time(), ok, latency_ms, (error or "")[:200] or None))
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
    # Self-heal tally survives the record purge a reforge does, so the
    # Creators page can still say "this server healed 3 times". Keyed by
    # server (or feature) name, not by the individual tool verb.
    _heal_counts: Dict[str, int] = {}

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

    @classmethod
    def tool_names_for(cls, capability: str, tier: str) -> List[str]:
        """The recorded verb keys belonging to one capability.

        * an MCP server owns every ``mcp_<capability>_*`` tool,
        * a lightweight FEATURE is a single verb recorded under its own name.
        """
        with cls._lock:
            if tier == "mcp":
                prefix = f"mcp_{capability}_"
                return [n for n in cls._records if n.startswith(prefix)]
            return [capability] if capability in cls._records else []

    @classmethod
    def rollup(cls, capability: str, tier: str) -> Dict[str, object]:
        """Aggregate a capability's events into the shapes the Creators page
        draws: 24-hour and 14-day success/failure buckets, totals, latency,
        the last five errors and a health verdict.

        Everything is derived from the timestamped ``events`` deques so the
        charts reflect real traffic, not the last-10 rolling window the
        unhealthy-detector uses.
        """
        now = time.time()
        names = cls.tool_names_for(capability, tier)
        buckets_24 = [{"ok": 0, "failed": 0} for _ in range(24)]
        buckets_14 = [{"ok": 0, "failed": 0} for _ in range(14)]
        calls_14d = ok_14d = fail_14d = 0
        lat_sum = lat_n = 0
        last_used = 0.0
        errors: List[Dict[str, object]] = []
        any_unhealthy = any_failing = False
        with cls._lock:
            for n in names:
                rec = cls._records.get(n)
                if rec is None:
                    continue
                if rec.is_unhealthy(cls.MIN_CALLS, cls.CONSECUTIVE_THRESHOLD,
                                    cls.FAILURE_RATE_THRESHOLD):
                    any_unhealthy = True
                    if rec.consecutive_failures >= cls.CONSECUTIVE_THRESHOLD:
                        any_failing = True
                for (ts, ok, lat, err) in rec.events:
                    age = now - ts
                    if age < 86400:
                        b = buckets_24[min(23, int(age / 3600))]
                        b["ok" if ok else "failed"] += 1
                    if age < 14 * 86400:
                        d = buckets_14[min(13, int(age / 86400))]
                        d["ok" if ok else "failed"] += 1
                        calls_14d += 1
                        if ok:
                            ok_14d += 1
                        else:
                            fail_14d += 1
                    lat_sum += lat
                    lat_n += 1
                    last_used = max(last_used, ts)
                    if not ok and err:
                        errors.append({"at": ts, "tool": n, "message": err})
        errors.sort(key=lambda e: e["at"], reverse=True)
        health = "err" if any_failing else ("warn" if any_unhealthy else "ok")
        return {
            "health": health,
            "calls_24h": list(reversed(buckets_24)),
            "calls_14d": list(reversed(buckets_14)),
            "calls_14d_total": calls_14d,
            "success_pct": round(100.0 * ok_14d / calls_14d, 1) if calls_14d else 100.0,
            "avg_latency_ms": round(lat_sum / lat_n, 1) if lat_n else 0.0,
            "last_used_at": last_used,
            "last_errors": errors[:5],
            "heal_count": cls._heal_counts.get(capability, 0),
        }

    @classmethod
    def note_heal(cls, capability: str) -> None:
        with cls._lock:
            cls._heal_counts[capability] = cls._heal_counts.get(capability, 0) + 1

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
            cls._heal_counts.clear()

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
            # Replay the user's original words when the forge stored them;
            # a description-only guess loses the intent behind the tool.
            goal = (manifest.get("goal") or manifest.get("description")
                    or f"Reforge {server}")
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
        # The fresh copy starts clean, but the heal tally is kept so the
        # Creators page still shows how many times Saturn has repaired it.
        CapabilityHealth.note_heal(server_name)
        with CapabilityHealth._lock:
            for k in list(CapabilityHealth._records.keys()):
                if k.startswith(f"mcp_{server_name}_"):
                    CapabilityHealth._records.pop(k, None)
    except Exception as exc:
        logger.warning(f"[Health] reforge of '{server_name}' failed: {exc}")
