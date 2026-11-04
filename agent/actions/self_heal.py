"""
agent/actions/self_heal.py
==========================
Saturn's own repair shop: the patch log, rolling one back, and asking for a fix.

Two intents, not four. "show patches" reads; "heal yourself", "undo last patch"
and "trigger test bug" all end in a source file being written - the last one
because the demo bug is meant to be caught and immediately patched - so they are
one capability and one decision about who may do it.

Nothing here speaks first. ``auto_heal`` and ``heal_traceback`` narrate the whole
operation themselves ("bringing the terminal forward", the diagnosis, the diff,
the commit); a handler sentence in front of that is a third voice.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


# =============================================================================
# READ - what has been patched
# =============================================================================

@register("self_heal_report")
def handle_self_heal_report(ctx: ActionContext) -> bool:
    action = str(ctx.action or "history").lower()
    if action not in ("history", "status"):
        return False

    def _run() -> None:
        try:
            from actions.auto_heal_engine import auto_heal

            result = auto_heal(
                {"action": action},
                player=getattr(ctx.app, "ui", None),
                speak=ctx.speak,
            )
            ctx.log(f"[AutoHeal] {result}")
            ctx.ui_call("finish_task_workspace", result, "Auto-heal report ready.", 100)
        except Exception as exc:
            ctx.log(f"ERR: Auto-heal report failed: {exc}")
            ctx.ui_call("finish_task_workspace", str(exc), "Auto-heal report failed", 0)
            ctx.speak("I could not read my own patch history just now, sir.")

    ctx.spawn(_run, name="self-heal-report")
    return True


# =============================================================================
# WRITE - patch, roll back, or prove the repair path still works
# =============================================================================

def _repair(ctx: ActionContext, action: str, done: str, thread: str) -> None:
    def _run() -> None:
        try:
            from actions.auto_heal_engine import auto_heal

            result = auto_heal(
                {"action": action},
                player=getattr(ctx.app, "ui", None),
                speak=ctx.speak,
            )
            ctx.log(f"[AutoHeal] {result}")
            ctx.ui_call("finish_task_workspace", result, done, 100)
        except Exception as exc:
            ctx.log(f"ERR: Self-repair failed: {exc}")
            ctx.ui_call("finish_task_workspace", str(exc), "Self-repair failed", 0)
            ctx.speak("The repair itself failed, sir. I have left the code untouched.")

    ctx.spawn(_run, name=thread)


def _trigger_and_heal(ctx: ActionContext) -> None:
    """Raise a known bug on purpose, then heal it - the original demo path."""

    def _run() -> None:
        try:
            from actions.test_action import test_action

            result = test_action({}, player=getattr(ctx.app, "ui", None), speak=ctx.speak)
            ctx.log(f"[TestAction] {result}")
            ctx.speak(f"Test action executed with no errors: {result}. The code is currently healthy.")
            return
        except Exception:
            import traceback

            traceback_text = traceback.format_exc()

        try:
            from actions.auto_heal_engine import AutoHealEngine

            AutoHealEngine.record_last_error(traceback_text)
            AutoHealEngine.heal_traceback(
                traceback_text,
                player=getattr(ctx.app, "ui", None),
                speak=ctx.speak,
                module_name="test_action",
            )
        except Exception as exc:
            ctx.log(f"ERR: Heal after test bug failed: {exc}")
            ctx.speak("I found the bug but could not repair it, sir.")

    ctx.spawn(_run, name="self-heal-trigger")


@register("self_heal_repair")
def handle_self_heal_repair(ctx: ActionContext) -> bool:
    action = str(ctx.action or "heal").lower()

    if action == "heal":
        _repair(ctx, "heal", "Self-patching complete.", "self-heal-patch")
        return True

    if action == "rollback":
        _repair(ctx, "rollback", "Rollback complete.", "self-heal-rollback")
        return True

    if action == "trigger_bug":
        _trigger_and_heal(ctx)
        return True

    return False
