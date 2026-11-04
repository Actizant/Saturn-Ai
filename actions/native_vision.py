"""Saturn's eyes, as three tools the assistant can actually call.

``see_screen``   - what window is focused, what controls it exposes, what it says
``read_region``  - the text inside a rectangle (or the whole screen)
``find_on_screen`` - where a label sits, so something else can click it

All three run on this machine: Tesseract for pixels, Windows UI Automation for
structure. None of them uploads an image or calls a model, and none of them
invents a result to look confident. When a sense is unavailable the answer says
which one and why.
"""

from __future__ import annotations

import re

from core import vision

# A window can carry hundreds of lines of text; the model needs enough to answer
# the question and not so much that the prompt drowns.
_MAX_TEXT = 1600
_MAX_CONTROLS = 18
_MAX_LABEL = 48

# Control characters other than newline and tab.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_UNAVAILABLE = {
    "OCR_UNAVAILABLE": "I can't read text without Tesseract installed",
    "OCR_TIMEOUT": "reading that took too long",
    "UIA_UNAVAILABLE": "Windows wouldn't tell me about the controls in that window",
    "UIA_TIMEOUT": "that window stopped answering while I was looking at it",
    "CAPTURE_FAILED": "I couldn't capture the screen",
    "QUERY_REQUIRED": "tell me what to look for",
    "VISION_TIMEOUT": "I couldn't finish looking in time",
    "VISION_FAILED": "something went wrong while I was looking",
}


def _clean(text: str) -> str:
    """Drop what a screenshot carries but a sentence cannot.

    OCR reads emoji, private-use glyphs and stray control codes straight off
    the screen. They mean nothing when spoken, and some of them crash a
    non-UTF-8 console, so the line structure and the words are all that survive.
    """
    text = _CONTROL.sub("", str(text or ""))
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    text = _clean(text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _region_from(parameters: dict, player=None):
    """Parse a rectangle out of tool arguments. None means the whole screen."""
    raw = parameters.get("region")
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        return tuple(raw)[:4]
    if isinstance(raw, str) and raw.strip():
        parts = [p for p in raw.replace(",", " ").split() if p.strip()]
        if len(parts) >= 4:
            return tuple(parts)[:4]
    keys = ("x", "y", "width", "height")
    if all(parameters.get(key) is not None for key in keys):
        return tuple(parameters[key] for key in keys)
    return None


def _target(parameters: dict) -> str:
    return str(parameters.get("target") or parameters.get("when") or "").strip().lower()


def _window_for(parameters: dict) -> dict:
    """The window under discussion: the focused one, unless asked for all screens."""
    if _target(parameters) in ("screen", "all", "desktop", "full"):
        return {}
    return vision.active_window()


def _controls_block(elements: list[dict]) -> str:
    if not elements:
        return ""
    shown = elements[:_MAX_CONTROLS]
    rows = []
    for element in shown:
        label = _clean(element["name"]).replace("\n", " ")
        if len(label) > _MAX_LABEL:
            label = label[:_MAX_LABEL - 1] + "…"
        rows.append(f"{element['type']} \"{label}\"")
    more = "" if len(elements) <= len(shown) else f" (+{len(elements) - len(shown)} more)"
    return ", ".join(rows) + more


def see_screen(parameters: dict, player=None, speak=None, **_) -> str:
    """Look at the focused window: its controls and its readable text."""
    params = parameters or {}
    window = _window_for(params)

    snapshot = vision.see(
        region=_region_from(params),
        window=window or None,
        include_text=params.get("text", True) is not False,
        include_elements=params.get("controls", True) is not False,
    )

    heading = snapshot["window"].get("title") or "the desktop"
    parts = [f"Focused window: \"{heading}\"."]

    controls = snapshot.get("elements") or []
    if controls:
        parts.append(f"Controls ({len(controls)}): {_controls_block(controls)}.")
    elif any(note.startswith("elements:") for note in snapshot.get("notes", [])):
        parts.append("I couldn't read that window's controls.")

    text = _clip(snapshot.get("text", ""))
    if text:
        parts.append("Text on it:\n" + text)
    elif any(note.startswith("text:") for note in snapshot.get("notes", [])):
        parts.append("I couldn't read any text from it.")

    if not controls and not text:
        parts.append("It exposes nothing readable - it may be drawing itself without telling Windows.")

    return "\n".join(parts)


def read_region(parameters: dict, player=None, speak=None, **_) -> str:
    """Read the text inside a rectangle, or anywhere on screen."""
    params = parameters or {}
    window = _window_for(params)
    region = _region_from(params)

    reading = vision.read_text(
        region=region,
        window=window or None,
        psm=params.get("psm") or vision.DEFAULT_PSM,
        scale=params.get("scale") or vision.DEFAULT_SCALE,
    )

    if not reading.get("ok"):
        code = str(reading.get("error") or "")
        reason = _UNAVAILABLE.get(code, "I couldn't read that")
        detail = str(reading.get("message") or "").strip()
        return f"{reason}." if not detail else f"{reason}: {detail}"

    text = _clip(reading.get("text", ""))
    if not text:
        x, y, w, h = reading.get("region") or (0, 0, 0, 0)
        return f"No text in that area ({w}x{h} at {x},{y})."

    lines = len(reading.get("lines") or [])
    return f"Text from {lines} line{'s' if lines != 1 else ''}:\n{text}"


def find_on_screen(parameters: dict, player=None, speak=None, **_) -> str:
    """Locate a label on screen and report where it is."""
    params = parameters or {}
    query = str(params.get("text") or params.get("query") or params.get("label") or "").strip()
    if not query:
        return "Tell me what to look for."

    result = vision.find_on_screen(
        query,
        region=_region_from(params),
        window=_window_for(params) or None,
        include_text=params.get("text_search", True) is not False,
    )
    if not result.get("ok"):
        code = str(result.get("error") or "")
        return _UNAVAILABLE.get(code, "I couldn't search the screen") + "."

    matches = result.get("matches") or []
    if not matches:
        note = "; ".join(result.get("notes") or [])
        extra = f" ({note})" if note else ""
        return f"I don't see \"{query}\" on screen{extra}."

    best = matches[0]
    x, y, w, h = best["box"]
    label = _clean(best["label"]).replace("\n", " ")
    where = f"at {x},{y} ({w}x{h})"
    if best["source"] == "control":
        sentence = f"\"{label}\" is a {best['type']} {where}, centre {best['center'][0]},{best['center'][1]}."
    else:
        sentence = f"\"{label}\" is printed {where}, centre {best['center'][0]},{best['center'][1]}."

    if len(matches) > 1:
        others = ", ".join(
            f"\"{_clean(m['label'])[:32]}\"" for m in matches[1:4]
        )
        sentence += f" Also matched: {others}."
    return sentence


# The overlay asks for structure only - it draws boxes, and pixels would be its
# own reflection rather than the user's screen.
def layout_boxes(hwnd: int | None = None, limit: int = vision.UIA_MAX_ELEMENTS) -> list[dict]:
    """Real control rectangles for one window, or an empty list."""
    if not hwnd:
        return []
    result = vision.ui_elements(hwnd=hwnd, limit=limit)
    return list(result.get("elements") or []) if result.get("ok") else []


# ------------------------------------------------- screen_process tool seam ---
#
# The retired actions/screen_processor.py drove this tool name through Gemini
# Live (uploading a frame to a cloud vision model). This is the 100% local
# replacement, kept behind the SAME tool name and call signature so every
# existing dispatch site is untouched: capture + Tesseract OCR + UI Automation
# (see_screen), optionally reasoned over by the resident local brain. Nothing
# here uploads a frame, and when there is no readable content it says so
# instead of inventing a scene.

def _announce(answer: str, player=None, speak=None) -> bool:
    """Deliver a reading to the UI workspace and the local voice, in that order
    of what is actually wired up. Never raises for a missing sink."""
    spoken = False
    if player is not None:
        try:
            if hasattr(player, "set_scanning"):
                player.set_scanning(False, "")
        except Exception:
            pass
        try:
            if hasattr(player, "update_task_workspace"):
                player.update_task_workspace(
                    status="Screen read locally", output=answer, percent=100)
        except Exception:
            pass
        try:
            if hasattr(player, "write_log"):
                player.write_log(f"Screen: {answer[:200]}")
        except Exception:
            pass
    for candidate in (speak, getattr(player, "speak", None)):
        if callable(candidate):
            try:
                candidate(answer)
                spoken = True
                break
            except Exception:
                continue
    if not spoken:
        # No assistant sink answered: fall back to the native OS voice helper
        # (the local VoiceLoop mouth was removed with the voice-stack pivot).
        try:
            from actions.attention_monitor import speak_native
            speak_native(answer)
            spoken = True
        except Exception:
            pass
    return spoken


def _reason_about(question: str, reading: str) -> str:
    """Let the resident offline brain answer the user's question from the local
    reading. Guarded: any failure (no model, no VRAM slot) returns "" and the
    caller falls back to the raw OCR/UIA text - honest degradation, never a
    fabricated scene."""
    try:
        import llm_client
        system = (
            "You are looking at the user's screen through a local OCR and UI-"
            "Automation reading. Answer their question using ONLY that reading. "
            "If the reading does not contain what they asked, say plainly that "
            "you can only see text, not images. Be concise and speakable."
        )
        prompt = f"Question: {question}\n\nScreen reading:\n{reading}"
        reply = llm_client.chat(prompt, system=system)
        return str(reply or "").strip()
    except Exception:
        return ""


def screen_process(
    parameters: dict | None = None,
    response: str | None = None,
    player=None,
    session_memory=None,
    image_bytes: bytes | None = None,
    speak=None,
    **_,
) -> bool:
    """Local replacement for the Gemini Live screen processor, same tool name.

    Reads the focused window (or whole screen) with OCR + UI Automation, lets
    the resident brain answer the user's question from that reading when one is
    asked, and announces the result through whatever voice sink is wired up.
    Returns True when a reading was produced (announced or not)."""
    params = parameters or {}
    user_text = str(params.get("text") or params.get("user_text") or "").strip()

    try:
        reading = see_screen(params, player=player)
    except Exception as exc:  # capture/OCR blew up - say so, don't crash the tool
        reading = _UNAVAILABLE.get("CAPTURE_FAILED", "I couldn't read the screen") + f" ({exc})."

    answer = _reason_about(user_text, reading) if user_text else ""
    answer = answer or reading
    _announce(answer, player=player, speak=speak)
    return bool(answer)
