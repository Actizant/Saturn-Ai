"""
features/study_guard.py
Hot-loaded dynamic feature for Saturn: StudyGuard & Focus Sentinel.
"""

from typing import Any, Dict
from core.study_guard import get_study_guard_manager

FEATURE_METADATA = {
    "name": "study_guard",
    "description": (
        "StudyGuard & Focus Sentinel: Webcam AI study supervisor. Detects drowsiness / falling asleep, "
        "mobile phone doomscrolling, and empty desk absence. Tracks Pomodoro focus flow and generates "
        "live HUD performance report cards."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'start' (begin session), 'stop' (finish session), 'pause', 'resume', 'status'."
            },
            "duration": {
                "type": "INTEGER",
                "description": "Target duration in minutes (e.g. 25, 45, 60)."
            },
            "subject": {
                "type": "STRING",
                "description": "Study topic or subject."
            },
            "lofi": {
                "type": "BOOLEAN",
                "description": "Whether to auto-launch background Lo-Fi study music."
            }
        },
        "required": ["action"]
    },
    "version": "1.0.0",
    "active": True
}


def execute(**kwargs: Any) -> Dict[str, Any]:
    manager = get_study_guard_manager()
    action = str(kwargs.get("action", "start")).lower().strip()
    duration = int(kwargs.get("duration") or 25)
    subject = str(kwargs.get("subject") or "Focus Sprint").strip()
    lofi = bool(kwargs.get("lofi", False))

    if action in ("start", "begin", "launch"):
        res = manager.start_session(
            duration_minutes=duration,
            subject=subject,
            play_lofi=lofi
        )
        return {
            "success": res.get("success", False),
            "action": "start",
            "message": res.get("message", ""),
            "summary": f"StudyGuard activated for {duration} minutes ({subject}). Webcam sentinel is monitoring your focus.",
            "voice_summary": f"StudyGuard activated for {duration} minutes, sir. I'll make sure you stay awake and off your phone.",
        }

    elif action in ("stop", "end", "finish"):
        res = manager.stop_session()
        stats = res.get("stats", {})
        return {
            "success": res.get("success", False),
            "action": "stop",
            "stats": stats,
            "summary": res.get("summary", "Study session concluded."),
            "voice_summary": f"Session finished, sir! Your focus efficiency was {stats.get('focus_score', 100)} percent.",
        }

    elif action in ("pause", "hold"):
        res = manager.pause_session()
        return {"success": res.get("success", False), "action": "pause", "summary": res.get("message", "")}

    elif action in ("resume", "continue"):
        res = manager.resume_session()
        return {"success": res.get("success", False), "action": "resume", "summary": res.get("message", "")}

    elif action in ("status", "stats", "info"):
        st = manager.get_status()
        return {"success": True, "action": "status", "data": st}

    return {"success": False, "error": f"Unknown action: {action}"}
