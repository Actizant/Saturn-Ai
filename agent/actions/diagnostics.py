"""
agent/actions/diagnostics.py
============================
The machine's own vitals: memory, battery, the full hardware report, screen
brightness, and terminating a process.

Three intents rather than one, because the trust gate sees an intent *name* and
never an action. "How much RAM is Chrome using" only reads; "dim the screen"
changes something a keypress can undo; "kill Chrome" ends a program and whatever
was unsaved inside it. One intent could not be all three, so it is not.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


def _ask_diagnostics(
    ctx: ActionContext, parameters: dict, done: str, thread: str
) -> None:
    """Run one diagnostics request off the turn thread and report both ways.

    The action module speaks its own summary ("Your RAM is at 71 percent...").
    That is the answer, not a second acknowledgement, so ``speak`` is passed
    through rather than suppressed.
    """
    def _run() -> None:
        try:
            from actions.system_diagnostics_mcp import system_diagnostics

            result = system_diagnostics(
                dict(parameters),
                player=getattr(ctx.app, "ui", None),
                speak=ctx.speak,
            )
            ctx.log(f"[Diagnostics] {result}")
            ctx.ui_call("finish_task_workspace", result, done, 100)
        except Exception as exc:
            ctx.log(f"ERR: System diagnostics failed: {exc}")
            ctx.ui_call("finish_task_workspace", str(exc), "Diagnostics unavailable", 0)
            ctx.speak("I could not read the system just now, sir.")

    ctx.spawn(_run, name=thread)


# =============================================================================
# READ - memory, power, the whole report
# =============================================================================

@register("system_status")
def handle_system_status(ctx: ActionContext) -> bool:
    action = str(ctx.action or "status").lower()

    if action in ("ram", "ram_hogs", "memory"):
        ctx.speak(ctx.ack("Scanning memory consumers..."))
        _ask_diagnostics(ctx, {"action": "ram_hogs"}, "Memory analysis complete.", "diag-ram")
        return True

    if action in ("cpu", "cpu_hogs", "processor"):
        ctx.speak(ctx.ack("Checking processor load..."))
        _ask_diagnostics(ctx, {"action": "cpu_hogs"}, "CPU analysis complete.", "diag-cpu")
        return True

    if action in ("battery", "power"):
        _ask_diagnostics(ctx, {"action": "battery"}, "Battery check complete.", "diag-battery")
        return True

    if action in ("status", "report", "diagnostics"):
        ctx.speak(ctx.ack("Retrieving system diagnostics report..."))
        _ask_diagnostics(ctx, {"action": "status"}, "System report ready.", "diag-status")
        return True

    return False


# =============================================================================
# CHANGE - screen brightness
# =============================================================================

@register("brightness_control")
def handle_brightness(ctx: ActionContext) -> bool:
    action = str(ctx.action or "brightness").lower()
    if action not in ("brightness", "set_brightness", "dim", "brighten"):
        return False

    parameters: dict = {"action": "brightness"}
    level = ctx.param("level")
    if level is not None:
        # No acknowledgement: "dim screen" should be dark by the time it would
        # have finished saying "dimming the screen".
        parameters["level"] = int(level)
        parameters["relative"] = bool(ctx.param("relative", False))

    _ask_diagnostics(ctx, parameters, "Brightness adjusted.", "diag-brightness")
    return True


# =============================================================================
# END SOMETHING - terminate a process
# =============================================================================

@register("process_kill")
def handle_process_kill(ctx: ActionContext) -> bool:
    action = str(ctx.action or "kill").lower()
    if action not in ("kill", "terminate", "close_app", "end_process"):
        return False

    target = str(ctx.param("target") or "").strip()
    if not target:
        ctx.speak("Which program should I close, sir?")
        return True

    ctx.speak(ctx.ack(f"Closing {target}, sir..."))
    _ask_diagnostics(
        ctx,
        {"action": "kill", "target": target, "force": bool(ctx.param("force", False))},
        "Process management complete.",
        "diag-kill",
    )
    return True
