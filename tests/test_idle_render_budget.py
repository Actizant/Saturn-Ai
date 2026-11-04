"""The idle app must not spend its frame budget animating nothing.

Measured on the live app before these guards existed: with no conversation, no
audio and no pointer movement, the WebGL backdrop still composited ~42 frames a
second. Each of those frames invalidates the window, so every translucent widget
layered above it was repainted too - 140 QLabel and 115 QWidget paints a second,
1.4 cores on the GUI thread - and the globe page could not hold its vsync
deadlines. To the user the whole app looked like a low frame-rate video.

The fix is a budget, not a switch: an untouched backdrop drops to a slow ambient
rate and returns to full frame rate the moment there is audio, a non-idle AI
state, pointer movement or a state change.
"""
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QHideEvent, QShowEvent
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def ui(qapp):
    import ui as module
    return module


def test_reactor_page_accepts_a_render_budget(qapp):
    """The backdrop must be throttleable from the host, in one place."""
    html = (ROOT / "assets" / "web_background" / "saturn_orb.html").read_text(encoding="utf-8")
    assert "window.setSaturnRenderBudget" in html
    assert "if(budget&&ms-last<budget)return;" in html, \
        "the animation loop must gate on the host-supplied budget, not a literal"
    assert "if(U-_lastFrame<15.5)return;" not in html


class _FakePage:
    def __init__(self):
        self.calls = []

    def runJavaScript(self, js):
        self.calls.append(js)


class _FakeView:
    def __init__(self):
        self.page_obj = _FakePage()

    def page(self):
        return self.page_obj


def _bare_backdrop(ui, monkeypatch):
    """A BackgroundWidget with no WebEngine: the budget logic, nothing else."""
    monkeypatch.setattr(ui.BackgroundWidget, "_init_web_engine", lambda self: None)
    bg = ui.BackgroundWidget()
    bg._web_view = _FakeView()
    bg._render_fps = 0.0
    return bg


def test_backdrop_is_uncapped_while_things_happen(ui, monkeypatch):
    bg = _bare_backdrop(ui, monkeypatch)
    now = bg._lively_since
    assert bg._budget_fps(now + 0.5) == 0.0, "just-seen activity keeps full frame rate"
    assert bg._budget_fps(now + bg.IDLE_GRACE_SECS + 1) == bg.IDLE_RENDER_FPS


def test_backdrop_stays_fast_for_engaged_states(ui, monkeypatch):
    bg = _bare_backdrop(ui, monkeypatch)
    bg._last_state = "THINKING"
    later = bg._lively_since + bg.IDLE_GRACE_SECS + 1
    assert bg._budget_fps(later) == 0.0, "thinking/executing/scanning render at full rate"

    bg._last_state = "IDLE"
    assert bg._budget_fps(later) == bg.IDLE_RENDER_FPS


def test_listening_with_a_quiet_room_is_not_an_animation(ui, monkeypatch):
    """Live voice parks the app in LISTENING for minutes at a time.

    The app sat in LISTENING with an open microphone for the whole idle session
    that was measured, so treating that state as "engaged" left the backdrop at
    full frame rate forever - which is the case the budget exists for. Sound
    itself (set_audio_level) is what earns the frames back.
    """
    bg = _bare_backdrop(ui, monkeypatch)
    bg._last_state = "LISTENING"
    later = bg._lively_since + bg.IDLE_GRACE_SECS + 1
    assert bg._budget_fps(later) == bg.IDLE_RENDER_FPS

    bg._do_set_audio_level(0.3)   # the user starts talking
    assert bg._budget_fps(time.monotonic()) == 0.0


def test_idle_budget_is_pushed_into_the_page(ui, monkeypatch):
    bg = _bare_backdrop(ui, monkeypatch)
    bg._lively_since -= bg.IDLE_GRACE_SECS + 1
    bg._apply_render_budget()
    js = bg._web_view.page().calls[-1]
    assert "setSaturnRenderBudget" in js
    assert bg._render_fps == bg.IDLE_RENDER_FPS

    # A state change is worth a full frame rate again, immediately.
    bg._mark_lively()
    assert bg._render_fps == 0.0
    assert "setSaturnRenderBudget(0)" in bg._web_view.page().calls[-1].replace(" ", "")


def test_audio_and_pointer_count_as_activity(ui, monkeypatch):
    bg = _bare_backdrop(ui, monkeypatch)
    bg._lively_since -= bg.IDLE_GRACE_SECS + 1
    bg._apply_render_budget()
    assert bg._render_fps == bg.IDLE_RENDER_FPS

    bg._do_set_audio_level(0.4)
    assert bg._render_fps == 0.0

    bg._lively_since -= bg.IDLE_GRACE_SECS + 1
    bg._apply_render_budget()
    assert bg._render_fps == bg.IDLE_RENDER_FPS

    bg.set_pointer_norm(0.2, 0.3)
    assert bg._render_fps == 0.0


def test_silent_background_audio_updates_do_not_defeat_the_budget(ui, monkeypatch):
    """A quiet microphone is reported repeatedly; that is not activity."""
    bg = _bare_backdrop(ui, monkeypatch)
    bg._lively_since -= bg.IDLE_GRACE_SECS + 1
    bg._apply_render_budget()
    for level in (0.0, 0.005, 0.0):
        bg._do_set_audio_level(level)
    assert bg._render_fps == bg.IDLE_RENDER_FPS


def test_waveform_shimmers_instead_of_repainting_at_full_rate(ui):
    w = ui.VoiceWaveformVisualizer()
    assert w._timer.interval() == w.ACTIVE_INTERVAL_MS
    w._tick()
    assert w._timer.interval() == w.IDLE_INTERVAL_MS, \
        "a silent waveform must drop to its idle interval"

    w.set_audio_level(0.6)
    assert w._timer.interval() == w.ACTIVE_INTERVAL_MS


def test_waveform_stops_ticking_while_hidden(ui):
    w = ui.VoiceWaveformVisualizer()
    w.hideEvent(QHideEvent())
    assert not w._timer.isActive()
    w.showEvent(QShowEvent())
    assert w._timer.isActive()
