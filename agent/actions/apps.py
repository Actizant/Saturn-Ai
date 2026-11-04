"""
agent/actions/apps.py
=====================
App launching, phone routing, and on-demand visual perception.

``vision`` had a defect worth recording: the branch built the
``_run_vision_process`` worker, painted a task workspace, and then simply
ended without ever starting the thread or returning. Saying "check my screen"
therefore did nothing and *also* fell through into the legacy chain, where the
LLM answered the same request a second time. The handler below starts the
worker and owns the turn.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


@register("open_app")
def handle_open_app(ctx: ActionContext) -> bool:
    app_name = str(ctx.param("app_name") or "").strip()
    if not app_name:
        # No resolvable app name: let the legacy launcher/LLM decide.
        return False

    from actions.open_app import open_app

    def _run() -> None:
        open_app({"app_name": app_name}, player=ctx.app.ui)
        ctx.speak(ctx.ack(f"Opening {app_name}, sir."))

    ctx.spawn(_run, name="open-local-app")
    return True


@register("mobile_connect")
def handle_mobile_connect(ctx: ActionContext) -> bool:
    if ctx.source == "instagram":
        return False
    return bool(
        ctx.app._handle_saturn_connect_command(
            ctx.text, source=ctx.source or "local", parsed_intent=ctx.intent
        )
    )


@register("vision")
def handle_vision(ctx: ActionContext) -> bool:
    angle = str(ctx.param("angle") or ("camera" if "cam" in ctx.action else "screen"))
    is_camera = angle == "camera"
    workspace_title = "Webcam Inspection" if is_camera else "Screen Analysis"

    ctx.ui_call(
        "update_task_workspace",
        title=workspace_title,
        command=ctx.text,
        plan=["Capture visual frame", "Analyze with vision AI", "Report observations"],
        status="Checking camera..." if is_camera else "Scanning screen...",
        output=(
            "Saturn is inspecting your camera..."
            if is_camera
            else "Saturn is inspecting your screen..."
        ),
        percent=40,
        source=ctx.source or "local",
    )

    # Screen capture must happen on the UI thread, before the worker starts.
    img_bytes = None
    if angle == "screen" and ctx.has_ui("capture_screen_bytes"):
        try:
            img_bytes = ctx.app.ui.capture_screen_bytes()
        except Exception as exc:
            ctx.log(f"ERR: Screen capture failed: {exc}")

    def _run() -> None:
        try:
            from actions.screen_processor import screen_process

            success = screen_process(
                parameters={"angle": angle, "text": ctx.text},
                response=None,
                player=ctx.app.ui,
                session_memory=None,
                image_bytes=img_bytes,
            )
        except Exception as exc:
            ctx.log(f"ERR: Visual perception failed: {exc}")
            success = False

        if not success:
            ctx.ui_call(
                "update_task_workspace",
                status=f"{workspace_title} failed",
                output="Visual perception could not complete. Check your camera or screen permissions.",
                percent=0,
            )

    ctx.spawn(_run, name="vision-worker")
    return True
