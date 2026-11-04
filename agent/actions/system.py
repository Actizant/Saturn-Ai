"""
agent/actions/system.py
=======================
Local machine health: Game Sentinel (power plan + turbo + RAM purge) and the
Storage Sentinel disk audit.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


@register("game_sentinel")
def handle_game_sentinel(ctx: ActionContext) -> bool:
    action = str(ctx.action or "clean_ram").lower()
    game = str(ctx.param("game_name") or "Gaming Session")

    def _run() -> None:
        try:
            from core.game_sentinel import get_game_sentinel_manager

            manager = get_game_sentinel_manager()

            if action in ("start", "enable", "on", "activate", "boost"):
                ctx.ui_call(
                    "begin_task_workspace",
                    f"Game Sentinel: {game}",
                    [
                        "Engage Ultimate Performance Power Plan",
                        "Elevate game process thread priority to High",
                        "Execute Mem Reduct deep memory & standby purge",
                        "Silence non-critical desktop notifications",
                    ],
                    source=ctx.source or "local",
                )
                result = manager.activate_game_mode(game_name=game, manual=True, speak=False)
                ram_freed = (result.get("ram_cleaned") or {}).get("freed_mb", 0)
                ctx.ui_call(
                    "update_task_workspace",
                    status="Turbo Boost Engaged",
                    output=(
                        f"RAM Purged: {ram_freed} MB | Power: {result.get('power_plan')} "
                        "| Priority: HIGH"
                    ),
                    percent=100,
                )
                ctx.speak(result.get("message"))

            elif action in ("stop", "disable", "off", "deactivate", "exit"):
                result = manager.deactivate_game_mode(manual=True, speak=False)
                if result.get("success"):
                    ctx.ui_call(
                        "update_task_workspace", status="Game Mode Disengaged", percent=100
                    )
                    ctx.speak(result.get("message"))
                else:
                    ctx.speak("Game mode is not currently active, sir.")

            elif action in (
                "clean_ram",
                "purge_ram",
                "mem_reduct",
                "free_ram",
                "clear_ram",
            ):
                ctx.ui_call(
                    "begin_task_workspace",
                    "Mem Reduct RAM Cleaner",
                    [
                        "Query system working sets & processes",
                        "Empty process working sets via K32EmptyWorkingSet",
                        "Purge Windows Standby List & Modified Page List",
                        "Calculate freed physical memory",
                    ],
                    source=ctx.source or "local",
                )
                result = manager.clean_ram_now(speak=False)
                stats = result.get("stats", {})
                ctx.ui_call(
                    "finish_task_workspace",
                    result=result.get("message"),
                    status=(
                        f"Freed {stats.get('freed_mb', 0)} MB "
                        f"({stats.get('before_percent')}% -> {stats.get('after_percent')}%)"
                    ),
                    percent=100,
                )
                ctx.speak(result.get("message"))

            elif action in ("status", "telemetry"):
                status = manager.get_status()
                state = "Active" if status.get("is_game_mode_active") else "Idle"
                name = (
                    f" for '{status.get('active_game_name')}'"
                    if status.get("active_game_name")
                    else ""
                )
                ctx.speak(
                    f"Game Sentinel is {state}{name}. RAM usage is at "
                    f"{status.get('ram_percent')} percent."
                )

        except Exception as exc:
            ctx.log(f"ERR: Game Sentinel failed: {exc}")
            ctx.speak(f"Game Sentinel encountered an issue: {exc}")

    ctx.spawn(_run, name="game-sentinel")
    return True


@register("space_cleaner")
def handle_space_cleaner(ctx: ActionContext) -> bool:
    ctx.speak(ctx.ack("Initializing Saturn Storage Sentinel..."))

    def _run() -> None:
        try:
            from core.space_cleaner import get_space_cleaner

            cleaner = get_space_cleaner()

            # Prefer the in-app HUD overlay; fall back to the standalone window.
            if ctx.has_ui("show_space_cleaner_hud"):
                ctx.ui_call("show_space_cleaner_hud")
            else:
                from core.space_cleaner_hud import show_space_cleaner_hud

                show_space_cleaner_hud()

            ctx.ui_call(
                "begin_task_workspace",
                "Storage Sentinel: Interactive Disk Audit",
                [
                    "Project Radial Sunburst Storage Topography",
                    "Audit System Temp, Browser Shader Caches & Crash Logs",
                    "Calculate Safe Reclaimable Clusters",
                    "Safely Purge Volatile Bloat",
                ],
                source=ctx.source or "local",
            )

            telemetry = cleaner.get_disk_telemetry()
            drives = telemetry.get("drives", [])
            primary = drives[0] if drives else {}
            free_gb = primary.get("free_gb", 0)
            total_gb = primary.get("total_gb", 0)
            percent_used = primary.get("percent_used", 0)

            ctx.ui_call(
                "update_task_workspace",
                status=f"{free_gb} GB Free / {total_gb} GB Total",
                output=(
                    f"Storage Sentinel active. Primary disk has {free_gb} GB free "
                    f"({100 - percent_used:.1f}% available). Interactive Sunburst "
                    "scope ready."
                ),
                percent=100,
            )
        except Exception as exc:
            ctx.log(f"ERR: Storage Sentinel failed: {exc}")
            ctx.speak(f"Storage Sentinel encountered an issue: {exc}")

    ctx.spawn(_run, name="space-cleaner")
    return True
