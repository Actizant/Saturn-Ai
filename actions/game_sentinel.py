"""
actions/game_sentinel.py
Saturn - Game Sentinel & Hardware Turbo Boost Action.
Provides high-performance hardware tuning, Mem Reduct RAM purging, process priority elevation,
and automatic Windows power scheme switching for gaming.
"""

from typing import Any, Dict
from core.game_sentinel import get_game_sentinel_manager

PLUGIN = {
    "name": "game_sentinel",
    "description": (
        "Game Sentinel & Hardware Turbo Boost: Detects running games, purges standby RAM like Mem Reduct, "
        "elevates game process priorities to High, switches Windows power plan to Ultimate Performance, "
        "and silences non-critical notifications. Supports actions: 'start' (enable game mode), 'stop' (disable game mode), "
        "'clean_ram' (instant Mem Reduct RAM purge), 'status' (telemetry)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action to execute: 'start', 'stop', 'clean_ram', 'boost', or 'status'."
            },
            "game_name": {
                "type": "STRING",
                "description": "Optional custom name of the game being launched."
            }
        },
        "required": ["action"]
    }
}


def run(parameters: Dict[str, Any], player: Any = None, session_memory: Any = None) -> str:
    """Executes Game Sentinel actions."""
    manager = get_game_sentinel_manager()
    action = str(parameters.get("action", "clean_ram")).lower().strip()
    game_name = str(parameters.get("game_name") or "Gaming Session").strip()

    speak_cb = getattr(player, "speak", None) if player else None

    if action in ("start", "enable", "on", "activate", "boost"):
        res = manager.activate_game_mode(game_name=game_name, manual=True, speak=False)
        ram_freed = res.get("ram_cleaned", {}).get("freed_mb", 0)
        return (
            f"Game Mode & Turbo Boost Activated for '{game_name}'! "
            f"RAM purged: {ram_freed} MB freed. Process priority set to HIGH, "
            f"Power plan: {res.get('power_plan')}."
        )

    elif action in ("stop", "disable", "off", "deactivate", "exit"):
        res = manager.deactivate_game_mode(manual=True, speak=False)
        if not res.get("success"):
            return res.get("message", "Game Mode is not currently active.")
        return f"Game Mode Deactivated. {res.get('message')}"

    elif action in ("clean_ram", "purge_ram", "mem_reduct", "free_ram", "clear_ram"):
        res = manager.clean_ram_now(speak=False)
        stats = res.get("stats", {})
        return (
            f"Mem Reduct RAM purge completed: Freed {stats.get('freed_mb', 0)} MB across "
            f"{stats.get('trimmed_processes', 0)} processes. Usage: {stats.get('before_percent', 0)}% -> {stats.get('after_percent', 0)}%."
        )

    elif action in ("status", "telemetry", "info"):
        st = manager.get_status()
        active_str = "ACTIVE" if st.get("is_game_mode_active") else "IDLE"
        game_str = f" ({st.get('active_game_name')})" if st.get("active_game_name") else ""
        return (
            f"Game Sentinel: {active_str}{game_str} | "
            f"RAM Usage: {st.get('ram_percent')}% ({st.get('ram_used_mb')} MB / {st.get('ram_available_mb')} MB free)."
        )

    return f"Unknown Game Sentinel action '{action}'."
