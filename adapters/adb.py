"""Android over USB or Wi-Fi: phones, Android TV, TV boxes.

ADB is the cheapest way to actually *use* a device rather than poke it: it can
launch an app, type, press buttons, take a screenshot and read the window that
is in front. A device entry needs one line::

    "adapter": "adb", "address": {"serial": "192.168.1.31:5555"}

``serial`` may be omitted when exactly one device is attached.

WHAT THIS DELIBERATELY CANNOT DO
    ``lock.unlock``. Unlocking a phone over ADB means typing the PIN, and the
    moment Saturn holds a PIN it becomes the thing an attacker wants. The
    refusal is part of the design, not a gap.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Tuple

from adapters import Adapter, failure, register

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_TIMEOUT = 12.0

_KEYCODES = {
    "home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "enter": "KEYCODE_ENTER",
    "menu": "KEYCODE_MENU", "recents": "KEYCODE_APP_SWITCH",
    "wake": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP", "power": "KEYCODE_POWER",
    "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE",
    "play": "KEYCODE_MEDIA_PLAY", "pause": "KEYCODE_MEDIA_PAUSE",
    "next": "KEYCODE_MEDIA_NEXT", "previous": "KEYCODE_MEDIA_PREVIOUS",
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER",
}

_SPECIAL = set('()<>|;&*\\~"\'`$')


class Unsupported(Exception):
    """The capability is understood, and is deliberately not done this way."""


def _find_adb() -> str:
    found = shutil.which("adb")
    if found:
        return found
    for root in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 os.environ.get("LOCALAPPDATA", "") + r"\Android\Sdk"):
        if not root:
            continue
        candidate = os.path.join(root, "platform-tools", "adb.exe" if os.name == "nt" else "adb")
        if os.path.exists(candidate):
            return candidate
    return ""


def escape_text(text: str) -> str:
    """``adb shell input text`` wants spaces as %s and shell metacharacters escaped."""
    out: List[str] = []
    for char in str(text):
        if char == " ":
            out.append("%s")
        elif char in _SPECIAL:
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out)


class AdbAdapter(Adapter):
    name = "adb"
    summary = "Android over USB or Wi-Fi: phones, Android TV, TV boxes"
    hint = "adb is not on PATH (install platform-tools)"

    @property
    def available(self) -> bool:
        return bool(_find_adb())

    # ── plumbing ────────────────────────────────────────────────────────

    def _run(self, device: Any, args: List[str], timeout: float = DEFAULT_TIMEOUT
             ) -> Tuple[int, str, str]:
        exe = _find_adb()
        if not exe:
            raise FileNotFoundError(self.hint)
        serial = str((getattr(device, "address", None) or {}).get("serial") or "")
        command = [exe] + (["-s", serial] if serial else []) + list(args)
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout, creationflags=_NO_WINDOW)
        return (completed.returncode or 0,
                (completed.stdout or "").strip(),
                (completed.stderr or "").strip())

    def send(self, device: Any, capability: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params or {})

        if capability == "screen.capture":
            return self._screenshot(device)

        try:
            args, note = self._command(capability, params)
        except Unsupported as exc:
            return failure("UNSUPPORTED", f"{device.name}: {exc}")
        except KeyError as exc:
            return failure("BAD_REQUEST", f"{device.name}: {capability} needs {exc}")

        try:
            code, out, err = self._run(device, args)
        except FileNotFoundError:
            return failure("ADAPTER_UNAVAILABLE", self.hint, hint=self.hint)
        except subprocess.TimeoutExpired:
            return failure("TIMEOUT", f"{device.name} did not answer in time.")
        except Exception as exc:
            return failure("ADAPTER_FAILED", str(exc))

        ok = code == 0
        detail = note or f"{device.name} {capability}"
        if not ok:
            return failure("COMMAND_FAILED", f"{detail}: {err or out or f'exit {code}'}")
        return {"ok": True, "detail": detail, "command": " ".join(args), "output": out[:400]}

    # ── capability table ────────────────────────────────────────────────

    def _command(self, capability: str, params: Dict[str, Any]) -> Tuple[List[str], str]:
        if capability == "app.launch":
            package = str(params["package"]).strip()
            return (["shell", "monkey", "-p", package,
                     "-c", "android.intent.category.LAUNCHER", "1"],
                    f"opened {package}")
        if capability == "text.input":
            return (["shell", "input", "text", escape_text(params["text"])], "typed")
        if capability == "key.press":
            key = str(params["key"]).strip().lower()
            code = _KEYCODES.get(key) or key.upper()
            if not code.startswith("KEYCODE_"):
                code = f"KEYCODE_{code}"
            return ["shell", "input", "keyevent", code], f"pressed {key}"
        if capability == "display.show":
            content = str(params["content"])
            return (["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", content],
                    f"showing {content}")
        if capability == "notify.send":
            return (["shell", "cmd", "notification", "post", "-t", "Saturn",
                     "saturn", str(params["text"])], "notified")
        if capability == "switch.on":
            return ["shell", "input", "keyevent", "KEYCODE_WAKEUP"], "woke the screen"
        if capability == "switch.off":
            return ["shell", "input", "keyevent", "KEYCODE_SLEEP"], "slept the screen"
        if capability.startswith("media."):
            verb = capability.split(".", 1)[1]
            key = {"play": "play", "pause": "pause", "next": "next",
                   "previous": "previous"}.get(verb)
            if not key:
                raise Unsupported(f"{capability} has no ADB mapping")
            return (["shell", "input", "keyevent", _KEYCODES[key]], f"media {verb}")
        if capability == "lock.lock":
            return ["shell", "input", "keyevent", "KEYCODE_SLEEP"], "slept the screen"
        if capability == "lock.unlock":
            raise Unsupported(
                "unlocking a phone over ADB would mean holding your PIN, so I won't")
        raise Unsupported(f"{capability} has no ADB mapping")

    def _screenshot(self, device: Any) -> Dict[str, Any]:
        exe = _find_adb()
        if not exe:
            return failure("ADAPTER_UNAVAILABLE", self.hint, hint=self.hint)
        serial = str((getattr(device, "address", None) or {}).get("serial") or "")
        command = [exe] + (["-s", serial] if serial else []) + ["exec-out", "screencap", "-p"]
        try:
            completed = subprocess.run(command, capture_output=True,
                                       timeout=DEFAULT_TIMEOUT, creationflags=_NO_WINDOW)
        except Exception as exc:
            return failure("ADAPTER_FAILED", str(exc))
        data = completed.stdout or b""
        if (completed.returncode or 0) != 0 or not data:
            return failure("COMMAND_FAILED",
                           f"{device.name} screenshot failed: "
                           f"{(completed.stderr or b'').decode('utf-8', 'ignore').strip()[:200]}")
        try:
            from core.user_paths import get_user_data_dir

            target = get_user_data_dir() / "screenshots"
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{device.id}-{int(time.time())}.png"
            path.write_bytes(data)
        except Exception as exc:
            return failure("SAVE_FAILED", str(exc))
        return {"ok": True, "detail": f"{device.name} screenshot saved",
                "path": str(path), "bytes": len(data)}


register(AdbAdapter())
