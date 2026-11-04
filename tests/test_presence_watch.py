"""The presence loop: presence, absence, fault and "not my turn" are four
different facts, and only one of them may ever reach a lock decision."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from core import presence_watch
from core.camera_hub import CameraBusy


class _Frame:
    size = 12

    def __init__(self, clean: bool = True):
        self.clean = clean

    def std(self) -> float:
        return 20.0 if self.clean else 0.0


class _Lease:
    def __init__(self, frame, ok=True):
        self._frame = frame
        self._ok = ok

    def read(self):
        return self._ok, self._frame if self._ok else None


class FakeHub:
    def __init__(self, frame=None, ok=True, yield_now=False, busy=False):
        self.frame = _Frame() if frame is None else frame
        self.ok = ok
        self.yield_now = yield_now
        self.busy = busy
        self.releases = 0
        self.leases = 0

    def should_yield(self) -> bool:
        return self.yield_now

    @contextmanager
    def lease(self, owner="presence", size=None, timeout=None):
        if self.busy:
            raise CameraBusy("in use")
        self.leases += 1
        yield _Lease(self.frame, self.ok)

    def release(self, force=False):
        self.releases += 1

    def release_idle(self):
        pass

    def health(self):
        return {"ok": self.ok, "reason": "ok" if self.ok else "NO_FRAME"}


def _watcher(hub=None, faces=None, identify=None, owner_name="Ada"):
    watcher = presence_watch.PresenceWatcher(
        hub=hub or FakeHub(),
        interval=0.5,
        identify=identify,
        owner_name=owner_name,
    )
    found = {"faces": faces if faces is not None else [{}]}
    watcher._detect = lambda frame: list(found["faces"])
    watcher.faces = found
    return watcher


@pytest.fixture(autouse=True)
def _unlocked(monkeypatch):
    monkeypatch.setattr(presence_watch, "session_locked", lambda: False)


def test_a_face_makes_the_desk_present_and_the_first_sighting_is_an_arrival():
    watcher = _watcher(faces=[{}], identify=lambda frames: {"name": None, "reason": "X"})

    first = watcher.sample_once(now=100.0)
    assert first["presence"] is True
    assert first["arrival"] is True
    assert first["state"] == presence_watch.PRESENT_UNVERIFIED

    second = watcher.sample_once(now=103.0)
    assert second["arrival"] is False
    assert second["presence"] is True


def test_no_face_is_absence_and_not_a_fault():
    watcher = _watcher(faces=[])
    sample = watcher.sample_once(now=100.0)

    assert sample["state"] == presence_watch.ABSENT
    assert sample["faulted"] is False
    assert sample["presence"] is False


def test_a_yielded_tick_is_skipped_without_touching_the_state():
    hub = FakeHub(yield_now=True)
    watcher = _watcher(hub=hub, faces=[{}])

    sample = watcher.sample_once(now=100.0)

    assert sample.get("skipped") is True
    assert sample["samples"] == 0
    assert hub.leases == 0
    assert watcher.snapshot()["state"] == presence_watch.STARTING


def test_a_null_frame_is_a_fault_and_releases_the_camera():
    hub = FakeHub(ok=False)
    watcher = _watcher(hub=hub)

    sample = watcher.sample_once(now=100.0)

    assert sample["state"] == presence_watch.FAULT
    assert sample["faulted"] is True
    assert hub.releases >= 1


def test_a_black_frame_is_a_covered_lens_not_an_empty_room():
    watcher = _watcher(hub=FakeHub(frame=_Frame(clean=False)))

    sample = watcher.sample_once(now=100.0)

    assert sample["state"] == presence_watch.FAULT
    assert "LENS_COVERED" in sample["reason"]


def test_a_fault_is_retried_on_a_slower_clock():
    hub = FakeHub(ok=False)
    watcher = _watcher(hub=hub)

    watcher.sample_once(now=100.0)
    leases_after_fault = hub.leases

    # One second later: skipped, and the camera is not touched again.
    again = watcher.sample_once(now=101.0)
    assert again.get("skipped") is True
    assert hub.leases == leases_after_fault

    # Past the retry window: it tries again.
    later = watcher.sample_once(now=100.0 + presence_watch.FAULT_RETRY_SECONDS + 1)
    assert hub.leases > leases_after_fault
    assert later["faulted"] is True


def test_a_locked_session_pauses_the_camera(monkeypatch):
    monkeypatch.setattr(presence_watch, "session_locked", lambda: True)
    hub = FakeHub()
    watcher = _watcher(hub=hub, faces=[{}])
    watcher._running = True

    sample = watcher.sample_once(now=100.0)

    assert sample["state"] == presence_watch.LOCKED
    assert sample["locked"] is True
    assert hub.leases == 0
    assert hub.releases >= 1


def test_a_busy_camera_is_skipped_not_faulted():
    watcher = _watcher(hub=FakeHub(busy=True), faces=[{}])

    sample = watcher.sample_once(now=100.0)

    assert sample.get("skipped") is True
    assert sample.get("faulted") is not True
    assert watcher.snapshot()["state"] == presence_watch.STARTING


def test_identity_is_asked_once_per_arrival():
    calls = []

    def identify(frames):
        calls.append(1)
        return {"name": "Ada"}

    watcher = _watcher(faces=[{}], identify=identify)

    watcher.sample_once(now=100.0)
    watcher.sample_once(now=103.0)
    watcher.sample_once(now=106.0)
    assert len(calls) == 1

    # Desk empties, then someone arrives again: a second question.
    watcher.faces["faces"] = []
    watcher.sample_once(now=109.0)
    watcher.faces["faces"] = [{}]
    watcher.sample_once(now=112.0)
    assert len(calls) == 2


def test_the_configured_owner_is_recognised_as_the_owner():
    watcher = _watcher(faces=[{}], identify=lambda frames: {"name": "Ada"}, owner_name="Ada")
    sample = watcher.sample_once(now=100.0)

    assert sample["state"] == presence_watch.PRESENT_OWNER


def test_another_enrolled_person_is_known_but_is_not_the_owner():
    watcher = _watcher(faces=[{}], identify=lambda frames: {"name": "Sam"}, owner_name="Ada")
    sample = watcher.sample_once(now=100.0)

    assert sample["state"] == presence_watch.PRESENT_KNOWN


def test_an_unreadable_face_is_unverified():
    watcher = _watcher(
        faces=[{}],
        identify=lambda frames: {"name": None, "reason": "IDENTITY_UNAVAILABLE"},
    )
    sample = watcher.sample_once(now=100.0)

    assert sample["state"] == presence_watch.PRESENT_UNVERIFIED
    assert sample["identity"]["reason"] == "IDENTITY_UNAVAILABLE"


def test_a_stuck_identifier_times_out_instead_of_hanging_the_loop(monkeypatch):
    monkeypatch.setattr(presence_watch, "INFERENCE_TIMEOUT", 0.05)

    def slow(frames):
        import time as _time

        _time.sleep(0.5)
        return {"name": "Ada"}

    watcher = _watcher(faces=[{}], identify=slow)
    sample = watcher.sample_once(now=100.0)

    assert sample["identity"]["timed_out"] is True
    assert sample["identity"]["reason"] == "INFERENCE_TIMEOUT"
    assert sample["state"] == presence_watch.PRESENT_UNVERIFIED
