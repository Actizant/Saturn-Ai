"""
actions/media_keys.py
=====================
Hardware media keys: volume and transport control.

This module was referenced by both ``main.py``'s intent dispatcher and
``dashboard/server.py`` but never existed, so every "volume up" / "next track"
command died on ``ImportError`` — main.py swallowed it with a bare ``except:
pass`` and then crashed the worker thread on a ``NameError``. It is the single
implementation both call sites expect.

Volume delegates to :mod:`actions.computer_settings` so there is exactly one
cross-platform volume implementation. Transport keys use the Win32 virtual
key codes first (they work even when the media app is not focused) and fall
back to ``pyautogui``.
"""

from __future__ import annotations

import sys
import time

__all__ = [
    "volume_up",
    "volume_down",
    "volume_mute",
    "volume_set",
    "media_play_pause",
    "media_next",
    "media_previous",
]

# Windows virtual-key codes for the consumer media keys.
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3

_VK_MAP = {
    "playpause": _VK_MEDIA_PLAY_PAUSE,
    "nexttrack": _VK_MEDIA_NEXT_TRACK,
    "prevtrack": _VK_MEDIA_PREV_TRACK,
}

_KEYEVENTF_KEYUP = 0x0002


# =============================================================================
# VOLUME (shared with the OS settings actions)
# =============================================================================

def volume_up() -> None:
    from actions.computer_settings import volume_up as _impl

    _impl()


def volume_down() -> None:
    from actions.computer_settings import volume_down as _impl

    _impl()


def volume_mute() -> None:
    from actions.computer_settings import volume_mute as _impl

    _impl()


def volume_set(value: int) -> None:
    from actions.computer_settings import volume_set as _impl

    _impl(int(value))


# =============================================================================
# TRANSPORT
# =============================================================================

def _press_media_key(name: str) -> bool:
    """Tap a media key. Returns True when some backend accepted it."""
    key = (name or "").lower()

    if sys.platform == "win32":
        vk = _VK_MAP.get(key)
        if vk:
            try:
                import ctypes

                user32 = ctypes.windll.user32
                user32.keybd_event(vk, 0, 0, 0)
                time.sleep(0.04)
                user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
                return True
            except Exception as exc:
                print(f"[MediaKeys] keybd_event failed for {key}: {exc}")

    try:
        import pyautogui

        pyautogui.press(key)
        return True
    except Exception as exc:
        print(f"[MediaKeys] {key} unavailable: {exc}")
        return False


def media_play_pause() -> bool:
    return _press_media_key("playpause")


def media_next() -> bool:
    return _press_media_key("nexttrack")


def media_previous() -> bool:
    return _press_media_key("prevtrack")
