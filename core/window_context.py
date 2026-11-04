"""
Active Window & Foreground Application Context for Saturn.
Uses Windows Win32 APIs via ctypes to inspect the user's current focused application,
window title, and capture targeted screenshots of the active workspace.

Saturn's own windows are excluded
---------------------------------
``GetForegroundWindow`` answers "which window has focus", not "what is the user
working on". Saturn's orb, HUDs and command bar are always-on-top Qt windows, so
as soon as one of them is clicked - or steals focus when it is shown - the two
questions stop having the same answer. That is how Saturn came to describe its
own window when asked "what am I doing?".

:func:`get_active_user_window` therefore returns the user's window: the real
foreground when it is not ours, and otherwise the topmost non-Saturn window from
the desktop Z-order (the same order Alt+Tab lists), falling back to the last
window we know the user actually worked in.
"""

import os
import sys
import ctypes
from ctypes import wintypes
from typing import Any, Dict, Iterator, Optional, Set

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


# Handle-returning Win32 functions default to a C ``int`` return type, which
# truncates a 64-bit HWND. Declared once here rather than at every call site.
_GW_HWNDNEXT = 2
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080

if user32:
    try:
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetTopWindow.argtypes = [wintypes.HWND]
        user32.GetTopWindow.restype = wintypes.HWND
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
    except Exception:
        # A partially declared API is not worth a crash: the plain calls below
        # still work on 32-bit handles.
        pass


# Desktop, taskbar and shell surfaces. They are always present and always
# visible, so they would win every Z-order scan.
_SHELL_CLASSES = frozenset({
    "progman",
    "workerw",
    "shell_traywnd",
    "shell_secondarytraywnd",
    "windows.ui.core.corewindow",
    "applicationframewindow",
    "ime",
    "msctfime ui",
    "default ime",
})

#: Pids known to belong to Saturn (e.g. a window host started for a sub-engine).
_OWN_PIDS: Set[int] = set()
#: Image paths of a packaged build, resolved once on first use.
_FROZEN_PIDS: Optional[Set[int]] = None
#: The last window that was definitely the user's, so an instant where every
#: scan fails still answers with something true a moment ago instead of nothing.
_LAST_USER_WINDOW: Dict[str, Any] = {}


# =============================================================================
# OWNERSHIP
# =============================================================================

def register_own_window_pid(pid: Any) -> None:
    """Declare an extra pid whose windows are Saturn's, not the user's."""
    try:
        value = int(pid or 0)
    except (TypeError, ValueError):
        return
    if value > 0:
        _OWN_PIDS.add(value)


def _frozen_pids() -> Set[int]:
    """Pids sharing our executable image when running as a packaged build.

    PyInstaller's one-file bootloader re-executes itself, so the window can
    belong to a child process. In development the executable is ``python.exe``,
    where that rule would also swallow every unrelated Python window - so it is
    only applied when frozen.
    """
    global _FROZEN_PIDS
    if _FROZEN_PIDS is not None:
        return _FROZEN_PIDS
    pids: Set[int] = set()
    if getattr(sys, "frozen", False):
        try:
            import psutil

            exe = (psutil.Process().exe() or "").lower()
            if exe:
                for proc in psutil.process_iter(["pid", "exe"]):
                    if (proc.info.get("exe") or "").lower() == exe:
                        pids.add(int(proc.info["pid"]))
        except Exception:
            pids = set()
    _FROZEN_PIDS = pids
    return pids


def own_pids() -> Set[int]:
    """Every pid whose windows must never be reported as the user's."""
    return {os.getpid()} | set(_OWN_PIDS) | _frozen_pids()


def is_own_window(info: Optional[Dict[str, Any]]) -> bool:
    """True when a window description belongs to Saturn itself."""
    if not isinstance(info, dict):
        return False
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    return pid > 0 and pid in own_pids()


# =============================================================================
# WINDOW DESCRIPTION
# =============================================================================

def _describe_window(hwnd: int) -> Dict[str, Any]:
    """Title, class, pid, geometry for one HWND. Never raises."""
    empty = {"title": "", "class_name": "", "pid": 0, "hwnd": hwnd, "rect": (0, 0, 0, 0)}
    if not user32 or not hwnd:
        return empty

    try:
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.strip()

        class_buff = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buff, 256)
        class_name = class_buff.value.strip()

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = max(0, rect.right - rect.left)
        h = max(0, rect.bottom - rect.top)

        return {
            "title": title,
            "class_name": class_name,
            "pid": pid.value,
            "hwnd": hwnd,
            "rect": (rect.left, rect.top, w, h),
            "bbox": (rect.left, rect.top, rect.right, rect.bottom),
        }
    except Exception as exc:
        return {"title": "", "class_name": "error", "error": str(exc), "pid": 0, "hwnd": hwnd,
                "rect": (0, 0, 0, 0)}


def get_foreground_window_info() -> Dict[str, Any]:
    """
    Returns metadata about the currently active foreground window:
    - title: Window title string
    - class_name: Executable or window class name
    - hwnd: Window handle
    - rect: (x, y, w, h)

    This is the raw OS answer and may well be one of Saturn's own windows. Use
    :func:`get_active_user_window` to ask about the user instead.
    """
    if not user32:
        return {"title": "", "class_name": "unknown", "hwnd": 0, "rect": (0, 0, 0, 0)}

    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"title": "", "class_name": "", "hwnd": 0, "rect": (0, 0, 0, 0)}
        return _describe_window(hwnd)
    except Exception as exc:
        return {"title": "", "class_name": "error", "error": str(exc), "rect": (0, 0, 0, 0)}


def process_name_for(pid: Any) -> str:
    """Executable name of a pid, or "" when it cannot be resolved."""
    try:
        value = int(pid or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        import psutil

        return str(psutil.Process(value).name() or "")
    except Exception:
        return ""


def _iter_top_level_windows(limit: int = 80) -> Iterator[int]:
    """Top-level windows in Z-order, front to back."""
    if not user32:
        return
    try:
        hwnd = user32.GetTopWindow(None)
    except Exception:
        return
    seen = 0
    while hwnd and seen < limit:
        yield hwnd
        seen += 1
        try:
            hwnd = user32.GetWindow(hwnd, _GW_HWNDNEXT)
        except Exception:
            return


def _is_tool_window(hwnd: int) -> bool:
    """Palettes and overlays: no taskbar entry, so never "what the user is doing"."""
    try:
        return bool(user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW)
    except Exception:
        return False


def _zorder_user_window() -> Dict[str, Any]:
    """Frontmost visible, titled, non-Saturn window. {} when there is none."""
    if not user32:
        return {}
    for hwnd in _iter_top_level_windows():
        try:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                continue
        except Exception:
            continue
        if _is_tool_window(hwnd):
            continue
        info = _describe_window(hwnd)
        if not info.get("title") or info.get("class_name") == "error":
            continue
        if str(info.get("class_name") or "").lower() in _SHELL_CLASSES:
            continue
        if is_own_window(info):
            continue
        info["method"] = "zorder"
        info["process_name"] = process_name_for(info.get("pid"))
        return info
    return {}


def get_active_user_window() -> Dict[str, Any]:
    """The window the *user* is working in - never one of Saturn's own.

    Adds two keys to the window description:

    - ``method``: ``"foreground"`` (the real focus), ``"zorder"`` (Saturn held
      the focus, so the topmost other window was used) or ``"cache"`` (nothing
      could be read; the last known user window is returned).
    - ``self_window``: the focus was Saturn itself.

    When the focus is Saturn and no other window can be found, the description
    is returned with an empty title rather than Saturn's own. Answering "you
    are in Saturn" to "what am I doing?" is worse than answering nothing: it
    reads as a description of the user's work and gets repeated verbatim.
    """
    info = get_foreground_window_info()

    if not is_own_window(info):
        if info.get("title"):
            result = dict(info)
            result["method"] = "foreground"
            result["self_window"] = False
            if result.get("pid"):
                result["process_name"] = process_name_for(result.get("pid"))
            _LAST_USER_WINDOW.clear()
            _LAST_USER_WINDOW.update(result)
            return result
        # Nothing focused and nothing to fall back to: report the emptiness
        # instead of inventing a window.
        result = dict(info)
        result["method"] = "foreground"
        result["self_window"] = False
        if result.get("pid"):
            result["process_name"] = process_name_for(result.get("pid"))
        return result

    candidate = _zorder_user_window()
    if candidate:
        candidate = dict(candidate)
        candidate["method"] = candidate.get("method") or "zorder"
        candidate["self_window"] = True
        _LAST_USER_WINDOW.clear()
        _LAST_USER_WINDOW.update(candidate)
        return candidate

    if _LAST_USER_WINDOW:
        cached = dict(_LAST_USER_WINDOW)
        cached["method"] = "cache"
        cached["self_window"] = True
        return cached

    return {"title": "", "class_name": "", "pid": 0, "hwnd": 0,
            "rect": (0, 0, 0, 0), "process_name": "",
            "method": "self", "self_window": True}


def capture_active_window_screenshot() -> Optional[bytes]:
    """
    Captures a high-resolution JPEG screenshot of only the current active
    window - the user's, not Saturn's own overlay.
    """
    info = get_active_user_window()
    bbox = info.get("bbox")
    if not bbox or info.get("rect", (0, 0, 0, 0))[2] < 10:
        return None

    try:
        from PIL import ImageGrab
        import io
        img = ImageGrab.grab(bbox=bbox)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None
