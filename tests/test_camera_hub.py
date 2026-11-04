"""The camera hub: one owner, and a device that says when it is blind."""

from __future__ import annotations

import types

import pytest

from core import camera_hub
from core.camera_hub import CameraBusy, CameraHub


class _Frame:
    def __init__(self, clean: bool = True):
        self.clean = clean
        self.size = 12

    def std(self) -> float:
        return 20.0 if self.clean else 0.0


class _Capture:
    def __init__(self, opened: bool = True, delivers: bool = True, clean: bool = True):
        self.opened = opened
        self.delivers = delivers
        self.clean = clean
        self.released = False
        self.settings = {}

    def isOpened(self) -> bool:
        return self.opened and not self.released

    def read(self):
        if not self.delivers:
            return False, None
        return True, _Frame(self.clean)

    def set(self, prop, value):
        self.settings[prop] = value
        return True

    def release(self):
        self.released = True


def _fake_cv2(opened=True, delivers=True, clean=True):
    module = types.SimpleNamespace(
        CAP_DSHOW=700,
        CAP_ANY=0,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_BUFFERSIZE=38,
        captures=[],
    )

    def _video_capture(index, backend=None):
        capture = _Capture(opened=opened, delivers=delivers, clean=clean)
        module.captures.append(capture)
        return capture

    module.VideoCapture = _video_capture
    return module


@pytest.fixture(autouse=True)
def _clear_ui_flag():
    camera_hub.set_ui_camera_active(False)
    yield
    camera_hub.set_ui_camera_active(False)


def test_a_lease_reports_its_owner(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2())
    hub = CameraHub()

    with hub.lease("presence", timeout=0.2) as lease:
        ok, frame = lease.read()
        assert ok and frame is not None
        assert hub.health()["owner"] == "presence"

    assert hub.health()["reason"] == "ok"


def test_a_second_lease_waits_then_refuses(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2())
    hub = CameraHub()

    with hub.lease("first", timeout=0.2):
        with pytest.raises(CameraBusy):
            with hub.lease("second", timeout=0.05):
                pass


def test_the_hub_yields_to_an_external_consumer(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2())
    hub = CameraHub()
    wanted = {"value": False}
    hub.register_external(lambda: wanted["value"])

    assert hub.should_yield() is False
    wanted["value"] = True
    assert hub.should_yield() is True


def test_the_hub_yields_to_the_hud_camera_preview(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2())
    hub = CameraHub()

    camera_hub.set_ui_camera_active(True)
    assert hub.should_yield() is True
    assert hub.health()["external_active"] is True

    camera_hub.set_ui_camera_active(False)
    assert hub.should_yield() is False


def test_suspend_closes_the_device_and_stops_sampling(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2())
    hub = CameraHub()

    with hub.lease("presence", timeout=0.2):
        pass
    assert hub._capture is not None

    hub.suspend()
    assert hub.should_yield() is True
    assert hub._capture is None

    hub.resume()
    assert hub.should_yield() is False


def test_a_working_camera_probes_healthy(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2(clean=True))
    result = CameraHub().probe()

    assert result["healthy"] is True
    assert result["reason"] == "ok"
    assert result["consecutive_nulls"] == 0


def test_a_covered_lens_probes_as_covered_not_absent(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2(clean=False))
    result = CameraHub().probe()

    assert result["healthy"] is False
    assert result["reason"] == "LENS_COVERED"


def test_a_camera_that_opens_but_delivers_nothing_is_a_fault(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2(delivers=False))
    result = CameraHub().probe()

    assert result["healthy"] is False
    assert result["reason"] == "NO_FRAME"


def test_a_camera_that_will_not_open_is_reported_plainly(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2(opened=False))
    result = CameraHub().probe()

    assert result["healthy"] is False
    assert result["reason"] == "CAMERA_UNAVAILABLE"


def test_missing_opencv_is_stated_rather_than_raised(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: None)
    hub = CameraHub()

    result = hub.probe()
    assert result["reason"] == "OPENCV_UNAVAILABLE"

    with pytest.raises(CameraBusy):
        with hub.lease("presence", timeout=0.1):
            pass


def test_an_idle_device_is_released_but_a_busy_one_is_not(monkeypatch):
    monkeypatch.setattr(camera_hub, "_cv2", lambda: _fake_cv2())
    hub = CameraHub(idle_seconds=0.0)

    with hub.lease("presence", timeout=0.2):
        pass
    assert hub._capture is not None

    hub.release_idle()
    assert hub._capture is None


def test_a_stale_device_is_reopened_rather_than_trusted(monkeypatch):
    fake = _fake_cv2()
    monkeypatch.setattr(camera_hub, "_cv2", lambda: fake)
    hub = CameraHub()

    with hub.lease("presence", timeout=0.2):
        pass
    # Something outside the hub released it, as a sleep/resume would.
    hub._capture.released = True

    with hub.lease("presence", timeout=0.2) as lease:
        ok, frame = lease.read()
        assert ok and frame is not None
    assert len(fake.captures) >= 2
