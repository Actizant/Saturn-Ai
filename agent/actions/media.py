"""
agent/actions/media.py
======================
Media transport controls and meeting mode.

Both are pure local reflexes: they touch no network and finish in
milliseconds, so the media path runs on a short-lived daemon thread to keep
the UI thread free while the Win32 media keys settle.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


@register("media_control")
def handle_media_control(ctx: ActionContext) -> bool:
    action = ctx.action

    def _run() -> None:
        try:
            from actions.media_keys import (
                media_next,
                media_play_pause,
                media_previous,
                volume_down,
                volume_mute,
                volume_set,
                volume_up,
            )
        except Exception as exc:
            ctx.log(f"ERR: Media keys unavailable: {exc}")
            return

        if action == "volume_up":
            volume_up()
        elif action == "volume_down":
            volume_down()
        elif action == "volume_mute":
            volume_mute()
        elif action == "volume_set":
            try:
                value = int(ctx.param("value", 50))
            except (TypeError, ValueError):
                value = 50
            volume_set(value)
        elif action in {"media_pause", "media_play"}:
            media_play_pause()
        elif action == "media_next":
            media_next()
        elif action == "media_previous":
            media_previous()

        ctx.speak(ctx.voice_ack)

    ctx.spawn(_run, name="media-control")
    return True


@register("meeting_mode")
def handle_meeting_mode(ctx: ActionContext) -> bool:
    if ctx.action == "start":
        ctx.app._start_meeting_mode("Meeting mode activated.")
    else:
        ctx.app._stop_meeting_mode("Meeting mode closed.")
    return True
