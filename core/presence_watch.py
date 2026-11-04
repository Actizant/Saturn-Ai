"""The low-power eye: one cheap frame every few seconds, on the CPU.

WHAT THIS DOES NOT DO
    No neural network, no GPU, no VRAM. The Haar cascade that ships inside
    ``core/`` answers "is there a face in this frame" in a couple of
    milliseconds at 320x240, which is all a presence loop needs. Identity - the
    "whose face" question - is asked **once per arrival**, not on every tick,
    because that is the expensive part and it is also the only moment the answer
    changes anything.

WHAT IT REFUSES TO CONFUSE
    Absence and fault are different facts and they lead to opposite actions:

    * ``ABSENT``  - the camera works, nobody is in front of it.
    * ``FAULT``   - the camera does not work: unplugged, held by another app, or
      the lens is covered. A security system that reads a dead sensor as "empty
      room" is worse than none, so this state hard-disables the lock.
    * ``LOCKED``  - the Windows session is locked. Monitoring stops and the
      device is released; there is nothing to protect and the LED should be off.

    A tick that is skipped because another feature borrowed the camera is
    neither of these. It is not evidence about the world, so it is not counted.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from typing import Callable, Optional

from core.camera_hub import CameraBusy, get_camera_hub

SAMPLE_SECONDS = float(os.environ.get("SATURN_PRESENCE_INTERVAL", "3.0"))
FRAME_SIZE = (320, 240)

# A decision about a person must survive five seconds of thinking. Beyond that
# the machine is not slow, it is stuck, and the safe answer is "cannot verify".
INFERENCE_TIMEOUT = 5.0

# After a fault, look again this often. Frequent enough to notice the camera was
# plugged back in, rare enough not to hammer a broken driver.
FAULT_RETRY_SECONDS = 30.0

# Three frames of pure black in a row is a covered lens or a dead sensor, not a
# dark room.
MIN_FRAME_STD = 0.5

STARTING = "STARTING"
FAULT = "FAULT"
ABSENT = "ABSENT"
LOCKED = "LOCKED"
PRESENT_UNVERIFIED = "PRESENT_UNVERIFIED"
PRESENT_OWNER = "PRESENT_OWNER"
PRESENT_KNOWN = "PRESENT_KNOWN"

_DESKTOP_SWITCHDESKTOP = 0x0100


def session_locked() -> bool:
    """True when the Windows desktop is secured. Always False elsewhere.

    ``OpenInputDesktop`` fails exactly when the input desktop is not ours, which
    is what a lock screen does. If it fails for another reason we simply stop
    sampling - the safe direction, because it can never cause a lockout.
    """
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if handle:
            user32.CloseDesktop(handle)
            return False
        return True
    except Exception:
        return False


class PresenceWatcher:
    def __init__(self, hub=None, interval: float = SAMPLE_SECONDS,
                 camera: int = 0, size=FRAME_SIZE,
                 identify: Optional[Callable[[list], dict]] = None,
                 owner_name: str = ""):
        self.hub = hub or get_camera_hub()
        self.interval = max(0.5, float(interval))
        self.camera = int(camera)
        self.size = size
        self._identify = identify
        self._on_sample: Optional[Callable[[dict], None]] = None
        # Who counts as the owner. Set by the perimeter from its own settings;
        # the sensor deliberately does not read configuration on every tick.
        self.owner_name = str(owner_name or "")

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._state = STARTING
        self._since = time.time()
        self._reason = "starting"
        self._face_count = 0
        self._samples = 0
        self._skipped = 0
        self._presence = False
        self._identity: dict = {}
        self._last_fault_probe = 0.0
        self._previous_presence = False

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return True
            self._running = True
            self._stop.clear()
            self._state = STARTING
            self._since = time.time()
            self._thread = threading.Thread(target=self._loop, name="SaturnPresence",
                                            daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        try:
            self.hub.release(force=True)
        except Exception:
            pass

    def set_callback(self, callback: Optional[Callable[[dict], None]]) -> None:
        """Called on every evaluated sample, from this thread."""
        self._on_sample = callback

    @property
    def running(self) -> bool:
        return self._running

    # ── The loop ────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self.sample_once()
                if sample.get("skipped"):
                    self._stop.wait(0.5)
                    continue
                callback = self._on_sample
                if callback is not None:
                    try:
                        callback(sample)
                    except Exception:
                        # A policy error must not kill the sensor loop.
                        pass
                self.hub.release_idle()
            except Exception as exc:
                with self._lock:
                    self._set_state_locked(FAULT, f"loop error: {exc}")
            self._stop.wait(self.interval)

    def _set_state_locked(self, state: str, reason: str) -> None:
        if state != self._state:
            self._state = state
            self._since = time.time()
        self._reason = reason

    def _emit(self, **extra) -> dict:
        sample = self.snapshot()
        sample.update(extra)
        return sample

    # ── One sample ──────────────────────────────────────────────────────

    def sample_once(self, now: Optional[float] = None) -> dict:
        """Take one reading. Returns the sample the policy layer will judge."""
        moment = time.time() if now is None else float(now)

        if self.hub.should_yield():
            with self._lock:
                self._skipped += 1
            return self._emit(skipped=True, reason="camera yielded")

        if session_locked():
            with self._lock:
                if self._state != LOCKED:
                    self._set_state_locked(LOCKED, "session locked")
                self._presence = False
                self._face_count = 0
                # The next face seen after an unlock is an arrival, so it is
                # verified rather than inheriting the old verdict.
                self._previous_presence = False
            try:
                self.hub.release(force=True)
            except Exception:
                pass
            return self._emit(skipped=True, reason="session locked")

        # A faulted camera is retried on a slower clock, and never on the
        # strength of a single frame.
        if self._state == FAULT and (moment - self._last_fault_probe) < FAULT_RETRY_SECONDS:
            with self._lock:
                self._skipped += 1
            return self._emit(skipped=True, reason="camera fault, waiting to retry")

        frame, error = self._grab()
        if frame is None:
            if error.startswith("CAMERA_BUSY"):
                # Another part of Saturn had the device for this tick. That is
                # not a fact about the room, so it is skipped rather than
                # recorded as an absent desk or a fault.
                with self._lock:
                    self._skipped += 1
                return self._emit(skipped=True, reason="camera busy")
            with self._lock:
                self._last_fault_probe = moment
                self._samples += 1
                self._set_state_locked(FAULT, error)
                self._presence = False
                self._face_count = 0
                self._previous_presence = False
            try:
                self.hub.release(force=True)
            except Exception:
                pass
            return self._emit(fault=True, reason=error)

        faces = self._detect(frame)
        present = bool(faces)
        arrival = present and not self._previous_presence
        identity = self._identity_for(frame) if arrival else self._identity

        with self._lock:
            self._samples += 1
            self._presence = present
            self._previous_presence = present
            self._face_count = len(faces)
            self._identity = identity or {}
            if not present:
                self._set_state_locked(ABSENT, "no face in frame")
            elif arrival:
                name = (identity or {}).get("name")
                if name and self.owner_name and name.lower() == self.owner_name.lower():
                    self._set_state_locked(PRESENT_OWNER, f"owner ({name})")
                elif name:
                    self._set_state_locked(PRESENT_KNOWN, f"known person ({name})")
                else:
                    self._set_state_locked(PRESENT_UNVERIFIED,
                                           (identity or {}).get("reason") or "unverified face")
            # While present, keep the earlier verdict: an identity check taken
            # six seconds ago is still the truth about who is sitting there.

        return self._emit(arrival=arrival)

    def _grab(self):
        """One usable frame, or (None, reason)."""
        try:
            with self.hub.lease("presence", size=self.size) as lease:
                frame = None
                for _ in range(2):
                    ok, candidate = lease.read()
                    if ok and candidate is not None and getattr(candidate, "size", 0):
                        frame = candidate
                if frame is None:
                    return None, "NO_FRAME"
        except CameraBusy as exc:
            return None, f"CAMERA_BUSY: {exc}"
        except Exception as exc:
            return None, f"CAPTURE_FAILED: {exc}"

        try:
            if float(frame.std()) < MIN_FRAME_STD:
                return None, "LENS_COVERED"
        except Exception:
            pass
        return frame, "ok"

    def _detect(self, frame) -> list:
        try:
            from core import face_id

            return face_id.detect(frame)
        except Exception:
            return []

    def _identity_for(self, frame) -> dict:
        """Who this is. Once per arrival, bounded by ``INFERENCE_TIMEOUT``.

        Whichever identifier is in use - the real model or an injected one -
        runs on a worker thread, so a hung inference costs one unanswered
        question instead of a frozen sensor loop.
        """
        if self._identify is not None:
            def run():
                return self._identify([frame])
        else:
            try:
                from core import face_id
            except Exception as exc:
                return {"name": None, "reason": f"FACE_MODULE_UNAVAILABLE: {exc}"}

            info = face_id.available()
            if not info.get("identity"):
                return {"name": None, "reason": "IDENTITY_UNAVAILABLE",
                        "message": info.get("hint")}

            def run():
                return face_id.identify([frame])

        box: dict = {}

        def worker():
            try:
                box.update(dict(run() or {}))
            except Exception as exc:
                box.update({"name": None, "reason": f"IDENTIFY_FAILED: {exc}"})

        thread = threading.Thread(target=worker, name="SaturnIdentify", daemon=True)
        thread.start()
        thread.join(INFERENCE_TIMEOUT)
        if thread.is_alive():
            # A stuck encoder must not freeze the sensor loop, and must not be
            # read as a negative identification either.
            return {"name": None, "reason": "INFERENCE_TIMEOUT", "timed_out": True}
        return box

    # ── Reading the sensor ──────────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            state = self._state
            return {
                "state": state,
                "presence": self._presence,
                "face_count": self._face_count,
                "since": round(self._since, 3),
                "seconds_in_state": round(time.time() - self._since, 1),
                "samples": self._samples,
                "skipped": self._skipped,
                "reason": self._reason,
                "identity": dict(self._identity or {}),
                "identity_available": self._identity_available(),
                "locked": state == LOCKED,
                "faulted": state == FAULT,
                "at": time.time(),
                "hub": self.hub.health(),
            }

    def _identity_available(self) -> bool:
        try:
            from core import face_id

            return bool(face_id.available().get("identity"))
        except Exception:
            return False


_watcher: Optional[PresenceWatcher] = None
_watcher_lock = threading.Lock()


def get_presence_watcher(**kwargs) -> PresenceWatcher:
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            _watcher = PresenceWatcher(**kwargs)
        return _watcher
