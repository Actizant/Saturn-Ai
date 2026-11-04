"""
tests/test_user_window_context.py
==================================
"What am I doing?" must describe the user's window, and must say "IDE" when the
user is in one.

Both halves of that were broken in a way that made the assistant confidently
wrong rather than visibly broken:

* ``PassiveSensorium`` polled ``GetForegroundWindow`` and reported whatever
  answered - including Saturn's own always-on-top overlay, which is the window
  most likely to hold the focus at the moment the user starts talking. Saturn
  then described itself as the user's work.
* IDE detection was a process allow-list of ``code.exe`` / ``cursor.exe`` /
  ``pycharm64.exe`` / ``idea64.exe`` / ``devenv.exe``. Every Chromium-shell
  editor - Qoder, Windsurf, Trae, Kiro, Cursor, VS Code - reports the same
  process and class, so Qoder could never match, and the honest answer from
  that classifier's point of view was "no IDE is open".

These tests pin the fix for both, plus the per-request injection that stops a
snapshot taken at connection time from being repeated for the whole session.
"""

import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from core import app_kind
from core import window_context

ROOT = Path(__file__).resolve().parents[1]

QODER_WINDOW = {
    "title": "cognition_loop.py - Saturn-Ai-Evo-main - Qoder IDE",
    # The exact shape that defeated the old process allow-list: an Electron
    # shell has no per-app process name to match on.
    "class_name": "Chrome_WidgetWin_1",
    "pid": 9812,
    "hwnd": 0x2B,
    "rect": (0, 0, 1600, 900),
    "bbox": (0, 0, 1600, 900),
}

OWN_WINDOW = {
    "title": "Saturn",
    "class_name": "Qt5152QWindowIcon",
    "pid": os.getpid(),
    "hwnd": 0x11,
    "rect": (10, 10, 420, 320),
    "bbox": (10, 10, 430, 330),
}


@pytest.fixture(autouse=True)
def _clean_state():
    window_context._LAST_USER_WINDOW.clear()
    window_context._OWN_PIDS.clear()
    yield
    window_context._LAST_USER_WINDOW.clear()
    window_context._OWN_PIDS.clear()


# ── Ownership ────────────────────────────────────────────────────────────────

def test_our_own_process_is_recognised_as_ours():
    assert window_context.is_own_window(OWN_WINDOW) is True


def test_another_process_is_the_users():
    assert window_context.is_own_window(QODER_WINDOW) is False


def test_a_registered_pid_also_counts_as_ours():
    window_context.register_own_window_pid(4242)
    assert window_context.is_own_window({"pid": 4242}) is True


@pytest.mark.parametrize("bad", [None, {}, {"pid": "not-a-pid"}, {"pid": 0}])
def test_unusable_descriptions_are_never_claimed_as_ours(bad):
    assert window_context.is_own_window(bad) is False


# ── Which window is reported ─────────────────────────────────────────────────

def test_the_users_window_is_reported_when_it_has_the_focus():
    with patch("core.window_context.get_foreground_window_info", return_value=dict(QODER_WINDOW)), \
         patch("core.window_context.process_name_for", return_value="Qoder.exe"):
        info = window_context.get_active_user_window()

    assert info["title"] == QODER_WINDOW["title"]
    assert info["method"] == "foreground"
    assert info["self_window"] is False


def test_saturns_own_window_is_replaced_by_the_next_real_window():
    """The reported bug: Saturn in focus, Saturn in the answer."""
    with patch("core.window_context.get_foreground_window_info", return_value=dict(OWN_WINDOW)), \
         patch("core.window_context._zorder_user_window", return_value=dict(QODER_WINDOW)):
        info = window_context.get_active_user_window()

    assert info["title"] == QODER_WINDOW["title"], "Saturn described its own window as the user's work"
    assert info["method"] == "zorder"
    assert info["self_window"] is True, "the caller must still know Saturn held the focus"


def test_our_own_title_is_never_returned_even_as_a_last_resort():
    with patch("core.window_context.get_foreground_window_info", return_value=dict(OWN_WINDOW)), \
         patch("core.window_context._zorder_user_window", return_value={}):
        info = window_context.get_active_user_window()

    assert info["title"] == ""
    assert info["method"] == "self"
    assert info["self_window"] is True


def test_the_last_real_window_survives_a_desktop_it_cannot_read():
    with patch("core.window_context.get_foreground_window_info", return_value=dict(QODER_WINDOW)), \
         patch("core.window_context.process_name_for", return_value="Qoder.exe"):
        first = window_context.get_active_user_window()
    assert first["title"] == QODER_WINDOW["title"]

    with patch("core.window_context.get_foreground_window_info", return_value=dict(OWN_WINDOW)), \
         patch("core.window_context._zorder_user_window", return_value={}):
        second = window_context.get_active_user_window()

    assert second["title"] == QODER_WINDOW["title"]
    assert second["method"] == "cache"


def test_a_shell_window_is_not_somebody_working():
    """Progman and the taskbar are always visible, so they would win any scan."""
    assert "progman" in window_context._SHELL_CLASSES
    assert "shell_traywnd" in window_context._SHELL_CLASSES


# ── App classification ───────────────────────────────────────────────────────

def test_qoder_is_an_ide_even_though_it_looks_like_chromium():
    assert app_kind.classify(QODER_WINDOW["title"], QODER_WINDOW["class_name"]) == app_kind.IDE


@pytest.mark.parametrize("title", [
    "main.py - saturn - Visual Studio Code",
    "core.py - Saturn - Cursor",
    "app.py - Windsurf",
    "server.ts - Trae",
    "notes.md - Zed",
    "build.gradle - Android Studio",
])
def test_chromium_shell_editors_are_recognised_from_the_title(title):
    assert app_kind.is_ide(title, "Chrome_WidgetWin_1") is True


@pytest.mark.parametrize("title", [
    "main.py - PyCharm",
    "project - IntelliJ IDEA",
    "hello.go - GoLand",
])
def test_native_editors_are_recognised_too(title):
    assert app_kind.is_ide(title) is True


def test_a_short_hint_does_not_fire_inside_a_longer_word():
    assert app_kind.classify("organised notes - Notepad") != app_kind.IDE
    assert app_kind.classify("Analyzed report - Chrome", "chrome.exe") == app_kind.BROWSER


def test_the_process_name_alone_is_enough_for_a_document_title():
    assert app_kind.classify("report.docx", "WINWORD.EXE") == app_kind.OFFICE


def test_describe_names_the_app_and_the_kind():
    described = app_kind.describe(QODER_WINDOW["title"], "Qoder.exe", QODER_WINDOW["class_name"])

    assert described.startswith("Qoder IDE")
    assert "IDE" in described


def test_describe_is_empty_when_nothing_is_known():
    assert app_kind.describe("", "", "") == ""


def test_a_meeting_is_never_reported_as_chat():
    assert app_kind.classify("Standup - Zoom Meeting", "zoom.exe") == app_kind.MEETING


# ── The sensorium reports the user, not itself ───────────────────────────────

def test_sensorium_snapshot_carries_the_window_class_and_ownership():
    from core.sensorium import PassiveSensorium

    sensor = PassiveSensorium(poll_interval=100.0)
    sensor.current_window = dict(QODER_WINDOW)
    sensor.current_window["method"] = "zorder"
    sensor.current_process_name = "Qoder.exe"

    snapshot = sensor.get_snapshot()

    assert snapshot["window_title"] == QODER_WINDOW["title"]
    assert snapshot["window_class"] == "Chrome_WidgetWin_1"
    assert snapshot["window_method"] == "zorder"
    assert snapshot["self_window"] is False


def test_sensorium_asks_for_the_users_window_not_the_foreground():
    from core.sensorium import PassiveSensorium

    sensor = PassiveSensorium(poll_interval=100.0)
    with patch("core.sensorium.get_active_user_window", return_value=dict(QODER_WINDOW)) as asked:
        sensor._update_telemetry()

    assert asked.called
    assert sensor.current_window["title"] == QODER_WINDOW["title"]
    # The stub carries no resolved process name, so the sensorium keeps its
    # honest "Unknown" marker rather than blanking the field.
    assert sensor.current_process_name == "Unknown"


def test_a_qoder_session_now_raises_the_dev_session_alert():
    """The old allow-list could never fire for an Electron editor."""
    from core.sensorium import PassiveSensorium

    sensor = PassiveSensorium(poll_interval=100.0)
    alerts = []
    sensor.register_interjection_handler(lambda alert_type, meta: alerts.append((alert_type, meta)))
    sensor.current_window = dict(QODER_WINDOW)
    sensor.current_process_name = "Chrome_WidgetWin_1"
    sensor.user_idle_seconds = 0.0

    with patch("core.sensorium.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 10, 30, 14, 0, 0)
        sensor._evaluate_heuristics()

    dev = [a for a in alerts if a[0] == "dev_session"]
    assert dev, f"Qoder IDE was not detected as a coding session: {alerts}"
    assert dev[0][1]["project"] == "Saturn-Ai-Evo-main"


# ── The brief the model answers from ─────────────────────────────────────────

def _situation(**overrides):
    from core.world_model import Situation

    base = dict(
        query="what am i doing",
        window_title=QODER_WINDOW["title"],
        window_process="Qoder.exe",
        window_class=QODER_WINDOW["class_name"],
        window_source="foreground",
        app_kind=app_kind.IDE,
        app_label="Qoder IDE - code editor / IDE",
    )
    base.update(overrides)
    return Situation(**base)


def test_the_brief_states_the_app_and_its_kind():
    brief = _situation().brief()

    assert "Active App: Qoder IDE" in brief
    assert "IDE" in brief


def test_the_brief_forbids_denying_an_app_that_is_open():
    brief = _situation().brief()

    assert "never say an app, editor or IDE is not open" in brief
    assert "the newest block wins" in brief


def test_the_brief_says_so_when_saturn_held_the_focus():
    brief = _situation(window_source="zorder").brief()

    assert "most recently used" in brief
    assert "Saturn itself has focus" in brief


def test_a_foreground_window_gets_no_hedge():
    assert "most recently used" not in _situation().brief()


# ── Per-request freshness ────────────────────────────────────────────────────

def test_main_injects_a_fresh_situation_into_every_request():
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "def _situation_context_for_request" in source
    assert "situation_ctx = _situation_context_for_request(text)" in source
    assert 'routed_text = f"{situation_ctx}\\n\\n{routed_text}"' in source


def test_the_live_turn_sends_the_routed_text_not_the_bare_utterance():
    """The live path dropped both the recalled memory and the situation."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "text=routed_text" in source
    assert "parts=[types.Part.from_text(text=text)]" not in source


def test_the_situation_helper_degrades_to_an_empty_string():
    """A dead desktop read must not cost the user their answer."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("def _situation_context_for_request")
    body = source[start:source.index("\nTOOL_DECLARATIONS", start)]

    assert "try:" in body
    assert "except Exception" in body
    assert 'return ""' in body
