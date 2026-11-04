"""One owner for the webcam, so two features never fight over the device.

WHY THIS EXISTS
    Five parts of Saturn already grab the camera directly: the study guard, the
    pushup and calorie counters, the screen processor and the HUD preview. All of
    them work today. A single always-on presence loop added beside them would
    collide with whichever one happens to be running, and on Windows the loser
    does not get an error - it gets black frames, which reads as "nobody is at
    the desk" to a security module.

    So this is a cooperative hub rather than a rewrite: new code leases the
    device here, and the hub *yields* to the existing consumers instead of
    stealing from them. Nothing that works today is modified.

THE TWO RULES
    * A lease is exclusive. A second caller waits, then raises ``CameraBusy``.
      It never silently steals frames from the first.
    * While a lease is idle the device stays open for a while, because opening a
      DirectShow camera costs about a second and the presence loop samples every
      three. It is released once nobody has used it for ``idle_seconds`` - the
      LED going out is a feature.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

DEFAULT_SIZE = (320, 240)
OPEN_TIMEOUT = 5.0

# A frame whose pixels barely vary is a covered lens or a dead sensor, not a
# dark room: even a night-lit room clears this comfortably.
MIN_FRAME_STD = 0.5

# A camera takes a moment to deliver a usable picture after opening; probing
# throws these away before judging anything.
PROBE_WARMUP = 5


class CameraBusy(RuntimeError):
    """The device is leased by someone else and did not free up in time."""


# The HUD's hand-tracking preview opens the same device directly, outside this
# hub, and it is started by a click. It reports itself here from the GUI thread
# so the presence loop can step aside - the alternative is that whichever side
# opens second looks broken to the user.
_ui_holding = threading.Event()


def set_ui_camera_active(active: bool) -> None:
    """Called by the gesture preview when it starts and stops the camera."""
    if active:
        _ui_holding.set()
    else:
        _ui_holding.clear()


def ui_camera_active() -> bool:
    return _ui_holding.is_set()


def _cv2():
    try:
        import cv2

        return cv2
    except Exception:
        return None


def _silence_opencv() -> None:
    """OpenCV prints to stderr when a device is missing; that is noise here."""
    cv2 = _cv2()
    if cv2 is None:
        return
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass


class _Lease:
    """What a caller actually touches: a capture handle plus health reporting."""

    def __init__(self, hub: "CameraHub", capture, owner: str):
        self._hub = hub
        self._capture = capture
        self.owner = owner

    def read(self):
        """Read one frame, recording whether it was any good."""
        try:
            ok, frame = self._capture.read()
        except Exception:
            ok, frame = False, None
        self._hub._note_frame(ok, frame)
        return ok, frame

    def isOpened(self) -> bool:
        try:
            return bool(self._capture.isOpened())
        except Exception:
            return False


class CameraHub:
    def __init__(self, index: Optional[int] = None, idle_seconds: float = 60.0):
        self.index = index
        self.idle_seconds = float(idle_seconds)
        self._lock = threading.Lock()          # held for the duration of a lease
        self._state_lock = threading.Lock()    # short, for the fields below
        self._capture = None
        self._size = None
        self._owner: Optional[str] = None
        self._last_use = 0.0
        self._last_frame_at = 0.0
        self._consecutive_nulls = 0
        self._reason = "idle"
        self._opened_index: Optional[int] = None
        self._externals: list[Callable[[], bool]] = []
        self._suspended = False

    # ── External consumers ──────────────────────────────────────────────

    def register_external(self, check: Callable[[], bool]) -> None:
        """Register "someone else needs the device right now" as a callable."""
        self._externals.append(check)

    def _external_active(self) -> bool:
        if ui_camera_active():
            return True
        for check in list(self._externals):
            try:
                if check():
                    return True
            except Exception:
                continue
        return False

    def should_yield(self) -> bool:
        """True when the presence loop must skip this tick rather than fault.

        A skipped tick is not evidence about the world, so the loop must not
        count it as an absence - that is the difference between a secure system
        and one that locks its owner out because a study session took the camera.
        """
        if self._suspended:
            return True
        return self._external_active()

    # ── Health ──────────────────────────────────────────────────────────

    def _note_frame(self, ok: bool, frame) -> None:
        if ok and frame is not None and getattr(frame, "size", 0):
            self._consecutive_nulls = 0
            self._last_frame_at = time.time()
            self._reason = "ok"
            return
        self._consecutive_nulls += 1
        if self._consecutive_nulls >= 3:
            self._reason = "NO_FRAME"

    def health(self) -> dict:
        with self._state_lock:
            age = time.time() - self._last_frame_at if self._last_frame_at else None
            return {
                "ok": self._reason == "ok" and self._consecutive_nulls == 0,
                "reason": self._reason,
                "index": self._opened_index,
                "owner": self._owner,
                "size": self._size,
                "consecutive_nulls": self._consecutive_nulls,
                "frame_age_seconds": None if age is None else round(age, 2),
                "external_active": self._external_active(),
                "suspended": self._suspended,
            }

    def suspend(self) -> None:
        """Stop the presence loop from touching the device at all."""
        self._suspended = True
        self.release(force=True)

    def resume(self) -> None:
        self._suspended = False

    # ── Opening and closing ─────────────────────────────────────────────

    def _candidates(self) -> list[int]:
        if self.index is not None:
            return [int(self.index)]
        return [0, 1]

    def _open(self, size):
        """Open the first device that answers, or return (None, reason)."""
        cv2 = _cv2()
        if cv2 is None:
            return None, "OPENCV_UNAVAILABLE"
        _silence_opencv()
        width, height = size
        last = "CAMERA_UNAVAILABLE"
        for idx in self._candidates():
            capture = None
            try:
                if hasattr(cv2, "CAP_DSHOW"):
                    capture = cv2.VideoCapture(int(idx), cv2.CAP_DSHOW)
                if capture is None or not capture.isOpened():
                    try:
                        if capture is not None:
                            capture.release()
                    except Exception:
                        pass
                    capture = cv2.VideoCapture(int(idx))
                if capture is None or not capture.isOpened():
                    last = "CAMERA_UNAVAILABLE"
                    continue
                try:
                    capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
                    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
                    # A small buffer keeps a sample from returning a frame that
                    # is already seconds old and describing a stale room.
                    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass
                # One real frame proves the device works, not just that it
                # opened - Windows will happily open a disconnected camera.
                ok, frame = capture.read()
                if not ok or frame is None or not getattr(frame, "size", 0):
                    last = "NO_FRAME"
                    try:
                        capture.release()
                    except Exception:
                        pass
                    continue
                self._opened_index = int(idx)
                return capture, "ok"
            except Exception as exc:
                last = f"OPEN_FAILED: {exc}"
                try:
                    if capture is not None:
                        capture.release()
                except Exception:
                    pass
        return None, last

    def _close_locked(self) -> None:
        """Close the device. Caller must hold ``_state_lock``."""
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:
                pass
        self._capture = None
        self._size = None
        self._owner = None

    def release(self, force: bool = False) -> None:
        """Close the device if nobody is using it.

        ``force`` closes it anyway, which is what a lockdown or a sleep event
        needs: the LED must go out immediately, not sixty seconds later.
        """
        if not force and self._lock.locked():
            return
        with self._state_lock:
            self._close_locked()
            self._reason = "idle"

    def release_idle(self) -> None:
        """Close the device once it has gone unused long enough to be idle."""
        if self._lock.locked():
            return
        if time.time() - self._last_use < self.idle_seconds:
            return
        with self._state_lock:
            self._close_locked()
            self._reason = "idle"

    # ── Leasing ─────────────────────────────────────────────────────────

    @contextmanager
    def lease(self, owner: str = "caller", size=DEFAULT_SIZE, timeout: float = OPEN_TIMEOUT):
        """Exclusive use of the device, reopening only when the size changes.

        Raises ``CameraBusy`` rather than blocking forever, so a security loop
        that loses the race skips a tick instead of hanging the thread.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        acquired = self._lock.acquire(timeout=max(0.01, deadline - time.monotonic()))
        if not acquired:
            with self._state_lock:
                self._reason = "BUSY"
            raise CameraBusy("The camera is in use by another part of Saturn.")
        try:
            with self._state_lock:
                wanted = (int(size[0]), int(size[1])) if size else None
                if self._capture is not None and wanted and self._size != wanted:
                    self._close_locked()
                if self._capture is not None and not self._usable_locked():
                    self._close_locked()
                if self._capture is None:
                    capture, reason = self._open(wanted or DEFAULT_SIZE)
                    if capture is None:
                        self._reason = reason
                        raise CameraBusy(reason)
                    self._capture = capture
                    self._size = wanted or DEFAULT_SIZE
                    self._consecutive_nulls = 0
                    self._last_frame_at = time.time()
                self._owner = owner
                self._reason = "ok"
                handle = _Lease(self, self._capture, owner)
            yield handle
        finally:
            self._last_use = time.time()
            try:
                self._lock.release()
            except Exception:
                pass

    def _usable_locked(self) -> bool:
        try:
            if self._capture is None or not self._capture.isOpened():
                return False
            ok, frame = self._capture.read()
            return bool(ok and frame is not None and getattr(frame, "size", 0))
        except Exception:
            return False

    # ── Probing ─────────────────────────────────────────────────────────

    def probe(self, reset: bool = False) -> dict:
        """Test the hardware once, honestly, without a lease.

        Used by the perimeter's self-test before it is allowed to arm: a lock
        decision made on a camera that cannot see is worse than no lock at all.

        Every path returns the same shape - ``health()`` plus ``healthy`` and
        ``std`` - so a caller never has to guess which failure it got.
        """
        cv2 = _cv2()
        if cv2 is None:
            with self._state_lock:
                self._reason = "OPENCV_UNAVAILABLE"
            return self._probe_result(False, None)

        capture, reason = self._open(DEFAULT_SIZE)
        if capture is None:
            with self._state_lock:
                self._reason = reason
                self._consecutive_nulls = 3
            return self._probe_result(False, None)

        std_value = None
        try:
            frame = None
            for _ in range(PROBE_WARMUP):
                ok, candidate = capture.read()
                if ok and candidate is not None and getattr(candidate, "size", 0):
                    frame = candidate
            if frame is not None:
                try:
                    std_value = float(frame.std())
                except Exception:
                    std_value = None
            if frame is None:
                reason = "NO_FRAME"
            elif std_value is not None and std_value < MIN_FRAME_STD:
                reason = "LENS_COVERED"
            else:
                reason = "ok"
        finally:
            try:
                capture.release()
            except Exception:
                pass

        with self._state_lock:
            if reason == "ok":
                self._reason = "ok"
                self._consecutive_nulls = 0
                self._last_frame_at = time.time()
            else:
                self._reason = reason
                self._consecutive_nulls = 3
        return self._probe_result(reason == "ok", std_value)

    def _probe_result(self, healthy: bool, std_value) -> dict:
        info = self.health()
        info["healthy"] = bool(healthy)
        info["std"] = None if std_value is None else round(std_value, 3)
        return info


_hub: Optional[CameraHub] = None
_hub_lock = threading.Lock()


def get_camera_hub() -> CameraHub:
    """The process-wide hub. Registers the study guard on first use."""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = CameraHub()
            try:
                from core.study_guard import StudyGuardManager

                _hub.register_external(lambda: bool(StudyGuardManager.get_instance().is_active))
            except Exception:
                pass
        return _hub
