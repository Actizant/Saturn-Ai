"""
tests/test_world_model.py
=========================
Hermetic tests for the shared per-turn situation snapshot.

The point of this module is that field names are defined in exactly one place.
These tests pin the normalized names, prove the legacy shapes still read, and
check that writers invalidate the cache so no engine ever acts on stale state.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from core.world_model import (
    Situation,
    WorldModel,
    part_of_day,
    read_window,
)


SENSORIUM = {
    "window_title": "main.py - Saturn - Visual Studio Code",
    "process_name": "Code.exe",
    "pid": 4242,
    "dwell_seconds": 184.0,
    "user_idle_seconds": 3.0,
    "battery_percent": 78.0,
    "power_plugged": True,
    "cpu_percent": 12.0,
    "ram_percent": 41.0,
    "is_user_active": True,
    "timestamp": 1793179473.0,
}


@pytest.fixture()
def model():
    return WorldModel(ttl_seconds=10.0)


@pytest.fixture(autouse=True)
def _no_real_desktop():
    """Stub the live foreground read.

    The snapshot asks the desktop directly so the answer is never three seconds
    stale, which means without this every test would read whatever window the
    developer happens to have focused. Tests that care patch it themselves and
    their patch wins, because it is applied inside this one.
    """
    empty = {"title": "", "class_name": "", "pid": 0, "hwnd": 0, "rect": (0, 0, 0, 0)}
    with patch("core.window_context.get_foreground_window_info", return_value=empty):
        yield


def _sensorium(value=SENSORIUM):
    """Patch the sensorium at its module boundary."""
    fake = type("S", (), {"get_snapshot": lambda self: dict(value)})()
    return patch("core.sensorium.get_sensorium", return_value=fake)


# ── Normalized field names ────────────────────────────────────────────────────

def test_snapshot_exposes_the_real_sensorium_fields(model):
    """The window/process keys are the ones the sensorium actually writes."""
    with _sensorium():
        sit = model.snapshot("what am I looking at")

    assert sit.window_title == "main.py - Saturn - Visual Studio Code"
    assert sit.window_process == "Code.exe"
    assert sit.window_pid == 4242
    assert sit.dwell_seconds == 184.0
    assert sit.battery_percent == 78.0
    assert sit.power_plugged is True
    assert sit.user_active is True


def test_read_window_accepts_both_shapes():
    real = {"window_title": "a.py", "process_name": "Code.exe", "dwell_seconds": 60.0}
    legacy = {"current_window": {"title": "a.py"}, "current_process": "Code.exe", "dwell_seconds": 60.0}

    assert read_window(real) == ("a.py", "Code.exe", 60.0)
    assert read_window(legacy) == ("a.py", "Code.exe", 60.0)
    assert read_window(None) == ("", "", 0.0)
    assert read_window({"dwell_seconds": "not-a-number"}) == ("", "", 0.0)


# ── One snapshot per turn ─────────────────────────────────────────────────────

def test_same_turn_reuses_one_snapshot(model):
    with _sensorium():
        first = model.snapshot("turn query")
        second = model.snapshot("turn query")

    assert first is second
    assert model.stats["snapshots"] == 1
    assert model.stats["hits"] == 1


def test_a_new_query_rebuilds(model):
    with _sensorium():
        model.snapshot("first")
        model.snapshot("second")

    assert model.stats["snapshots"] == 2


def test_refresh_forces_a_reread(model):
    with _sensorium():
        model.snapshot("q")
        model.snapshot("q", refresh=True)

    assert model.stats["snapshots"] == 2


def test_zero_ttl_never_reuses(model):
    model = WorldModel(ttl_seconds=0.0)
    with _sensorium():
        model.snapshot("q")
        model.snapshot("q")

    assert model.stats["snapshots"] == 2


# ── Writer side ───────────────────────────────────────────────────────────────

def test_writers_invalidate_the_cache(model):
    with _sensorium():
        model.snapshot("q")
        model.note_memory("- (Identity) User name: Alex")
        again = model.snapshot("q")

    assert again.salient_memories == "- (Identity) User name: Alex"
    assert model.stats["snapshots"] == 2


def test_note_action_records_verdict_and_reasons(model):
    model.note_action("computer_settings", ok=False, diagnosis="Insufficient privilege.")

    with _sensorium():
        sit = model.snapshot("q")

    assert sit.last_action == "computer_settings"
    assert sit.last_action_ok is False
    assert sit.last_reflection == "Insufficient privilege."


def test_recent_intents_keep_order_without_duplicate_tail(model):
    for name in ("media_control", "open_app", "open_app", "weather"):
        model.note_intent(name)

    with _sensorium():
        sit = model.snapshot("q")

    assert sit.recent_intents == ("media_control", "open_app", "weather")


def test_note_intent_ignores_blank(model):
    model.note_intent("   ")
    with _sensorium():
        assert model.snapshot("q").recent_intents == ()


# ── Devices ───────────────────────────────────────────────────────────────────

def test_connected_devices_come_from_the_provider(model):
    with _sensorium():
        sit = model.snapshot("q", device_provider=lambda: [{"name": "Pixel"}, "junk"])

    assert sit.connected_devices == ({"name": "Pixel"},)


def test_device_provider_failure_degrades_to_empty(model):
    def boom():
        raise RuntimeError("bridge offline")

    with _sensorium():
        sit = model.snapshot("q", device_provider=boom)

    assert sit.connected_devices == ()
    assert sit.window_title, "a dead device bridge must not blank the window context"


# ── Degradation ───────────────────────────────────────────────────────────────

def test_missing_sensorium_falls_back_to_window_context(model):
    fallback = {"title": "Terminal", "class_name": "WindowsTerminal.exe"}
    with patch("core.sensorium.get_sensorium", side_effect=RuntimeError("not started")), \
         patch("core.window_context.get_foreground_window_info", return_value=fallback):
        sit = model.snapshot("q")

    assert sit.window_title == "Terminal"
    assert sit.window_process == "WindowsTerminal.exe"


def test_total_failure_still_returns_a_usable_situation(model):
    with patch("core.sensorium.get_sensorium", side_effect=RuntimeError("x")), \
         patch("core.window_context.get_foreground_window_info", side_effect=RuntimeError("y")):
        sit = model.snapshot("q")

    assert isinstance(sit, Situation)
    assert sit.window_title == ""
    assert "ambient awareness" in sit.brief()


def test_unknown_placeholder_uses_the_desktop_fallback(model):
    """A not-yet-polled sensorium reports 'Unknown'; the bus must not trust it."""
    stub = {"window_title": "", "process_name": "Unknown", "dwell_seconds": 0.0}
    fallback = {"title": "Chrome", "class_name": "Chrome_WidgetWin_1"}
    with _sensorium(stub), \
         patch("core.window_context.get_foreground_window_info", return_value=fallback):
        sit = model.snapshot("q")

    assert sit.window_title == "Chrome"
    assert sit.window_process == "Chrome_WidgetWin_1", "'Unknown' is truthy, so a presence check would never fall back"


def test_placeholders_normalize_to_empty_when_nothing_is_available(model):
    stub = {"window_title": "n/a", "process_name": "Unknown"}
    empty = {"title": "", "class_name": "Unknown"}
    with _sensorium(stub), \
         patch("core.window_context.get_foreground_window_info", return_value=empty):
        sit = model.snapshot("q")

    assert sit.window_title == ""
    assert sit.window_process == ""
    assert "Active Window" not in sit.brief()


# ── Prompt rendering ──────────────────────────────────────────────────────────

def test_brief_renders_the_situation_for_the_prompt(model):
    with _sensorium():
        brief = model.snapshot("q").brief()

    assert "[REAL-TIME SENSORY & SITUATIONAL AWARENESS]" in brief
    assert "main.py - Saturn - Visual Studio Code" in brief
    assert "Code.exe" in brief
    assert "Battery: 78% (Plugged in (AC))" in brief


def test_brief_omits_missing_sections(model):
    empty = {"window_title": "", "process_name": "", "dwell_seconds": 0.0}
    # Patch the desktop fallback too, or the test would read the real screen.
    with _sensorium(empty), \
         patch("core.window_context.get_foreground_window_info", return_value={"title": "", "class_name": ""}):
        brief = model.snapshot("q").brief()

    assert "Active Window" not in brief
    assert "Battery" not in brief


def test_as_dict_is_a_superset_of_the_raw_sensorium(model):
    with _sensorium():
        data = model.snapshot("q").as_dict()

    for key in SENSORIUM:
        assert key in data, f"{key} was dropped from the telemetry tool payload"
    assert data["window_title"] == "main.py - Saturn - Visual Studio Code"
    assert data["connected_devices"] == []


# ── Time of day ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "hour,expected",
    [(7, "morning"), (11, "morning"), (12, "afternoon"), (17, "afternoon"),
     (18, "evening"), (22, "evening"), (23, "late night"), (3, "late night")],
)
def test_part_of_day_buckets(hour, expected):
    assert part_of_day(datetime(2026, 10, 30, hour, 0, 0)) == expected


def test_snapshot_uses_the_frozen_clock(model):
    with _sensorium(), patch("core.world_model.datetime", wraps=datetime) as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 30, 21, 0, 0)
        assert model.snapshot("q").part_of_day == "evening"


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_production_consumes_the_bus():
    """Engines must read the situation, and the invented keys must stay dead."""
    root = Path(__file__).resolve().parent.parent
    main_src = (root / "main.py").read_text(encoding="utf-8")
    proactive_src = (root / "actions" / "proactive.py").read_text(encoding="utf-8")

    assert "world_model.snapshot" in main_src
    assert "world_model.note_intent" in main_src
    assert "read_window" in proactive_src

    # ``current_window`` / ``current_process`` were never real snapshot keys;
    # reading them is what left two engines silently blind to the active window.
    for source in (main_src, proactive_src):
        assert '"current_window"' not in source
        assert '"current_process"' not in source
