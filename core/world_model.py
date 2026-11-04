"""
core/world_model.py
===================
Saturn - the single per-turn situation snapshot.

Before this module every engine asked the operating system for the same facts
separately, and they drifted apart:

* the intent dispatcher asked ``self.ui.get_active_window_title()`` - a method
  that does not exist anywhere, so it always received ``""``;
* the Live system instruction and the proactive engine read
  ``snapshot["current_window"]`` / ``["current_process"]`` while
  ``PassiveSensorium.get_snapshot()`` actually returns ``window_title`` /
  ``process_name`` - so both have been silently blind to the active window.

One normalized :class:`Situation` is built per turn and shared. Field names are
defined once here, so a consumer cannot read a key the collector never writes.

Writers (the cognition loop) push what it learned back in: the memories it
recalled, the intents it dispatched, and the last action plus its reflection.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.app_kind import classify as classify_app
from core.app_kind import describe as describe_app

# A snapshot is reused for the whole turn but never across turns: the query is
# part of the cache key, and every writer invalidates it.
DEFAULT_TTL_SECONDS = 2.5
_RECENT_INTENT_LIMIT = 8

# ``PassiveSensorium`` seeds ``current_process_name`` with "Unknown" before its
# first poll, and "Unknown" is a truthy string, so a naive presence check would
# suppress the desktop fallback for the rest of the session.
_PLACEHOLDERS = frozenset({"", "unknown", "none", "null", "n/a", "-"})


def _is_placeholder(value: Any) -> bool:
    return str(value or "").strip().lower() in _PLACEHOLDERS


def _log(message: str) -> None:
    try:
        print(f"[WorldModel] {message}")
    except Exception:
        pass


def part_of_day(now: Optional[datetime] = None) -> str:
    """Human label for the current hour, matching the proactive engine's buckets."""
    hour = (now or datetime.now()).hour
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "late night"


def read_window(snapshot: Optional[Dict[str, Any]]) -> Tuple[str, str, float]:
    """Read ``(title, process, dwell_seconds)`` from any historical shape.

    Accepts the real sensorium keys and the legacy ``current_window`` /
    ``current_process`` pair, so an older caller keeps working while every
    consumer migrates onto the normalized situation.
    """
    data = snapshot or {}
    window = data.get("current_window")
    window = window if isinstance(window, dict) else {}
    title = str(data.get("window_title") or window.get("title") or "")
    process = str(data.get("process_name") or data.get("current_process") or "")
    try:
        dwell = float(data.get("dwell_seconds") or 0.0)
    except (TypeError, ValueError):
        dwell = 0.0
    return title, process, dwell


@dataclass(frozen=True)
class Situation:
    """One immutable view of the user and their machine for a single turn."""

    query: str = ""
    captured_at: float = 0.0
    window_title: str = ""
    window_process: str = ""
    window_class: str = ""
    window_pid: Optional[int] = None
    #: ``foreground`` / ``zorder`` / ``cache`` / ``sensorium`` / ``self`` - how
    #: the window above was chosen, so the prompt can say how sure it is.
    window_source: str = ""
    #: ``ide``, ``terminal``, ``browser`` ... from core.app_kind.
    app_kind: str = ""
    app_label: str = ""
    dwell_seconds: float = 0.0
    user_idle_seconds: float = 0.0
    user_active: bool = True
    battery_percent: Optional[float] = None
    power_plugged: Optional[bool] = None
    cpu_percent: Optional[float] = None
    ram_percent: Optional[float] = None
    connected_devices: Tuple[Dict[str, Any], ...] = ()
    part_of_day: str = ""
    recent_intents: Tuple[str, ...] = ()
    salient_memories: str = ""
    last_action: Optional[str] = None
    last_action_ok: Optional[bool] = None
    last_reflection: str = ""
    sensorium_ts: float = 0.0
    raw_sensorium: Dict[str, Any] = field(default_factory=dict, repr=False)

    def as_dict(self) -> Dict[str, Any]:
        """Flat view for tools: every raw sensorium key plus the normalized ones."""
        data = dict(self.raw_sensorium)
        data.update({
            "window_title": self.window_title,
            "process_name": self.window_process,
            "window_class": self.window_class,
            "window_source": self.window_source,
            "app_kind": self.app_kind,
            "app_label": self.app_label,
            "dwell_seconds": self.dwell_seconds,
            "user_idle_seconds": self.user_idle_seconds,
            "battery_percent": self.battery_percent,
            "power_plugged": self.power_plugged,
            "cpu_percent": self.cpu_percent,
            "ram_percent": self.ram_percent,
            "connected_devices": [dict(d) for d in self.connected_devices],
            "part_of_day": self.part_of_day,
            "recent_intents": list(self.recent_intents),
            "last_action": self.last_action,
            "last_action_ok": self.last_action_ok,
            "last_reflection": self.last_reflection,
            "captured_at": self.captured_at,
        })
        return data

    def brief(self) -> str:
        """The prompt-ready paragraph describing where and how the user is."""
        lines = ["[REAL-TIME SENSORY & SITUATIONAL AWARENESS]"]
        if self.window_title or self.window_process:
            dwell = round(self.dwell_seconds / 60.0, 1)
            heading = "Active Window"
            if self.window_source in ("zorder", "cache"):
                # Saturn had the focus, so this is the user's real work window
                # rather than the one with the caret. Say so instead of
                # implying a focus we do not know about.
                heading = "Active Window (most recently used - Saturn itself has focus)"
            lines.append(
                f"{heading}: '{self.window_title}' "
                f"(Process: {self.window_process}, Active Dwell: {dwell}m)"
            )
            app = describe_app(self.window_title, self.window_process, self.window_class)
            if app:
                lines.append(f"Active App: {app}")
        if self.battery_percent is not None:
            power = "Plugged in (AC)" if self.power_plugged else "On Battery"
            lines.append(f"Battery: {int(self.battery_percent)}% ({power})")
        if self.connected_devices:
            names = ", ".join(
                str(d.get("name") or d.get("device_name") or d.get("id") or "device")
                for d in self.connected_devices[:4]
            )
            lines.append(f"Connected Devices: {names}")
        if self.part_of_day:
            lines.append(f"Time Of Day: {self.part_of_day}")
        if self.last_action:
            verdict = "" if self.last_action_ok is None else (" (succeeded)" if self.last_action_ok else " (failed)")
            lines.append(f"Last Action: {self.last_action}{verdict}")
        if self.window_title or self.window_process:
            lines.append(
                "You have continuous ambient awareness of what the user is working on. "
                "Use this context naturally without stating 'according to my sensors'. "
                "Answer questions about what the user is doing from the window and app "
                "lines above. The application named there is open right now - never say "
                "an app, editor or IDE is not open while it is named. This block is "
                "rebuilt for every request, so when it disagrees with anything earlier in "
                "the conversation - including something you said yourself - the newest "
                "block wins."
            )
        else:
            lines.append(
                "You have continuous ambient awareness of what the user is working on. "
                "Use this context naturally without stating 'according to my sensors'. "
                "Nothing about the active window could be read this time: say so plainly "
                "and ask what they are working in, rather than guessing or repeating an "
                "earlier guess."
            )
        return "\n".join(lines)


class WorldModel:
    """Assembles and caches the per-turn :class:`Situation`; also its writer side."""

    _instance: Optional["WorldModel"] = None
    _instance_lock = threading.Lock()

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._ttl = max(0.0, float(ttl_seconds))
        self._cached: Optional[Situation] = None
        self._cached_at = 0.0
        self._recent_intents: deque = deque(maxlen=_RECENT_INTENT_LIMIT)
        self._salient = ""
        self._last_action: Optional[str] = None
        self._last_action_ok: Optional[bool] = None
        self._last_reflection = ""
        self.stats: Dict[str, int] = {"snapshots": 0, "hits": 0, "memories": 0, "actions": 0}

    @classmethod
    def get_instance(cls) -> "WorldModel":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = WorldModel()
        return cls._instance

    # ── Reading ──────────────────────────────────────────────────────────

    def snapshot(
        self,
        query: str = "",
        *,
        device_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        refresh: bool = False,
    ) -> Situation:
        """The situation for this turn, rebuilt at most once per query/TTL."""
        key = (query or "").strip()
        now = time.monotonic()
        with self._lock:
            cached = self._cached
            fresh = cached is not None and (now - self._cached_at) < self._ttl
        if fresh and not refresh and cached.query == key:
            with self._lock:
                self.stats["hits"] += 1
            return cached

        situation = self._build(key, device_provider)
        with self._lock:
            self._cached = situation
            self._cached_at = time.monotonic()
            self.stats["snapshots"] += 1
        return situation

    def _build(
        self,
        query: str,
        device_provider: Optional[Callable[[], List[Dict[str, Any]]]],
    ) -> Situation:
        sensor = self._collect_sensorium()
        title, process, dwell = read_window(sensor)
        window_class = str(sensor.get("window_class") or "")
        source = "sensorium"

        # Ask the desktop live rather than trusting the sampler. The sensorium
        # polls every three seconds, and "what am I doing?" is asked exactly
        # when the answer has just changed - the one moment a stale sample is
        # wrong. get_active_user_window() also refuses to answer with Saturn's
        # own overlay, which is the window most likely to hold the focus while
        # the user is talking to Saturn.
        live = self._collect_user_window()
        live_title = str(live.get("title") or "").strip()
        if live_title and not _is_placeholder(live_title):
            if live_title != title:
                dwell = 0.0
            title = live_title
            live_process = str(live.get("process_name") or "") or str(live.get("class_name") or "")
            if live_process and not _is_placeholder(live_process):
                process = live_process
            window_class = str(live.get("class_name") or window_class)
            source = str(live.get("method") or "foreground")
        elif _is_placeholder(title) or _is_placeholder(process):
            fallback_title, fallback_process, _ = self._collect_foreground()
            if _is_placeholder(title):
                title = fallback_title
            if _is_placeholder(process):
                process = fallback_process

        title = "" if _is_placeholder(title) else title.strip()
        process = "" if _is_placeholder(process) else process.strip()
        app_kind = ""
        app_label = ""
        if title or process:
            app_kind = classify_app(title, process, window_class)
            app_label = describe_app(title, process, window_class)
        with self._lock:
            intents = tuple(self._recent_intents)
            salient = self._salient
            last_action = self._last_action
            last_ok = self._last_action_ok
            reflection = self._last_reflection
        return Situation(
            query=query,
            captured_at=time.time(),
            window_title=title,
            window_process=process,
            window_class=window_class,
            window_source=source if (title or process) else "",
            app_kind=app_kind,
            app_label=app_label,
            window_pid=sensor.get("pid"),
            dwell_seconds=dwell,
            user_idle_seconds=float(sensor.get("user_idle_seconds") or 0.0),
            user_active=bool(sensor.get("is_user_active", True)),
            battery_percent=sensor.get("battery_percent"),
            power_plugged=sensor.get("power_plugged"),
            cpu_percent=sensor.get("cpu_percent"),
            ram_percent=sensor.get("ram_percent"),
            connected_devices=tuple(self._collect_devices(device_provider)),
            part_of_day=part_of_day(),
            recent_intents=intents,
            salient_memories=salient,
            last_action=last_action,
            last_action_ok=last_ok,
            last_reflection=reflection,
            sensorium_ts=float(sensor.get("timestamp") or 0.0),
            raw_sensorium=dict(sensor),
        )

    @staticmethod
    def _collect_sensorium() -> Dict[str, Any]:
        try:
            from core.sensorium import get_sensorium

            snapshot = get_sensorium().get_snapshot()
            return dict(snapshot) if isinstance(snapshot, dict) else {}
        except Exception as exc:
            _log(f"sensorium unavailable: {exc}")
            return {}

    @staticmethod
    def _collect_user_window() -> Dict[str, Any]:
        """Live read of the window the user is in, never one of Saturn's own."""
        try:
            from core.window_context import get_active_user_window

            info = get_active_user_window() or {}
            return dict(info) if isinstance(info, dict) else {}
        except Exception as exc:
            _log(f"user window unavailable: {exc}")
            return {}

    @staticmethod
    def _collect_foreground() -> Tuple[str, str, float]:
        try:
            from core.window_context import get_active_user_window

            info = get_active_user_window() or {}
            process = str(info.get("process_name") or "") or str(info.get("class_name") or "")
            return str(info.get("title") or ""), process, 0.0
        except Exception as exc:
            _log(f"window context unavailable: {exc}")
            return "", "", 0.0

    @staticmethod
    def _collect_devices(
        provider: Optional[Callable[[], List[Dict[str, Any]]]],
    ) -> List[Dict[str, Any]]:
        if provider is None:
            return []
        try:
            devices = provider() or []
        except Exception as exc:
            _log(f"device list unavailable: {exc}")
            return []
        return [d for d in devices if isinstance(d, dict)]

    # ── Writing ──────────────────────────────────────────────────────────

    def note_memory(self, context_block: str) -> None:
        """Record what recall produced, so other engines see the same memories."""
        text = (context_block or "").strip()
        with self._lock:
            self._salient = text
            self._cached = None
            self.stats["memories"] += 1

    def note_intent(self, intent: str) -> None:
        """Remember a dispatched intent so the next turn can see the trajectory."""
        clean = (intent or "").strip()
        if not clean:
            return
        with self._lock:
            if self._recent_intents and self._recent_intents[-1] == clean:
                return
            self._recent_intents.append(clean)
            self._cached = None

    def note_action(self, name: str, ok: Optional[bool] = None, diagnosis: str = "") -> None:
        """Record the last executed action and how it went."""
        clean = (name or "").strip()
        if not clean:
            return
        with self._lock:
            self._last_action = clean
            self._last_action_ok = ok
            self._last_reflection = (diagnosis or "").strip()
            self._cached = None
            self.stats["actions"] += 1

    def last_intent(self) -> str:
        """The most recently dispatched intent, without rebuilding the snapshot."""
        with self._lock:
            return self._recent_intents[-1] if self._recent_intents else ""

    def invalidate(self) -> None:
        """Force the next snapshot to re-read the machine."""
        with self._lock:
            self._cached = None


world_model = WorldModel.get_instance()
