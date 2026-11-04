"""Starting the perimeter at boot, and the settings toggle that asks for it.

The bug these tests exist for: ``armed`` is a setting and therefore survives a
restart, while the sampler is a thread and therefore does not. Nothing built the
perimeter except a tool call, so an armed perimeter came back from a restart
standing at a green light over a camera nobody was reading. Every test below is
about the line between "start it because it is armed" and "do not quietly turn
the camera on for someone who never asked".
"""

from __future__ import annotations

import pytest

from actions import security_tools
from core import security_perimeter
from core.security_perimeter import DEFAULTS, SecurityPerimeter


# ── Doubles ────────────────────────────────────────────────────────────────

class FakeWatcher:
    def __init__(self, starts: bool = True):
        self.starts = starts
        self.start_calls = 0
        self.interval = 3.0
        self.owner_name = ""
        self._running = False

    def start(self) -> bool:
        self.start_calls += 1
        self._running = bool(self.starts)
        return self._running

    @property
    def running(self) -> bool:
        return self._running


class FakePerimeter:
    def __init__(self, armed: bool = False, arm_result=None, watcher=None):
        self._config = {**DEFAULTS, "armed": bool(armed)}
        self.arm_result = arm_result or {"ok": True, "armed": True, "message": "armed"}
        self.arm_calls = 0
        self.seams = {}
        self.watcher = watcher if watcher is not None else FakeWatcher()

    @property
    def config(self) -> dict:
        return dict(self._config)

    def configure(self, announce=None, ask_passphrase=None, context=None) -> None:
        self.seams = {"announce": announce, "ask_passphrase": ask_passphrase,
                      "context": context}

    def arm(self, owner: str = "", confirm_selftest: bool = True) -> dict:
        self.arm_calls += 1
        if self.arm_result.get("ok"):
            self._config["armed"] = True
        return dict(self.arm_result)


@pytest.fixture
def boot(monkeypatch):
    """Run start_at_boot against a fake perimeter and a scripted config."""

    def _run(config: dict | None = None, perimeter=None):
        settings = {**DEFAULTS, **(config or {})}
        fake = perimeter if perimeter is not None else FakePerimeter(armed=settings["armed"])
        monkeypatch.setattr(security_perimeter, "_booted", False)
        monkeypatch.setattr(security_perimeter, "load_config", lambda: dict(settings))
        monkeypatch.setattr(security_perimeter, "get_perimeter", lambda: fake)
        return security_perimeter.start_at_boot(), fake

    return _run


# ── Arming at start ────────────────────────────────────────────────────────

def test_arm_at_start_is_the_default():
    assert DEFAULTS["arm_on_start"] is True


def test_a_perimeter_that_is_already_armed_just_starts_watching(boot):
    report, perimeter = boot({"armed": True})

    assert report["started"] is True
    assert report["armed"] is True
    assert perimeter.watcher.start_calls == 1
    # No second arming: the state was already true, and re-running the camera
    # self-test for a perimeter that is already armed buys nothing.
    assert perimeter.arm_calls == 0


def test_an_unarmed_perimeter_arms_itself_at_start(boot):
    report, perimeter = boot({"armed": False, "arm_on_start": True})

    assert perimeter.arm_calls == 1
    assert report["armed"] is True
    assert report["started"] is True
    assert perimeter.watcher.start_calls == 1


def test_switching_off_arm_at_start_keeps_it_down(boot):
    report, perimeter = boot({"armed": False, "arm_on_start": False})

    assert report["started"] is False
    assert report["armed"] is False
    assert perimeter.arm_calls == 0
    assert perimeter.watcher.start_calls == 0
    assert "arm at start" in report["reason"]


def test_a_switched_off_perimeter_never_builds_anything(monkeypatch):
    monkeypatch.setattr(security_perimeter, "_booted", False)
    monkeypatch.setattr(security_perimeter, "load_config",
                        lambda: {**DEFAULTS, "enabled": False})

    def _must_not_be_called():
        raise AssertionError("the camera must not be touched while switched off")

    monkeypatch.setattr(security_perimeter, "get_perimeter", _must_not_be_called)
    report = security_perimeter.start_at_boot()

    assert report["started"] is False
    assert "switched off" in report["reason"]


def test_a_refusal_to_arm_never_opens_the_camera(boot):
    """An arming that failed means nothing is armed, so there is nothing to watch.

    Starting the sampler here would open the camera on every boot just to report
    a fault about a perimeter that is not switched on.
    """
    refusal = {"ok": False, "armed": False, "message": "I have not armed the perimeter.",
               "problems": ["Nobody is enrolled."]}
    report, perimeter = boot({"armed": False, "arm_on_start": True},
                             perimeter=FakePerimeter(arm_result=refusal))

    assert report["ok"] is False
    assert report["started"] is False
    assert report["problems"] == ["Nobody is enrolled."]
    assert perimeter.watcher.start_calls == 0


def test_boot_happens_once_per_process(monkeypatch):
    fake = FakePerimeter(armed=True)
    monkeypatch.setattr(security_perimeter, "_booted", False)
    monkeypatch.setattr(security_perimeter, "load_config", lambda: dict(DEFAULTS | {"armed": True}))
    monkeypatch.setattr(security_perimeter, "get_perimeter", lambda: fake)

    first = security_perimeter.start_at_boot()
    second = security_perimeter.start_at_boot()

    assert first["started"] is True
    assert second["reason"] == "already running"
    assert fake.watcher.start_calls == 1


def test_a_watcher_that_will_not_start_is_reported_as_failed(boot):
    report, _ = boot({"armed": True},
                     perimeter=FakePerimeter(armed=True, watcher=FakeWatcher(starts=False)))

    assert report["ok"] is False
    assert report["started"] is False
    assert "would not start" in report["reason"]


def test_a_broken_perimeter_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(security_perimeter, "_booted", False)
    monkeypatch.setattr(security_perimeter, "load_config", lambda: dict(DEFAULTS))

    def _explode():
        raise RuntimeError("camera stack missing")

    monkeypatch.setattr(security_perimeter, "get_perimeter", _explode)
    report = security_perimeter.start_at_boot()

    assert report["ok"] is False
    assert report["started"] is False
    assert "camera stack missing" in report["reason"]


def test_the_seams_are_handed_over_before_the_first_sample(boot):
    announced, asked, context = [], [], {}
    fake = FakePerimeter(armed=True)
    settings = dict(DEFAULTS | {"armed": True})

    from unittest import mock

    with mock.patch.object(security_perimeter, "_booted", False), \
            mock.patch.object(security_perimeter, "load_config", lambda: settings), \
            mock.patch.object(security_perimeter, "get_perimeter", lambda: fake):
        security_perimeter.start_at_boot(announce=announced.append,
                                         ask_passphrase=lambda reason: True,
                                         context=lambda: context)

    assert fake.seams["announce"].__self__ is announced
    assert fake.seams["ask_passphrase"] is not None
    assert fake.seams["context"] is not None


# ── Saying so ──────────────────────────────────────────────────────────────

def test_disarming_admits_it_will_arm_again(monkeypatch):
    """A half-truth after a reboot is worse than a longer sentence now."""

    # update() re-reads the stored settings, so the test store is replaced here
    # rather than the real one being written by a unit test.
    def build(arm_on_start: bool):
        perimeter = SecurityPerimeter(
            watcher=None, hub=None,
            config=dict(DEFAULTS | {"arm_on_start": arm_on_start}),
            lock=lambda: False)
        monkeypatch.setattr(security_perimeter, "save_config", lambda patch: {
            **dict(DEFAULTS | {"arm_on_start": arm_on_start}), **patch})
        return perimeter.disarm()["message"]

    assert "again at the next start" in build(True)
    assert "again at the next start" not in build(False)


# ── The tool wrapper ───────────────────────────────────────────────────────

class FakeUI:
    def __init__(self, with_prompt: bool = True):
        self.logs = []
        self.asked = []
        if with_prompt:
            self.ask_passphrase = self._ask

    def _ask(self, reason, on_result=None, mode="ask"):
        self.asked.append((reason, mode))

    def write_log(self, text):
        self.logs.append(text)


@pytest.fixture
def faux_boot(monkeypatch):
    """Capture what boot() hands to start_at_boot without touching a camera."""
    captured = {}

    def _fake(*, announce=None, ask_passphrase=None, context=None):
        captured["announce"] = announce
        captured["ask_passphrase"] = ask_passphrase
        captured["context"] = context
        return captured.get("report") or {"ok": True, "started": True, "armed": True,
                                          "reason": "armed and watching",
                                          "problems": []}

    monkeypatch.setattr(security_perimeter, "start_at_boot", _fake)
    return captured


def test_boot_wires_the_prompt_and_says_what_happened(faux_boot):
    ui = FakeUI()
    message = security_tools.boot(ui, speak=lambda text: None)

    assert faux_boot["ask_passphrase"] is not None
    assert "armed and watching" in message
    assert any("armed and watching" in line for line in ui.logs)


def test_boot_says_why_when_it_did_not_start(faux_boot):
    faux_boot["report"] = {"ok": False, "started": False, "armed": False,
                           "reason": "Nobody is enrolled.", "problems": ["Nobody is enrolled."]}
    message = security_tools.boot(FakeUI(), speak=lambda text: None)

    assert "not watching" in message
    assert "Nobody is enrolled." in message


def test_a_ui_without_a_prompt_does_not_break_the_boot(faux_boot):
    message = security_tools.boot(FakeUI(with_prompt=False), speak=lambda text: None)

    assert faux_boot["ask_passphrase"] is None
    assert "armed" in message


def test_a_lock_is_said_out_loud_and_logged(faux_boot):
    spoken = []
    ui = FakeUI()
    security_tools.boot(ui, speak=spoken.append)

    faux_boot["announce"]("workstation_locked", {"reason": "unverified_arrival"})

    assert spoken and "locked" in spoken[0]
    assert any("SEC:" in line for line in ui.logs)


def test_routine_events_are_logged_but_not_spoken(faux_boot):
    spoken = []
    ui = FakeUI()
    security_tools.boot(ui, speak=spoken.append)

    faux_boot["announce"]("evidence_captured", {})

    assert spoken == []
    assert any("photo" in line.lower() for line in ui.logs)


def test_an_announcement_never_raises_out_of_the_seam(faux_boot):
    class Broken:
        def write_log(self, text):
            raise RuntimeError("the log went away")

    security_tools.boot(Broken(), speak=None)
    faux_boot["announce"]("camera_fault", {"reason": "NO_FRAME"})  # must not raise
