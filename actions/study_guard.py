"""
actions/study_guard.py
Saturn - StudyGuard & Focus Sentinel Action.
Wraps real-time webcam computer vision for study focus monitoring, drowsiness detection,
phone doomscroll prevention, and Pomodoro study flow tracking.
"""

from typing import Any, Dict, Optional
from core.study_guard import get_study_guard_manager

PLUGIN = {
    "name": "study_guard",
    "description": (
        "AI Study Sentinel & Focus Monitor: Uses the webcam to supervise study sessions. "
        "Alerts the user if they fall asleep (Eye Aspect Ratio drowsiness detection), pick up their phone "
        "(anti-doomscroll detection), or step away from their desk. Tracks Pomodoro focus time and "
        "generates an interactive performance report card on the Circuit HUD."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action to perform: 'start', 'stop', 'pause', 'resume', or 'status'."
            },
            "duration": {
                "type": "INTEGER",
                "description": "Session duration in minutes (e.g. 25, 45, 60). Defaults to 25."
            },
            "subject": {
                "type": "STRING",
                "description": "Subject or study topic (e.g. 'Math', 'Coding', 'Biology'). Defaults to 'Focus Sprint'."
            },
            "lofi": {
                "type": "BOOLEAN",
                "description": "Whether to auto-launch background Lo-Fi study music."
            },
            "timer_only": {
                "type": "BOOLEAN",
                "description": "If true, runs a Pomodoro focus timer without webcam vision or absence alerts."
            }
        },
        "required": ["action"]
    }
}


def run(parameters: Dict[str, Any], player: Any = None, session_memory: Any = None) -> str:
    """Executes StudyGuard actions."""
    manager = get_study_guard_manager()
    action = str(parameters.get("action", "start")).lower().strip()
    duration = int(parameters.get("duration") or 25)
    subject = str(parameters.get("subject") or "Focus Sprint").strip()
    lofi = bool(parameters.get("lofi", False))
    timer_only = bool(parameters.get("timer_only", False))

    speak_cb = getattr(player, "speak", None) if player else None

    if action in ("start", "begin", "launch"):
        res = manager.start_session(
            duration_minutes=duration,
            subject=subject,
            play_lofi=lofi,
            timer_only=timer_only,
            speak_cb=speak_cb,
        )
        if res.get("success"):
            desc = "Pomodoro timer active (camera-free)" if timer_only else "Webcam sentinel is monitoring your focus"
            return f"StudyGuard activated for {duration} minutes ({subject}). {desc}."
        if res.get("no_camera"):
            return f"StudyGuard Error: {res.get('message')}"
        return f"StudyGuard error: {res.get('message')}"

    elif action in ("stop", "end", "finish"):
        res = manager.stop_session()
        if res.get("success"):
            stats = res.get("stats", {})
            return (
                f"Study session finished! Total time: {int(stats.get('total_seconds', 0) // 60)} minutes. "
                f"Focus Score: {stats.get('focus_score')}%. "
                f"Alerts: {stats.get('sleep_alerts', 0)} sleep warnings, {stats.get('phone_alerts', 0)} phone distractions."
            )
        return f"StudyGuard error: {res.get('message')}"

    elif action in ("pause", "hold"):
        res = manager.pause_session()
        return res.get("message", "Study session paused.")

    elif action in ("resume", "continue"):
        res = manager.resume_session()
        return res.get("message", "Study session resumed.")

    elif action in ("status", "stats", "info"):
        st = manager.get_status()
        if not st.get("is_active"):
            return "No study session is currently running."
        stats = st.get("stats", {})
        return (
            f"Active Session: '{stats.get('subject')}' | "
            f"Focus Score: {stats.get('focus_score')}% | "
            f"Elapsed: {int(stats.get('total_seconds', 0) // 60)}m | "
            f"Alerts: {stats.get('sleep_alerts', 0)} sleep, {stats.get('phone_alerts', 0)} phone."
        )

    return f"Unknown StudyGuard action '{action}'."
