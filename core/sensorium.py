"""
Saturn Passive Sensorium Engine (v2)
An omnipresent background telemetry and perception daemon.
Continuously senses user context, active task dwell time, system vitals,
and user idle state with zero performance overhead.
"""

import sys
import time
import threading
import ctypes
from ctypes import wintypes
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime
import psutil

from core.app_kind import is_ide
from core.window_context import get_active_user_window

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def get_idle_time_seconds() -> float:
    """Returns the number of seconds since the user last touched mouse or keyboard."""
    if not user32 or not kernel32:
        return 0.0
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if user32.GetLastInputInfo(ctypes.byref(lii)):
        millis = kernel32.GetTickCount() - lii.dwTime
        return max(0.0, millis / 1000.0)
    return 0.0


def get_cursor_position():
    """Screen position of the pointer, or None where there is no pointer.

    Read with GetCursorPos rather than a hook: position is not content, it costs
    nothing, and it cannot hang the input queue the way a global hook can.
    """
    if not user32:
        return None
    try:
        point = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(point)):
            return (int(point.x), int(point.y))
    except Exception:
        pass
    return None


class PassiveSensorium:
    def __init__(self, poll_interval: float = 3.0):
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Telemetry State
        self.current_window: Dict[str, Any] = {}
        self.current_process_name: str = "Unknown"
        self.dwell_start_time: float = time.time()
        self.current_dwell_seconds: float = 0.0
        self.user_idle_seconds: float = 0.0

        # Pointer travel between polls. A crude activity signal, and the only
        # one the perimeter is allowed to use: no keys, no hook, no content.
        self.cursor_pos = None
        self.cursor_moved_px: int = 0

        # System Vitals
        self.cpu_percent: float = 0.0
        self.ram_percent: float = 0.0
        self.battery_percent: Optional[float] = None
        self.power_plugged: Optional[bool] = None
        self._prev_power_plugged: Optional[bool] = None

        # Proactive Workflow & Rhythm Tracking
        self._in_meeting_mode: bool = False
        self._last_morning_briefing_date: str = ""
        self._last_dev_project: str = ""
        self._was_away: bool = False

        # Observers & Interjection Callbacks
        self._interjection_callbacks: List[Callable[[str, Dict[str, Any]], None]] = []
        self._last_alert_time: Dict[str, float] = {}

    def register_interjection_handler(self, callback: Callable[[str, Dict[str, Any]], None]):
        """Register a callback for autonomous interjections: callback(alert_type, metadata)"""
        self._interjection_callbacks.append(callback)

    def start(self):
        """Starts the background sensorium loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="SaturnSensorium", daemon=True)
        self._thread.start()

    def stop(self):
        """Stops the sensorium loop."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def get_snapshot(self) -> Dict[str, Any]:
        """Returns the full active situational context of the user and machine."""
        return {
            "window_title": self.current_window.get("title", ""),
            "window_class": self.current_window.get("class_name", ""),
            "process_name": self.current_process_name,
            # The owning process, so a consumer can tell Saturn's own window
            # from the user's without a second desktop query. ``self_window``
            # is the verdict of that check; it is False on every normal read
            # because get_active_user_window() already skips our own windows.
            "pid": self.current_window.get("pid"),
            "self_window": bool(self.current_window.get("self_window")),
            # foreground / zorder / cache - how the window was chosen.
            "window_method": self.current_window.get("method", ""),
            "dwell_seconds": round(self.current_dwell_seconds, 1),
            "user_idle_seconds": round(self.user_idle_seconds, 1),
            "cursor_pos": self.cursor_pos,
            "cursor_moved_px": self.cursor_moved_px,
            "cpu_percent": self.cpu_percent,
            "ram_percent": self.ram_percent,
            "battery_percent": self.battery_percent,
            "power_plugged": self.power_plugged,
            "is_user_active": self.user_idle_seconds < 60.0,
            "timestamp": time.time(),
        }

    def _loop(self):
        while self._running:
            try:
                self._update_telemetry()
                self._evaluate_heuristics()
            except Exception as e:
                # Sensorium must never crash the host process
                pass
            time.sleep(self.poll_interval)

    def _update_telemetry(self):
        # 1. Window & Process
        # The user's window, not Saturn's own overlay: see core/window_context.
        win_info = get_active_user_window()
        title = win_info.get("title", "")
        proc_name = str(win_info.get("process_name") or "") or "Unknown"

        # Dwell time tracking
        if title != self.current_window.get("title", "") or proc_name != self.current_process_name:
            self.current_window = win_info
            self.current_process_name = proc_name
            self.dwell_start_time = time.time()
            self.current_dwell_seconds = 0.0
        else:
            self.current_dwell_seconds = time.time() - self.dwell_start_time

        # 2. Idle State
        self.user_idle_seconds = get_idle_time_seconds()

        # 2b. Pointer travel since the last poll
        position = get_cursor_position()
        if position is None or self.cursor_pos is None:
            self.cursor_moved_px = 0
        else:
            dx = position[0] - self.cursor_pos[0]
            dy = position[1] - self.cursor_pos[1]
            self.cursor_moved_px = int((dx * dx + dy * dy) ** 0.5)
        self.cursor_pos = position

        # 3. System Vitals
        self.cpu_percent = psutil.cpu_percent(interval=None)
        self.ram_percent = psutil.virtual_memory().percent
        battery = psutil.sensors_battery()
        if battery:
            self.battery_percent = battery.percent
            self.power_plugged = battery.power_plugged
        else:
            self.battery_percent = None
            self.power_plugged = None

    def _evaluate_heuristics(self):
        now = time.time()

        # Alert: Severe RAM Pressure (> 92% sustained)
        if self.ram_percent > 92.0:
            if now - self._last_alert_time.get("ram_critical", 0) > 300:  # 5 min cooldown
                self._last_alert_time["ram_critical"] = now
                self._emit_alert(
                    "ram_critical",
                    {
                        "message": f"System memory threshold at {self.ram_percent}% capacity.",
                        "speech": f"Warning: system memory has reached {int(self.ram_percent)} percent capacity.",
                        "ram_percent": self.ram_percent,
                    },
                )

        # Alert: Power Connected / Disconnected State Transitions
        if self.power_plugged is not None and self._prev_power_plugged is not None:
            if self._prev_power_plugged is False and self.power_plugged is True:
                pct = int(self.battery_percent) if self.battery_percent is not None else 100
                self._emit_alert(
                    "power_connected",
                    {
                        "message": f"AC power connected. Charging at {pct}%.",
                        "speech": f"Power connected, sir. Charging at {pct} percent.",
                        "battery_percent": pct,
                    },
                )
            elif self._prev_power_plugged is True and self.power_plugged is False:
                pct = int(self.battery_percent) if self.battery_percent is not None else 100
                self._emit_alert(
                    "power_disconnected",
                    {
                        "message": f"AC power disconnected. Running on battery ({pct}%).",
                        "speech": f"Power disconnected, sir. Running on battery at {pct} percent.",
                        "battery_percent": pct,
                    },
                )
        self._prev_power_plugged = self.power_plugged

        # Alert: Battery Low (< 20% on battery)
        if self.battery_percent is not None and not self.power_plugged:
            if self.battery_percent <= 10.0:
                if now - self._last_alert_time.get("battery_critical", 0) > 240:
                    self._last_alert_time["battery_critical"] = now
                    self._emit_alert(
                        "battery_critical",
                        {
                            "message": f"Critical battery warning: {int(self.battery_percent)}% remaining.",
                            "speech": f"Critical warning, sir: battery has dropped to {int(self.battery_percent)} percent. Please connect the charger immediately.",
                            "battery_percent": self.battery_percent,
                        },
                    )
            elif self.battery_percent <= 20.0:
                if now - self._last_alert_time.get("battery_low", 0) > 600:
                    self._last_alert_time["battery_low"] = now
                    self._emit_alert(
                        "battery_low",
                        {
                            "message": f"Battery low: {int(self.battery_percent)}% remaining.",
                            "speech": f"Sir, your battery is at {int(self.battery_percent)} percent on battery power. I recommend plugging in soon.",
                            "battery_percent": self.battery_percent,
                        },
                    )

        # Proactive: Meeting Mode Detection (Zoom, Teams, Webex, Google Meet)
        proc_lower = self.current_process_name.lower()
        title_lower = (self.current_window.get("title") or "").lower()
        is_meeting = any(p in proc_lower for p in ("zoom.exe", "teams.exe", "webex.exe")) or "meet.google.com" in title_lower

        if is_meeting and not self._in_meeting_mode:
            self._in_meeting_mode = True
            self._emit_alert(
                "meeting_mode_enter",
                {
                    "message": f"Meeting application detected ({self.current_process_name}). Auto-engaged Meeting Mode.",
                    "speech": "Meeting detected, sir. Silencing background media and optimizing audio.",
                    "action": "meeting_mode_start"
                }
            )
        elif not is_meeting and self._in_meeting_mode:
            self._in_meeting_mode = False
            self._emit_alert(
                "meeting_mode_exit",
                {
                    "message": "Meeting concluded. Normal audio levels restored.",
                    "speech": "Meeting concluded, sir. Audio restored to normal.",
                    "action": "meeting_mode_stop"
                }
            )

        # Proactive: Development / Coding Session Detection
        # Classified together with the window title, because every Electron
        # editor reports the same process name (Qoder, Windsurf, Trae, Cursor
        # and VS Code are all "Chrome_WidgetWin_1"), so a process allow-list
        # concluded "no IDE" while the user was writing code.
        is_dev = is_ide(
            self.current_window.get("title", ""),
            self.current_process_name,
            self.current_window.get("class_name", ""),
        )
        if is_dev:
            # Extract project name from editor title e.g. "main.py - saturn - Visual Studio Code"
            raw_title = self.current_window.get("title", "")
            project = ""
            if " - " in raw_title:
                parts = [p.strip() for p in raw_title.split(" - ")]
                project = parts[-2] if len(parts) >= 2 else parts[0]
            if project and project != self._last_dev_project and (now - self._last_alert_time.get("dev_session", 0) > 2700):
                self._last_alert_time["dev_session"] = now
                self._last_dev_project = project
                self._emit_alert(
                    "dev_session",
                    {
                        "message": f"Development workspace active: {project}",
                        "speech": f"I see you opened project {project}, sir. Git tracking and dev sentinels are standing by.",
                        "project": project
                    }
                )

        # Proactive: First Morning Interaction & Briefing Prompt
        now_dt = datetime.now()
        if 6 <= now_dt.hour <= 11:
            today_str = now_dt.strftime("%Y-%m-%d")
            if today_str != self._last_morning_briefing_date and self.user_idle_seconds < 3.0:
                self._last_morning_briefing_date = today_str
                self._emit_alert(
                    "morning_greeting",
                    {
                        "message": f"Good morning! First morning interaction detected ({now_dt.strftime('%A')}).",
                        "speech": f"Good morning, sir. Today is {now_dt.strftime('%A')}. Systems are fully online and standing by.",
                        "action": "daily_briefing"
                    }
                )

        # Alert: Deep Work Strain (Continuous active work on single task > 90 mins)
        if self.current_dwell_seconds > 5400 and self.user_idle_seconds < 120:
            if now - self._last_alert_time.get("flow_strain", 0) > 3600:  # 60 min cooldown
                self._last_alert_time["flow_strain"] = now
                self._emit_alert(
                    "flow_strain",
                    {
                        "message": f"You've been focused on {self.current_process_name} for over 90 minutes.",
                        "speech": f"Sir, you have been continuously working on {self.current_process_name} for over 90 minutes. I recommend saving your session and taking a short break.",
                        "task": self.current_process_name,
                        "dwell_seconds": self.current_dwell_seconds,
                    },
                )

        # Proactive: User Return after extended absence (> 5 mins away)
        if hasattr(self, "_was_away") and self._was_away and self.user_idle_seconds < 2.0:
            self._was_away = False
            if now - self._last_alert_time.get("welcome_back", 0) > 600:
                self._last_alert_time["welcome_back"] = now
                self._emit_alert(
                    "welcome_back",
                    {
                        "message": f"User returned to workspace ({self.current_process_name}).",
                        "speech": "Welcome back, sir. Your workspace is active and standing by.",
                        "task": self.current_process_name,
                    },
                )
        elif self.user_idle_seconds > 300:
            self._was_away = True

    def _emit_alert(self, alert_type: str, metadata: Dict[str, Any]):
        for cb in self._interjection_callbacks:
            try:
                cb(alert_type, metadata)
            except Exception:
                pass


# Global singleton daemon instance
sensorium = PassiveSensorium()


def get_sensorium() -> PassiveSensorium:
    """The one daemon every consumer reads telemetry from.

    An accessor rather than `from core.sensorium import sensorium` because that
    name means two different things depending on where you stand: the module is
    `core.sensorium` and the instance inside it is also `sensorium`. Both
    main.py's prompt builder and actions/proactive.py import this function, and
    both used to fail on a name that had never existed here - silently, inside a
    bare `except Exception`, which is how the ambient context stayed out of every
    prompt for the whole life of the feature."""
    return sensorium
