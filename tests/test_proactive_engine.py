import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import pytest

from core.sensorium import PassiveSensorium
from core.anticipatory_watcher import AnticipatoryWatcher
from core.clipboard_sentry import ClipboardSentry
from actions.proactive import ProactiveEngine


# ── Sensorium Proactive Tests ───────────────────────────────────────────────────

def test_sensorium_power_transitions():
    sensorium = PassiveSensorium(poll_interval=100.0)
    alerts = []
    sensorium.register_interjection_handler(lambda at, meta: alerts.append((at, meta)))

    # Initial state: on AC power
    sensorium._prev_power_plugged = True
    sensorium.power_plugged = False
    sensorium.battery_percent = 78.0

    # Freeze the clock mid-afternoon so the 06:00-11:00 first-morning-interaction
    # briefing cannot add a second, unrelated interjection to the count.
    with patch("core.sensorium.datetime", wraps=datetime) as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 30, 14, 0, 0)
        sensorium._evaluate_heuristics()
    assert len(alerts) == 1
    assert alerts[0][0] == "power_disconnected"
    assert "battery at 78 percent" in alerts[0][1]["speech"]

    # Transition: plugged back in
    alerts.clear()
    sensorium.power_plugged = True
    sensorium.battery_percent = 79.0

    with patch("core.sensorium.datetime", wraps=datetime) as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 30, 14, 0, 1)
        sensorium._evaluate_heuristics()
    assert len(alerts) == 1
    assert alerts[0][0] == "power_connected"
    assert "Charging at 79 percent" in alerts[0][1]["speech"]


def test_sensorium_battery_warnings():
    sensorium = PassiveSensorium(poll_interval=100.0)
    alerts = []
    sensorium.register_interjection_handler(lambda at, meta: alerts.append((at, meta)))

    # Low battery (< 20%) on battery power
    sensorium.power_plugged = False
    sensorium.battery_percent = 18.0
    sensorium._evaluate_heuristics()
    assert any(a[0] == "battery_low" for a in alerts)

    # Critical battery (< 10%) on battery power
    alerts.clear()
    sensorium.battery_percent = 8.0
    sensorium._evaluate_heuristics()
    assert any(a[0] == "battery_critical" for a in alerts)


def test_sensorium_meeting_mode_detection():
    sensorium = PassiveSensorium(poll_interval=100.0)
    alerts = []
    sensorium.register_interjection_handler(lambda at, meta: alerts.append((at, meta)))

    # User launches Zoom
    sensorium.current_process_name = "Zoom.exe"
    sensorium.current_window = {"title": "Zoom Meeting"}
    sensorium._evaluate_heuristics()

    assert any(a[0] == "meeting_mode_enter" for a in alerts)
    assert sensorium._in_meeting_mode is True

    # User leaves Zoom
    alerts.clear()
    sensorium.current_process_name = "explorer.exe"
    sensorium.current_window = {"title": "File Explorer"}
    sensorium._evaluate_heuristics()

    assert any(a[0] == "meeting_mode_exit" for a in alerts)
    assert sensorium._in_meeting_mode is False


def test_sensorium_coding_session_detection():
    sensorium = PassiveSensorium(poll_interval=100.0)
    alerts = []
    sensorium.register_interjection_handler(lambda at, meta: alerts.append((at, meta)))

    sensorium.current_process_name = "Code.exe"
    sensorium.current_window = {"title": "main.py - saturn-ai - Visual Studio Code"}
    sensorium._evaluate_heuristics()

    dev_alerts = [a for a in alerts if a[0] == "dev_session"]
    assert len(dev_alerts) == 1
    assert dev_alerts[0][1]["project"] == "saturn-ai"


# ── Anticipatory Watcher Tests ─────────────────────────────────────────────────

def test_anticipatory_watcher_calendar_reminder():
    watcher = AnticipatoryWatcher(check_interval_sec=100.0)
    nudges = []
    watcher.set_nudge_callback(lambda msg, cat: nudges.append((msg, cat)))

    # Fake calendar event 8 minutes from now
    target_time_str = (datetime.now() + timedelta(minutes=8)).strftime("%H:%M")
    mock_events = [
        {"title": "Sync with Product Team", "date": "today", "time": target_time_str}
    ]

    watcher._last_calendar_check = 0.0
    with patch("actions.calendar_scheduler._load_events", return_value=mock_events), \
         patch("psutil.virtual_memory") as mock_vm:
        mock_vm.return_value = MagicMock(percent=50.0)
        watcher.run_inspection_cycle()

    assert len(nudges) >= 1
    cal_nudges = [n for n in nudges if n[1] == "calendar"]
    assert len(cal_nudges) == 1
    msg, cat = cal_nudges[0]
    assert "Sync with Product Team" in msg
    assert "begins in" in msg and "minutes" in msg


def test_anticipatory_watcher_network_restoration():
    watcher = AnticipatoryWatcher(check_interval_sec=100.0)
    nudges = []
    watcher.set_nudge_callback(lambda msg, cat: nudges.append((msg, cat)))

    # State 1: network is reported down
    watcher._last_network_check = 0.0
    with patch("socket.create_connection", side_effect=OSError("Network unreachable")), \
         patch("psutil.virtual_memory") as mock_vm:
        mock_vm.return_value = MagicMock(percent=50.0)
        watcher.run_inspection_cycle()
    assert watcher._network_was_down is True

    # State 2: network comes back up
    watcher._last_network_check = 0.0
    with patch("socket.create_connection", return_value=MagicMock()), \
         patch("psutil.virtual_memory") as mock_vm:
        mock_vm.return_value = MagicMock(percent=50.0)
        watcher.run_inspection_cycle()
    assert watcher._network_was_down is False
    net_nudges = [n for n in nudges if n[1] == "network"]
    assert len(net_nudges) == 1
    assert "Internet connectivity has been restored" in net_nudges[0][0]


# ── Clipboard Sentry Tests ─────────────────────────────────────────────────────

def test_clipboard_sentry_classification():
    sentry = ClipboardSentry()

    # Traceback
    tb_text = (
        "Traceback (most recent call last):\n"
        "  File 'main.py', line 10, in <module>\n"
        "ZeroDivisionError: division by zero"
    )
    assert sentry._classify_content(tb_text) == "error_traceback"

    # YouTube URL
    yt_text = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert sentry._classify_content(yt_text) == "youtube_url"

    # GitHub Repo URL
    gh_text = "https://github.com/torvalds/linux"
    assert sentry._classify_content(gh_text) == "github_url"

    # SQL query
    sql_text = "SELECT id, username, email FROM users WHERE active = 1"
    assert sentry._classify_content(sql_text) == "sql_query"

    # JSON Data
    json_text = '{"status": "ok", "latency_ms": 12.4}'
    assert sentry._classify_content(json_text) == "json_data"

    # Short / normal text
    assert sentry._classify_content("hello world") is None


# ── Proactive Engine Prompting Tests ───────────────────────────────────────────

def test_proactive_engine_rotation_and_sensors():
    engine = ProactiveEngine(min_silence_secs=10, check_cooldown=20)
    memory = {"name": "Developer", "topics": ["AI", "Robotics"]}

    snapshot = {
        "window_title": "app.py - Orion",
        "process_name": "Code.exe",
        "dwell_seconds": 180.0,
        "battery_percent": 90,
        "power_plugged": True,
    }

    # First rotation
    p0 = engine.build_prompt(memory=memory, sensor_snapshot=snapshot)
    assert "Working in 'app.py - Orion'" in p0
    assert "Focus on the user's active projects" in p0

    engine.mark_triggered()

    # Second rotation
    p1 = engine.build_prompt(memory=memory, sensor_snapshot=snapshot)
    assert "Focus on the time of day and the user's wellbeing" in p1

    engine.mark_triggered()

    # Third rotation (context aware screen/task focus)
    p2 = engine.build_prompt(memory=memory, sensor_snapshot=snapshot)
    assert "Focus on the user's current task or active window context" in p2
