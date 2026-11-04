"""
memory/config_manager.py - Centralized configuration access for Saturn.
Handles persistent app settings, audio device selection, push-to-talk,
and AI options. Backed by config/app_settings.json.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict

from core.user_paths import get_user_data_dir

BSE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = get_user_data_dir() / "config"
SETTINGS_FILE = CONFIG_DIR / "app_settings.json"



def _ensure_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> Dict[str, Any]:
    _ensure_config()
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: Dict[str, Any]) -> None:
    _ensure_config()
    current = load_settings()
    current.update(data)
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=4)
    except Exception as e:
        print(f"[CONFIG] Error saving settings: {e}")


def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def set_setting(key: str, value: Any) -> None:
    save_settings({key: value})


# --- Audio Devices
 

def get_input_device() -> str:
    return str(get_setting("input_device", "") or "")


def set_input_device(name: str) -> None:
    set_setting("input_device", name)


def get_output_device() -> str:
    return str(get_setting("output_device", "") or "")


def set_output_device(name: str) -> None:
    set_setting("output_device", name)


# --- Push-to-Talk

def get_push_to_talk_enabled() -> bool:
    return bool(get_setting("push_to_talk_enabled", False))


def set_push_to_talk_enabled(enabled: bool) -> None:
    set_setting("push_to_talk_enabled", bool(enabled))


# --- Wake Word & Briefing

def get_wake_word_enabled() -> bool:
    return bool(get_setting("wake_word_enabled", True))


def save_wake_word_enabled(enabled: bool) -> None:
    set_setting("wake_word_enabled", bool(enabled))


def get_brief_enabled() -> bool:
    return bool(get_setting("brief_enabled", True))


# --- Voice Style (persona) & Language

def get_voice_style() -> str:
    """The active persona key (saturn / jarvis / eve / edith / classic).

    core/speech_style.py reads this to pick the assistant voice; a missing
    accessor used to make the persona knob silently dead.
    """
    return str(get_setting("voice_style", "") or "")


def set_voice_style(style: str) -> None:
    set_setting("voice_style", str(style or ""))


def get_preferred_language() -> str:
    return str(get_setting("preferred_language", "") or "")


def set_preferred_language(lang: str) -> None:
    set_setting("preferred_language", str(lang or ""))


# --- Music Vibe (Saturn's third ear)

def get_vibe_enabled() -> bool:
    """Whether Saturn listens to what plays on your machine and enjoys it.

    On by default: the knowing half (Sense A) is free and the feeling half
    (Sense B) only opens a loopback while music is actually playing, so an idle
    machine pays nothing. Off is a hard stop - no window scan, no loopback,
    no spoken reactions.
    """
    return bool(get_setting("vibe_enabled", True))


def set_vibe_enabled(enabled: bool) -> None:
    set_setting("vibe_enabled", bool(enabled))


def get_vibe_speak_enabled() -> bool:
    """Whether Saturn says a short line when a new track starts. The orb can
    still vibe with this off; it only mutes the spoken reactions."""
    return bool(get_setting("vibe_speak_enabled", True))


def set_vibe_speak_enabled(enabled: bool) -> None:
    set_setting("vibe_speak_enabled", bool(enabled))
