"""Saturn's local eyes: screen text and UI structure, read on this machine.

Two senses, both offline:

* **Text** — the Tesseract binary already used for document OCR, run against a
  screenshot. No model, no API key, no network.
* **Structure** — Windows UI Automation, walked with a fixed element budget. It
  reports what the operating system says the controls *are*: type, name,
  rectangle. Nothing is inferred.

Neither sense guesses. If Tesseract is missing the result carries
``OCR_UNAVAILABLE``; if UI Automation cannot be reached it carries
``UIA_UNAVAILABLE``. An empty screen is reported as empty, never filled in.

Coordinates are physical pixels of the virtual desktop — the same space
``ImageGrab`` and ``pyautogui`` use — with the virtual origin at the top-left,
so a secondary monitor to the left yields negative x.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable

# ── Configuration ────────────────────────────────────────────────────────────

# A whole-screen OCR pass at 1.5x on a 1080p display lands near 1.7 s; anything
# past half a minute means the process is wedged, not slow.
OCR_TIMEOUT_SECONDS = float(os.environ.get("SATURN_VISION_OCR_TIMEOUT", "30"))
# UI Automation is walked in one thread and abandoned past this deadline. A
# hung application must not hang the assistant.
UIA_TIMEOUT_SECONDS = float(os.environ.get("SATURN_VISION_UIA_TIMEOUT", "6"))
# Element budget for one walk. Chrome exposes several thousand; the first few
# hundred hold the toolbar and the page frame, which is what "what's this
# window" actually needs.
UIA_MAX_ELEMENTS = int(os.environ.get("SATURN_VISION_MAX_ELEMENTS", "260"))
UIA_MAX_DEPTH = int(os.environ.get("SATURN_VISION_MAX_DEPTH", "12"))

# Screen text is small and often low-contrast; upscaling then boosting contrast
# measured both faster and more accurate than feeding the raw pixels.
DEFAULT_SCALE = 1.5
DEFAULT_CONTRAST = 2.0
DEFAULT_PSM = 6
DEFAULT_LANG = os.environ.get("SATURN_VISION_LANG", "eng")

# UIA control type ids. The names are the operating system's own vocabulary.
CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar",
    50038: "Separator", 50039: "SemanticZoom", 50040: "AppBar",
}

# Containers say nothing a caller wants and cost the most to walk.
_SKIP_TYPES = {50026, 50033, 50025, 50032}  # Group, Pane, Custom, Window


# ── Small helpers ────────────────────────────────────────────────────────────


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def active_window() -> dict:
    """The focused window, as Windows describes it. Never raises."""
    try:
        from core.window_context import get_foreground_window_info

        info = get_foreground_window_info() or {}
    except Exception as exc:  # pragma: no cover - win32 import guard
        return {"title": "", "class_name": "", "hwnd": 0, "rect": (0, 0, 0, 0),
                "error": str(exc)}
    if not info.get("rect"):
        info["rect"] = (0, 0, 0, 0)
    return info


def own_window_names() -> tuple[str, ...]:
    """Titles that mean "this is Saturn itself", never user content."""
    names = {"saturn"}
    try:
        from core.identity import identity

        for candidate in (identity.get_application_name(), identity.get_assistant_name()):
            value = str(candidate or "").strip().lower()
            if value:
                names.add(value)
    except Exception:
        pass
    return tuple(names)


def dpi_scale() -> float:
    """System DPI as a multiplier. 1.0 means 100% scaling."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes

        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def _region_tuple(region) -> tuple[int, int, int, int] | None:
    """Accept ``(x, y, w, h)`` or a mapping; reject anything unusable."""
    if region is None:
        return None
    if isinstance(region, dict):
        region = (region.get("x"), region.get("y"),
                  region.get("width", region.get("w")),
                  region.get("height", region.get("h")))
    try:
        values = [int(round(float(v))) for v in region]
    except (TypeError, ValueError):
        return None
    if len(values) < 4:
        return None
    x, y, w, h = values[:4]
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


def _run_bounded(func: Callable[[], object], timeout: float):
    """Run ``func`` in a daemon thread; give up waiting after ``timeout``.

    Returns ``(finished, result, error)``. An abandoned call is left to die with
    the process: killing a thread mid-COM-call is not possible, and a hung
    target application must not be allowed to block the caller.
    """
    box: dict = {}

    def runner():
        try:
            box["result"] = func()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True, name="SaturnVision")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None, None
    return True, box.get("result"), box.get("error")


# ── Screen capture ───────────────────────────────────────────────────────────


def capture(region=None, window: dict | None = None):
    """Grab the screen, a region, or one window.

    Returns ``(image, region, origin)`` where ``region`` is ``(x, y, w, h)`` and
    ``origin`` is ``(x, y)``. Raises ``RuntimeError`` when nothing can be read.
    """
    from PIL import ImageGrab

    target = _region_tuple(region)
    if target is None and window:
        rect = window.get("rect") or (0, 0, 0, 0)
        if len(rect) >= 4 and rect[2] > 0 and rect[3] > 0:
            target = _region_tuple(rect)

    try:
        if target is None:
            image = ImageGrab.grab(all_screens=True)
            return image, (0, 0, image.width, image.height), (0, 0)
        x, y, w, h = target
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        return image, target, (x, y)
    except Exception as exc:
        raise RuntimeError(f"screen capture failed: {exc}") from exc


# ── Text ─────────────────────────────────────────────────────────────────────


def tesseract_binary() -> str | None:
    """Find Tesseract the same way the memory engine does.

    The Windows installer adds nothing to PATH, so the install directory is
    searched explicitly rather than reporting the engine missing.
    """
    override = str(os.environ.get("SATURN_TESSERACT") or "").strip()
    if override and os.path.isfile(override):
        return override

    found = shutil.which("tesseract")
    if found:
        return found

    name = "tesseract.exe" if os.name == "nt" else "tesseract"
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if not base:
            continue
        candidate = os.path.join(base, "Tesseract-OCR", name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _prepare_for_ocr(image, scale: float, contrast: float):
    """Grayscale, upscale, boost contrast. Returns ``(image, applied_scale)``."""
    from PIL import ImageEnhance

    applied = _as_float(scale, DEFAULT_SCALE)
    if applied != 1.0:
        # LANCZOS measured better than NEAREST/BILINEAR on UI text.
        from PIL import Image as PILImage

        image = image.resize(
            (max(1, int(image.width * applied)), max(1, int(image.height * applied))),
            PILImage.LANCZOS,
        )
    image = image.convert("L")
    if contrast and contrast > 1.0:
        image = ImageEnhance.Contrast(image).enhance(contrast)
    return image, applied


def _parse_tsv(tsv: str, origin: tuple[int, int], scale: float) -> tuple[str, list, list]:
    """Turn Tesseract's TSV into ``(text, lines, words)`` in screen pixels."""
    ox, oy = origin
    words: list[dict] = []
    for row in tsv.splitlines()[1:]:  # the first line is the column header
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":  # level 5 is a word
            continue
        text = cols[11].strip()
        if not text:
            continue
        try:
            confidence = float(cols[10])
            # Columns: 0 level, 1 page, 2 block, 3 paragraph, 4 line, 5 word,
            # 6 left, 7 top, 8 width, 9 height, 10 confidence, 11 text.
            left, top, width, height = (int(cols[6]), int(cols[7]),
                                        int(cols[8]), int(cols[9]))
        except ValueError:
            continue
        if confidence < 0:
            continue
        words.append({
            "text": text,
            "conf": round(confidence, 1),
            "box": (ox + round(left / scale), oy + round(top / scale),
                    round(width / scale), round(height / scale)),
            "line": (cols[1], cols[2], cols[3], cols[4]),
        })

    grouped: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for word in words:
        key = word.pop("line")
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(word)

    lines: list[dict] = []
    for key in order:
        members = grouped[key]
        x0 = min(w["box"][0] for w in members)
        y0 = min(w["box"][1] for w in members)
        x1 = max(w["box"][0] + w["box"][2] for w in members)
        y1 = max(w["box"][1] + w["box"][3] for w in members)
        lines.append({
            "text": " ".join(w["text"] for w in members),
            "box": (x0, y0, x1 - x0, y1 - y0),
        })

    text = "\n".join(line["text"] for line in lines)
    return text, lines, words


def read_text(image=None, region=None, window: dict | None = None,
              psm: int | str = DEFAULT_PSM, scale: float = DEFAULT_SCALE,
              contrast: float = DEFAULT_CONTRAST, lang: str | None = None,
              timeout: float | None = None) -> dict:
    """Read the text in an image, a region, or a window.

    Returns a dict with ``ok``, ``text``, ``lines``, ``words``, ``region`` and
    ``ms``; failures carry ``error`` and ``message`` instead of a guess.
    """
    started = time.perf_counter()
    binary = tesseract_binary()
    if not binary:
        return {"ok": False, "error": "OCR_UNAVAILABLE",
                "message": "Tesseract is not installed or not on PATH"}
    if image is None:
        try:
            image, used_region, origin = capture(region, window)
        except RuntimeError as exc:
            return {"ok": False, "error": "CAPTURE_FAILED", "message": str(exc)}
    else:
        used_region = _region_tuple(region) or (0, 0, image.width, image.height)
        origin = (used_region[0], used_region[1])

    prepared, applied_scale = _prepare_for_ocr(
        image, _as_float(scale, DEFAULT_SCALE), _as_float(contrast, DEFAULT_CONTRAST)
    )

    import tempfile

    handle, path = tempfile.mkstemp(prefix="saturn_vision_", suffix=".png")
    os.close(handle)
    try:
        prepared.save(path, format="PNG")
        command = [binary, path, "stdout", "--psm", str(_as_int(psm, DEFAULT_PSM)),
                   "-l", str(lang or DEFAULT_LANG), "--dpi", "96", "tsv"]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or OCR_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "OCR_TIMEOUT",
                "message": f"Tesseract did not finish within {timeout or OCR_TIMEOUT_SECONDS:.0f}s"}
    except Exception as exc:
        return {"ok": False, "error": "OCR_FAILED", "message": str(exc)}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        return {"ok": False, "error": "OCR_FAILED",
                "message": detail[0] if detail else f"tesseract exited {completed.returncode}"}

    text, lines, words = _parse_tsv(completed.stdout or "", origin, applied_scale)
    return {
        "ok": True,
        "text": text,
        "lines": lines,
        "words": words,
        "region": used_region,
        "psm": _as_int(psm, DEFAULT_PSM),
        "ms": int((time.perf_counter() - started) * 1000),
    }


# ── Structure ────────────────────────────────────────────────────────────────

_uia_lock = threading.Lock()
_uia_module = None
_uia_cache: dict[tuple, tuple[float, list]] = {}
_UIA_CACHE_SECONDS = 2.0


def _uia_automation():
    """The UI Automation root object, created once per process.

    COM is apartment-scoped, so every thread that touches it initialises first.
    """
    global _uia_module
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    with _uia_lock:
        if _uia_module is None:
            client = comtypes.client.GetModule("UIAutomationCore.dll")
            automation = comtypes.client.CreateObject(
                client.CUIAutomation, interface=client.IUIAutomation
            )
            _uia_module = (client, automation)
    return _uia_module[1]


def _walk_tree(automation, hwnd: int, limit: int, depth_cap: int) -> list[dict]:
    """Breadth-first walk of one window's control view, capped on both axes."""
    from ctypes import wintypes

    walker = automation.ControlViewWalker
    root = automation.ElementFromHandle(wintypes.HWND(hwnd))
    if not root:
        return []

    rows: list[dict] = []
    queue: list[tuple[object, int]] = [(root, 0)]
    visited = 0
    while queue and len(rows) < limit and visited < limit * 3:
        node, depth = queue.pop(0)
        visited += 1
        try:
            control_type = int(node.CurrentControlType)
            name = str(node.CurrentName or "").strip()
            offscreen = bool(node.CurrentIsOffscreen)
            rect = node.CurrentBoundingRectangle
        except Exception:
            continue

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        interesting = (
            name
            and control_type not in _SKIP_TYPES
            and not offscreen
            and width > 0
            and height > 0
        )
        if interesting:
            rows.append({
                "type": CONTROL_TYPES.get(control_type, f"Control{control_type}"),
                "name": name,
                "box": (int(rect.left), int(rect.top), width, height),
                "depth": depth,
            })

        if depth >= depth_cap:
            continue
        try:
            child = walker.GetFirstChildElement(node)
            while child is not None and visited + len(queue) < limit * 3:
                queue.append((child, depth + 1))
                child = walker.GetNextSiblingElement(child)
        except Exception:
            continue
    return rows


def ui_elements(window: dict | None = None, hwnd: int | None = None,
                limit: int = UIA_MAX_ELEMENTS, depth: int = UIA_MAX_DEPTH,
                timeout: float | None = None, use_cache: bool = True) -> dict:
    """Named, on-screen controls of a window, with rectangles.

    Returns ``{"ok": True, "elements": [...], "count": n, "ms": int}`` or an
    ``UIA_*`` error. A window exposing nothing readable is ``count: 0``, not a
    failure - some applications publish no accessibility tree at all.
    """
    started = time.perf_counter()
    if sys.platform != "win32":
        return {"ok": False, "error": "UIA_UNAVAILABLE", "message": "UI Automation is Windows only"}

    target = window or active_window()
    # An explicit 0 means "no window"; only an omitted handle falls back to the
    # focused one. Returning the focused window for a caller that asked about
    # nothing would answer a different question.
    handle = int(target.get("hwnd") or 0) if hwnd is None else int(hwnd)
    if not handle:
        return {"ok": False, "error": "UIA_UNAVAILABLE", "message": "no focused window to inspect"}

    key = (handle, limit, depth)
    if use_cache:
        cached = _uia_cache.get(key)
        if cached and (time.time() - cached[0]) < _UIA_CACHE_SECONDS:
            return {"ok": True, "elements": [dict(row) for row in cached[1]],
                    "count": len(cached[1]), "ms": 0, "cached": True,
                    "window": target.get("title", "")}

    def collect() -> list[dict]:
        return _walk_tree(_uia_automation(), handle, limit, depth)

    finished, rows, error = _run_bounded(collect, timeout or UIA_TIMEOUT_SECONDS)
    if not finished:
        return {"ok": False, "error": "UIA_TIMEOUT",
                "message": f"UI Automation did not answer within {timeout or UIA_TIMEOUT_SECONDS:.0f}s"}
    if error is not None:
        return {"ok": False, "error": "UIA_UNAVAILABLE", "message": str(error)}

    rows = rows or []
    if use_cache:
        _uia_cache[key] = (time.time(), rows)
    return {"ok": True, "elements": rows, "count": len(rows),
            "ms": int((time.perf_counter() - started) * 1000),
            "window": target.get("title", "")}


# ── Composite senses ─────────────────────────────────────────────────────────


def see(region=None, window: dict | None = None, include_text: bool = True,
        include_elements: bool = True, element_limit: int = UIA_MAX_ELEMENTS,
        psm: int | str = DEFAULT_PSM, scale: float = DEFAULT_SCALE,
        uia_timeout: float | None = None, text_timeout: float | None = None) -> dict:
    """One snapshot: what window is focused, what it says, what it contains.

    Text and structure are gathered concurrently, because either alone takes
    about two seconds and together they should not take four. Each sense gets
    its own deadline: a window that will not answer to UI Automation must not
    discard text that has already been read, and a stubborn OCR pass must not
    hide structure that arrived instantly.
    """
    started = time.perf_counter()
    target = window or active_window()
    box: dict = {}

    jobs: list[tuple[str, threading.Thread, float]] = []

    def spawn(label: str, func: Callable[[], object], budget: float) -> None:
        def runner():
            try:
                box[label] = func()
            except BaseException as exc:  # noqa: BLE001 - reported, never raised
                box[label] = {"ok": False, "error": "VISION_FAILED", "message": str(exc)}

        thread = threading.Thread(target=runner, daemon=True, name=f"SaturnVision-{label}")
        thread.start()
        jobs.append((label, thread, max(0.1, budget)))

    if include_elements:
        spawn("elements", lambda: ui_elements(target, limit=element_limit),
              uia_timeout or UIA_TIMEOUT_SECONDS)
    if include_text:
        spawn("text", lambda: read_text(region=region, window=target, psm=psm, scale=scale),
              text_timeout or OCR_TIMEOUT_SECONDS)

    for _label, thread, budget in jobs:
        thread.join(budget)

    elements = box.get("elements") or {"ok": False, "error": "VISION_TIMEOUT"}
    text = box.get("text") or {"ok": False, "error": "VISION_TIMEOUT"}

    payload = {
        "ok": True,
        "window": {
            "title": target.get("title", ""),
            "class_name": target.get("class_name", ""),
            "pid": target.get("pid", 0),
            "rect": list(target.get("rect") or (0, 0, 0, 0)),
        },
        "region": list(text.get("region") or ()),
        "text": text.get("text", "") if text.get("ok") else "",
        "lines": text.get("lines", []) if text.get("ok") else [],
        "elements": elements.get("elements", []) if elements.get("ok") else [],
        "counts": {
            "text_ms": text.get("ms"),
            "elements_ms": elements.get("ms"),
            "lines": len(text.get("lines", []) or []),
            "elements": len(elements.get("elements", []) or []),
        },
        "ms": int((time.perf_counter() - started) * 1000),
        "notes": [],
    }
    for label, part in (("text", text), ("elements", elements)):
        if not part.get("ok"):
            payload["notes"].append(f"{label}: {part.get('error')} - {part.get('message', '')}".strip())
    return payload


def _score(name: str, query: str) -> int:
    """How well an element name answers the query. 0 means no match."""
    hay = name.strip().lower()
    needle = query.strip().lower()
    if not hay or not needle:
        return 0
    if hay == needle:
        return 100
    if hay.startswith(needle):
        return 80
    if needle in hay:
        # A hit that starts a word ("as" in "Save as") is a better answer than
        # one buried inside one ("as" in "Baseline").
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}", hay):
            return 70
        return 60
    return 0


def find_on_screen(query: str, region=None, window: dict | None = None,
                   include_text: bool = True, limit: int = 8) -> dict:
    """Locate a label on screen, by control name or by rendered text.

    Controls are checked first: a real button reports its own rectangle and the
    operating system's word for what it is. Text found by OCR only proves the
    glyphs are there, so those matches are marked ``source: text`` and score
    lower.
    """
    needle = str(query or "").strip()
    if not needle:
        return {"ok": False, "error": "QUERY_REQUIRED",
                "message": "give me something to look for"}

    started = time.perf_counter()
    target = window or active_window()
    matches: list[dict] = []

    controls = ui_elements(target)
    exact = False
    if controls.get("ok"):
        for element in controls["elements"]:
            score = _score(element["name"], needle)
            if not score:
                continue
            x, y, w, h = element["box"]
            matches.append({
                "source": "control",
                "label": element["name"],
                "type": element["type"],
                "box": element["box"],
                "center": (x + w // 2, y + h // 2),
                "score": score,
            })
            if score == 100:
                exact = True

    # An exact named control is the strongest answer available, and a full OCR
    # pass costs another few seconds. Read the pixels only when it adds
    # information - a different hit, a coordinate for text a control cannot find.
    if include_text and not exact:
        reading = read_text(region=region, window=target)
        if reading.get("ok"):
            for line in reading["lines"]:
                score = _score(line["text"], needle)
                if not score:
                    continue
                x, y, w, h = line["box"]
                matches.append({
                    "source": "text",
                    "label": line["text"],
                    "type": "Text",
                    "box": line["box"],
                    "center": (x + w // 2, y + h // 2),
                    # Rendered text is weaker evidence than a named control.
                    "score": max(1, score - 20),
                })
        else:
            matches.append({"source": "note", "label": "", "type": "",
                            "box": (0, 0, 0, 0), "center": (0, 0), "score": 0,
                            "note": f"text: {reading.get('error')}"})

    matches.sort(key=lambda row: (-row["score"], row.get("source") != "control"))
    found = [row for row in matches if row["score"] > 0][:limit]
    return {
        "ok": True,
        "query": needle,
        "found": bool(found),
        "matches": found,
        "window": target.get("title", ""),
        "ms": int((time.perf_counter() - started) * 1000),
        "notes": [row["note"] for row in matches if row.get("note")],
    }


# ── Capabilities ─────────────────────────────────────────────────────────────


def available() -> dict:
    """What this machine can actually do, so callers never have to guess."""
    ocr = tesseract_binary()
    uia = {"ok": False, "error": "UIA_UNAVAILABLE"}
    if sys.platform == "win32":
        probe = ui_elements(limit=8, depth=2, use_cache=False, timeout=3.0)
        uia = {"ok": bool(probe.get("ok")), "error": probe.get("error")}
    return {
        "ocr": bool(ocr),
        "ocr_binary": ocr or "",
        "uia": uia["ok"],
        "uia_error": uia.get("error", ""),
        "platform": sys.platform,
        "dpi_scale": round(dpi_scale(), 2),
    }
