"""
features/game_sentinel.py
Hot-loaded dynamic feature for Saturn: Game Sentinel & Hardware Turbo Boost.
"""

from typing import Any, Dict
from core.game_sentinel import get_game_sentinel_manager

FEATURE_METADATA = {
    "name": "game_sentinel",
    "description": (
        "Game Sentinel & Hardware Turbo Boost: Detects running games in real time, purges standby RAM "
        "like Mem Reduct, elevates game process priority to High, switches Windows power plan to "
        "Ultimate Performance, and silences non-critical notifications."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'start' (enable game mode), 'stop' (disable game mode), 'clean_ram' (purge RAM), 'status'."
            },
            "game_name": {
                "type": "STRING",
                "description": "Name of game (optional)."
            }
        },
        "required": ["action"]
    },
    "version": "1.0.0",
    "active": True
}


def execute(**kwargs: Any) -> Dict[str, Any]:
    manager = get_game_sentinel_manager()
    action = str(kwargs.get("action", "clean_ram")).lower().strip()
    game_name = str(kwargs.get("game_name") or "Gaming Session").strip()

    if action in ("start", "enable", "on", "activate", "boost"):
        res = manager.activate_game_mode(game_name=game_name, manual=True, speak=False)
        return {
            "success": res.get("success", True),
            "action": "start",
            "game_name": game_name,
            "power_plan": res.get("power_plan"),
            "ram_cleaned": res.get("ram_cleaned"),
            "summary": res.get("message")
        }

    elif action in ("stop", "disable", "off", "deactivate", "exit"):
        res = manager.deactivate_game_mode(manual=True, speak=False)
        return {
            "success": res.get("success", False),
            "action": "stop",
            "summary": res.get("message")
        }

    elif action in ("clean_ram", "purge_ram", "mem_reduct", "free_ram"):
        res = manager.clean_ram_now(speak=False)
        return {
            "success": res.get("success", True),
            "action": "clean_ram",
            "stats": res.get("stats"),
            "summary": res.get("message")
        }

    elif action in ("status", "telemetry"):
        st = manager.get_status()
        return {
            "success": True,
            "action": "status",
            "data": st,
            "summary": f"Game Sentinel active: {st.get('is_game_mode_active')}"
        }

    return {"success": False, "error": f"Unknown action '{action}'"}
