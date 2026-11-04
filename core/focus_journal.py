"""Where the user's attention went, remembered instead of re-observed.

The passive sensorium already knows the focused window, its process and how long
the user has been there. It forgets all of that the moment the window changes.
This journal keeps the part worth keeping: a rolling, in-memory list of focus
segments - who had the screen, what it was called, and for how long.

Three deliberate limits:

* **No new polling.** The journal reads the sensorium's in-memory snapshot every
  ``SAMPLE_SECONDS``. It never touches Win32, never captures the screen and
  never runs OCR. Segment boundaries are exact rather than sample-granular
  because the sensorium reports the dwell time itself.
* **No disk.** Segments live in a bounded ring buffer for the life of the
  process, because window titles carry things people did not agree to have
  written down. Restarting the app starts a new journal.
* **No speaking.** Nothing here interrupts. The answer is available on request
  and stays silent otherwise.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

# The sensorium polls at 3 s; reading its fields twice that often keeps the
# record accurate without a second observer of the desktop.
SAMPLE_SECONDS = float(os.environ.get("SATURN_FOCUS_SAMPLE_SECONDS", "10"))
# Below this a segment is title flicker, not attention.
MIN_SEGMENT_SECONDS = float(os.environ.get("SATURN_FOCUS_MIN_SEGMENT", "5"))
# A user who has not touched the machine for this long is reading or gone, and
# the journal guesses gone: reading leaves no trace, and claiming attention that
# may not have happened is the worse error. Coming back to the same window
# within MERGE_GAP_SECONDS rejoins the segment below.
IDLE_SECONDS = float(os.environ.get("SATURN_FOCUS_IDLE_SECONDS", "180"))
# Twelve hours of a change every 90 seconds, then the oldest falls off.
MAX_SEGMENTS = int(os.environ.get("SATURN_FOCUS_MAX_SEGMENTS", "480"))
# The same window returning within this window of a segment is a continuation.
MERGE_GAP_SECONDS = 60.0

_TITLE_LIMIT = 70
_OWN_TITLES = ("saturn", "saturn echo", "saturn plus")

# Packaged builds, so a frozen executable recognises itself by name too.
_OWN_PROCESSES = ("saturn.exe", "saturn echo.exe", "saturnecho.exe",
                  "saturn plus.exe", "saturnplus.exe")


def _clean_title(title: str) -> str:
    """One line, no stray whitespace, cut at a word rather than mid-word."""
    text = " ".join(str(title or "").split())
    if len(text) <= _TITLE_LIMIT:
        return text
    clipped = text[:_TITLE_LIMIT]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped + " ..."


def _human(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(round(seconds))} sec"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{int(round(minutes))} min"
    hours = int(minutes // 60)
    rest = int(round(minutes - hours * 60))
    if rest == 0:
        return f"{hours} h"
    return f"{hours} h {rest} min"


def _window_label(minutes: float) -> str:
    if 59 <= minutes <= 60:
        return "the last hour"
    if 119 <= minutes <= 120:
        return "the last 2 hours"
    return f"the last {int(minutes)} minutes"


class FocusJournal:
    """A bounded, in-memory record of where the screen's attention has been."""

    def __init__(
        self,
        sensorium=None,
        sample_seconds: float = SAMPLE_SECONDS,
        max_segments: int = MAX_SEGMENTS,
    ):
        self._sensorium = sensorium
        self.sample_seconds = float(sample_seconds)
        self.max_segments = int(max_segments)

        self._segments: List[Dict[str, Any]] = []
        self._current: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

        # Observers, for anyone who wants to know that focus moved.
        self._change_callbacks: List[Callable[[Dict[str, Any]], None]] = []

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """Begin sampling. Returns False when there is nothing to sample."""
        with self._lock:
            if self._running:
                return True
            sensorium = self._resolve_sensorium()
            if sensorium is None or not getattr(sensorium, "_running", False):
                # A stopped sensorium has no live timestamps, so a journal fed
                # from it would file the last poll's minute over and over.
                return False
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, name="FocusJournal", daemon=True
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            if not self._running and self._thread is None:
                return
            self._stop_event.set()
            self._running = False
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sample()
            except Exception:
                # A journal is not worth crashing a session over.
                pass
            self._stop_event.wait(self.sample_seconds)

    def _resolve_sensorium(self):
        if self._sensorium is not None:
            return self._sensorium
        try:
            from core.sensorium import sensorium

            self._sensorium = sensorium
            return self._sensorium
        except Exception:
            return None

    def on_change(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._change_callbacks.append(callback)

    # ── Sampling ─────────────────────────────────────────────────────────────

    def sample(self, snapshot: Optional[Dict[str, Any]] = None) -> bool:
        """Record one observation. Returns True when focus was attributed.

        ``snapshot`` lets a caller (or a test) supply the observation instead of
        reading the sensorium, which keeps the attribution rules verifiable
        without a desktop.
        """
        if snapshot is None:
            sensorium = self._resolve_sensorium()
            if sensorium is None:
                return False
            if not getattr(sensorium, "_running", False):
                # Its fields would be frozen at whatever the last poll saw, so
                # recording them would invent segments that never happened.
                return False
            snapshot = sensorium.get_snapshot()

        now = float(snapshot.get("timestamp") or time.time())
        process = str(snapshot.get("process_name") or "Unknown")
        title = _clean_title(snapshot.get("window_title") or "")
        dwell = max(0.0, float(snapshot.get("dwell_seconds") or 0.0))
        idle = max(0.0, float(snapshot.get("user_idle_seconds") or 0.0))

        segment_start = now - dwell

        if idle >= IDLE_SECONDS:
            # Close at the moment of the last input rather than now: the
            # sensorium keeps counting dwell while nobody is there.
            self._close_open(now - idle, clamp_upper=True)
            return False

        if self._ignored(snapshot, process, title):
            # The user switched to Saturn itself; their work ended when this
            # window gained focus.
            self._close_open(segment_start, clamp_upper=True)
            return False

        with self._lock:
            current = self._current
            if current is not None and (current["app"], current["title"]) == (process, title):
                current["end"] = now
                return True

        # Either nothing is open or the window changed. Both need the lock
        # released first, because closing a segment takes it itself. The two
        # segments meet at the new window's start, so no time is counted twice
        # and none falls between them.
        self._close_open(segment_start)
        with self._lock:
            self._current = {"app": process, "title": title,
                             "start": segment_start, "end": now}

        for callback in list(self._change_callbacks):
            try:
                callback(self.current() or {})
            except Exception:
                pass
        return True

    def _close_open(self, boundary: float, clamp_upper: bool = False) -> None:
        """Finish the open segment at ``boundary``, never before it started.

        ``clamp_upper`` is for boundaries that are estimates of when attention
        stopped (a last keystroke, a window switch): they must never extend a
        segment past the last observation that supports it.
        """
        with self._lock:
            current = self._current
            if current is None:
                return
            end = max(float(current["start"]), float(boundary))
            if clamp_upper:
                end = min(end, float(current["end"]))
            current["end"] = end
            self._store(current)
            self._current = None

    def _store(self, segment: Dict[str, Any]) -> None:
        """Append a finished segment, merging a quick return to the same window."""
        duration = float(segment["end"]) - float(segment["start"])
        if duration < MIN_SEGMENT_SECONDS:
            return
        record = {
            "app": segment["app"],
            "title": segment["title"],
            "start": float(segment["start"]),
            "end": float(segment["end"]),
        }
        if self._segments:
            last = self._segments[-1]
            same_window = (last["app"], last["title"]) == (record["app"], record["title"])
            gap = record["start"] - last["end"]
            if same_window and 0.0 <= gap <= MERGE_GAP_SECONDS:
                # Alt-tabbing away and back is one visit, not two.
                last["end"] = max(last["end"], record["end"])
                return
        self._segments.append(record)
        if len(self._segments) > self.max_segments:
            del self._segments[: len(self._segments) - self.max_segments]

    def _ignored(self, snapshot: Dict[str, Any], process: str, title: str) -> bool:
        """Saturn looking at its own window is not the user's context."""
        pid = snapshot.get("pid")
        try:
            if pid and int(pid) == os.getpid():
                return True
        except (TypeError, ValueError):
            pass
        if process.lower() in _OWN_PROCESSES:
            return True
        folded = title.lower()
        return any(folded == name or folded.startswith(name + " ")
                   or folded.startswith(name + " -") for name in _OWN_TITLES)

    # ── Reading ──────────────────────────────────────────────────────────────

    def current(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            current = dict(self._current) if self._current else None
        if current is None:
            return None
        current["seconds"] = max(0.0, current["end"] - current["start"])
        return current

    def segments(self, minutes: float = 60.0) -> List[Dict[str, Any]]:
        """Finished segments inside the window, oldest first, plus the open one."""
        since = time.time() - max(1.0, float(minutes)) * 60.0
        with self._lock:
            finished = [dict(item) for item in self._segments if item["end"] >= since]
            current = dict(self._current) if self._current else None
        if current is not None and current["end"] >= since:
            finished.append(current)
        for item in finished:
            item["seconds"] = max(0.0, item["end"] - item["start"])
        return finished

    def recent(self, minutes: float = 60.0, limit: int = 10) -> List[Dict[str, Any]]:
        """The newest segments first, each with its duration in seconds."""
        items = self.segments(minutes)
        items.sort(key=lambda item: item["end"], reverse=True)
        return items[: max(1, int(limit))]

    def summary(self, minutes: float = 60.0, limit: int = 5) -> str:
        """A short spoken-ready account of where the time went."""
        items = self.segments(minutes)
        if not items:
            if not self._running:
                return ("I'm not tracking focus right now, so I can't tell you "
                        "where the time went.")
            return (f"I don't have anything recorded for {_window_label(minutes)} "
                    "yet. The journal starts when Saturn starts.")

        totals: Dict[tuple, float] = {}
        for item in items:
            key = (item["app"], item["title"])
            totals[key] = totals.get(key, 0.0) + item["seconds"]
        ordered = sorted(totals.items(), key=lambda pair: pair[1], reverse=True)

        tracked = sum(totals.values())
        lines = [f"{_window_label(minutes).capitalize()}: {_human(tracked)} tracked "
                 f"across {len(totals)} window{'' if len(totals) == 1 else 's'}."]
        for (app, title), seconds in ordered[: max(1, int(limit))]:
            where = f"{app} - {title}" if title else app
            lines.append(f"  {_human(seconds):>9}  {where}")
        return "\n".join(lines)


_journal: Optional[FocusJournal] = None


def get_focus_journal() -> FocusJournal:
    global _journal
    if _journal is None:
        _journal = FocusJournal()
    return _journal
