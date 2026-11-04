"""
agent/actions/creative.py
=========================
Image synthesis and reverse engineering. Both hand a fully-formed request to
an ``actions/`` worker on a daemon thread and report through the task
workspace.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


@register("generate_image")
def handle_generate_image(ctx: ActionContext) -> bool:
    prompt = str(ctx.param("prompt") or ctx.text)
    style = str(ctx.param("style") or "photorealistic")

    ctx.speak(ctx.ack("Generating your image now, sir..."))
    ctx.ui_call(
        "begin_task_workspace",
        f"Image Synthesis: {prompt[:25]}",
        [
            "Refining neural diffusion prompt",
            "Synthesizing high-res image",
            "Saving to Desktop",
            "Rendering HUD preview",
        ],
        source=ctx.source or "local",
    )

    def _run() -> None:
        try:
            from actions.image_generator import generate_image

            ctx.log(
                generate_image(
                    {"prompt": prompt, "style": style},
                    player=ctx.app.ui,
                    speak=ctx.speak,
                )
            )
            ctx.ui_call(
                "update_task_workspace",
                status="Render Complete",
                output="Saved to Desktop/Saturn_Images",
                percent=100,
            )
        except Exception as exc:
            ctx.log(f"ERR: Image generation failed: {exc}")
            ctx.speak(f"Image generation encountered an error: {exc}")

    ctx.spawn(_run, name="image-gen-worker")
    return True


@register("reverse_engineer")
def handle_reverse_engineer(ctx: ActionContext) -> bool:
    target = str(ctx.param("target") or ctx.text)
    re_action = str(ctx.action or ctx.param("action") or "analyze")
    feature = str(ctx.param("feature") or "")
    destination = str(ctx.param("destination") or "")

    ctx.speak(ctx.ack("Initiating reverse engineering..."))
    ctx.ui_call(
        "begin_task_workspace",
        f"Reverse Engineering: {target[:30]}",
        [
            "Connecting to target",
            "Inspecting DOM / Binary layout",
            "Extracting API surface & schemas",
            "Synthesizing output",
        ],
        source=ctx.source or "local",
    )

    def _run() -> None:
        try:
            from actions.reverse_engineer import reverse_engineer

            result = reverse_engineer(
                {
                    "target": target,
                    "action": re_action,
                    "feature": feature,
                    "destination": destination,
                },
                player=ctx.app.ui,
                speak=ctx.speak,
            )
            ctx.log(result)
            ctx.ui_call(
                "update_task_workspace",
                status="Complete",
                output=str(result)[:300],
                percent=100,
            )
        except Exception as exc:
            ctx.log(f"ERR: Reverse engineering failed: {exc}")
            ctx.speak(f"Reverse engineering encountered an error: {exc}")

    ctx.spawn(_run, name="reverse-engineer-worker")
    return True
