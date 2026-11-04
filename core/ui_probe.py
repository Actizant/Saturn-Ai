"""What is in front of the user, and where is the thing I was told to use.

WHY THIS IS NOT JUST ``vision.find_on_screen``
    ``core/vision`` already reads a window two ways - the accessibility tree and
    OCR - and that is the hard part, so this module does not repeat it. What it
    adds is the three things a control loop needs and a one-shot lookup does not:

    * **The user's window, not the focused one.** Saturn's own orb is
      always-on-top; probing "the foreground window" would eventually drive
      Saturn's own widgets around. This asks
      :func:`core.window_context.get_active_user_window`, the same source the
      situation brief uses, so the agent, the brief and the person all agree
      about what is on screen.
    * **A stable snapshot to compare.** Verification is a diff, so an
      observation carries a digest of the title, control names and text. "Did
      the screen change" is answerable without another whole probe.
    * **A cached observation.** Look, act, look costs two probes; the second one
      within a fraction of a second would return the pre-click tree and the
      agent would conclude nothing happened.

THE THREE RUNGS, IN THE ORDER THEY ARE TRUSTED
    1. A named control with a rectangle - the OS is telling us what it is.
    2. Rendered text located by OCR - the glyphs are there, but a label is not
       a button.
    3. Nothing readable - reported as such. An app that publishes no
       accessibility tree and renders custom-drawn text is a real case
       (Blender's viewport, most games), and the honest answer is to say the
       screen could not be read rather than to click somewhere plausible.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.app_kind import describe as describe_app
from core.window_context import get_active_user_window

CACHE_SECONDS = float(os.environ.get("SATURN_PROBE_CACHE", "0.8"))

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_CACHE: Dict[str, "Observation"] = {}
_CACHE_LOCK = threading.Lock()


# ── The snapshot ────────────────────────────────────────────────────────────

@dataclass
class Observation:
    """One look at one window."""

    window_title: str = ""
    process: str = ""
    class_name: str = ""
    app: str = ""
    hwnd: int = 0
    elements: List[Dict[str, Any]] = field(default_factory=list)
    words: List[Dict[str, Any]] = field(default_factory=list)
    text: str = ""
    at: float = field(default_factory=time.time)
    notes: List[str] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        return bool(self.window_title or self.elements or self.text)

    @property
    def content_readable(self) -> bool:
        """Whether anything *inside* the window could be read.

        A title proves there is a window, not what is in it. Verification needs
        the second question answered, so it is asked separately instead of
        letting a title stand in for a screen.
        """
        return bool(self.elements or self.text)

    @property
    def rung(self) -> str:
        """Which sense actually produced something."""
        if self.elements:
            return "control"
        if self.text:
            return "text"
        return "none"

    def names(self) -> List[str]:
        return [str(element.get("name") or "") for element in self.elements
                if element.get("name")]

    def digest(self) -> str:
        """A fingerprint of what a person would see, for verification."""
        parts = [self.window_title,
                 "|".join(sorted(self.names())),
                 re.sub(r"\s+", " ", self.text).strip().lower()[:2000]]
        return hashlib.sha1("\n".join(parts).encode("utf-8", "ignore")).hexdigest()[:16]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "window": self.window_title,
            "process": self.process,
            "app": self.app,
            "hwnd": self.hwnd,
            "rung": self.rung,
            "elements": len(self.elements),
            "text": self.text[:400],
            "notes": list(self.notes),
            "digest": self.digest(),
            "at": self.at,
        }


# ── Looking ─────────────────────────────────────────────────────────────────

def _window_or_none() -> Dict[str, Any]:
    try:
        window = get_active_user_window() or {}
    except Exception:
        return {}
    return window if window.get("hwnd") else {}


def observe(hwnd: Optional[int] = None, include_text: bool = True,
            use_cache: bool = True) -> Observation:
    """Look at the user's window. Never raises; unreadable is a valid answer."""
    window = _window_or_none()
    handle = int(hwnd or window.get("hwnd") or 0)
    key = f"{handle}:{int(include_text)}"

    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is not None and (time.time() - cached.at) < CACHE_SECONDS:
            return cached

    observation = Observation(
        window_title=str(window.get("title") or ""),
        process=str(window.get("process_name") or ""),
        class_name=str(window.get("class_name") or ""),
        hwnd=handle,
        at=time.time(),
    )
    observation.app = describe_app(observation.window_title, observation.process,
                                  observation.class_name)

    controls = _read_controls(window or None, handle)
    if controls.get("ok"):
        observation.elements = [
            dict(element) for element in controls.get("elements") or []
            if isinstance(element, dict) and element.get("name")
        ]
    else:
        observation.notes.append(str(controls.get("error") or "no accessibility tree"))

    if include_text:
        rendered = _read_text(window or None)
        if rendered.get("ok"):
            observation.text = str(rendered.get("text") or "")
            observation.words = [dict(word) for word in rendered.get("words") or []
                                 if isinstance(word, dict)]
        else:
            observation.notes.append(str(rendered.get("error") or "no text"))

    if not observation.content_readable:
        observation.notes.append(
            "this window exposes nothing readable: no accessibility tree and no "
            "legible text")

    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = observation
            if len(_CACHE) > 8:
                for stale in sorted(_CACHE, key=lambda item: _CACHE[item].at)[:-4]:
                    _CACHE.pop(stale, None)
    return observation


def _read_controls(window: Optional[Dict[str, Any]], handle: int) -> Dict[str, Any]:
    try:
        from core import vision

        if window is None and not handle:
            return {"ok": False, "error": "NO_WINDOW"}
        return vision.ui_elements(window=window, hwnd=handle or None)
    except Exception as exc:
        return {"ok": False, "error": f"UIA_FAILED: {exc}"}


def _read_text(window: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        from core import vision

        return vision.read_text(window=window)
    except Exception as exc:
        return {"ok": False, "error": f"OCR_FAILED: {exc}"}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def forget(window_digest: str = "") -> None:
    """Drop cached looks so the next observation is fresh."""
    if not window_digest:
        clear_cache()
        return
    with _CACHE_LOCK:
        for key in [item for item in _CACHE
                    if _CACHE[item].digest() == window_digest]:
            _CACHE.pop(key, None)


# ── Finding ─────────────────────────────────────────────────────────────────

def normalise(text: Any) -> str:
    return _WORD_SPLIT.sub(" ", str(text or "").lower()).strip()


def _run_in(haystack: List[str], needle: List[str]) -> bool:
    """Whether ``needle`` appears as a contiguous run of whole words.

    Substring matching is not good enough here: "ok" must not match "Look",
    and that class of mistake is exactly how a stray click lands on the wrong
    dialog.
    """
    span = len(needle)
    if not span or span > len(haystack):
        return False
    return any(haystack[index:index + span] == needle
               for index in range(len(haystack) - span + 1))


def score(name: Any, needle: Any) -> int:
    """How strongly a candidate matches what was asked for, 0 when it cannot.

    Deliberately conservative: an exact name beats everything, a whole-word
    occurrence is a strong hit, the reverse containment is a weaker one, and
    loose word overlap is only ever a hint. Clicking the wrong button is the
    failure this function exists to prevent.
    """
    wanted = normalise(needle)
    candidate = normalise(name)
    if not wanted or not candidate:
        return 0
    if candidate == wanted:
        return 100

    wanted_words = wanted.split()
    candidate_words = candidate.split()
    if _run_in(candidate_words, wanted_words):
        return 85
    if _run_in(wanted_words, candidate_words):
        return 70

    overlap = set(wanted_words) & set(candidate_words)
    if not overlap:
        return 0
    return int(60 * len(overlap) / len(wanted_words))


def find(target: str, observation: Optional[Observation] = None,
         include_text: bool = True) -> Optional[Dict[str, Any]]:
    """The best match for ``target`` on screen, or None.

    A control match always beats a text match of the same strength: one reports
    its own rectangle, the other is pixels guessed into boxes.
    """
    observation = observation or observe(include_text=include_text)
    wanted = str(target or "").strip()
    if not wanted:
        return None

    best: Optional[Dict[str, Any]] = None

    for element in observation.elements:
        value = score(element.get("name"), wanted)
        if not value:
            continue
        match = _match_from_element(element, value)
        if best is None or _better(match, best):
            best = match

    if include_text:
        text_best = _find_in_text(wanted, observation.words)
        if text_best and (best is None or _better(text_best, best)):
            best = text_best

    return best


def _better(candidate: Dict[str, Any], current: Dict[str, Any]) -> bool:
    if candidate["score"] != current["score"]:
        return candidate["score"] > current["score"]
    return candidate["source"] == "control" and current["source"] != "control"


def _match_from_element(element: Dict[str, Any], value: int) -> Dict[str, Any]:
    box = list(element.get("box") or (0, 0, 0, 0))
    while len(box) < 4:
        box.append(0)
    x, y, width, height = (int(number) for number in box[:4])
    match = {
        "source": "control",
        "label": str(element.get("name") or ""),
        "type": str(element.get("type") or ""),
        "box": (x, y, width, height),
        "center": (x + width // 2, y + height // 2),
        "score": value,
    }
    for key in ("enabled", "offscreen", "automation_id", "keyboard_focus"):
        if key in element:
            match[key] = element[key]
    return match


def _find_in_text(wanted: str, words: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    for word in words:
        label = str(word.get("text") or word.get("word") or "")
        value = score(label, wanted)
        if not value:
            continue
        box = _word_box(word)
        if box is None:
            continue
        x, y, width, height = box
        candidate = {
            "source": "text",
            "label": label,
            "type": "text",
            "box": (x, y, width, height),
            "center": (x + width // 2, y + height // 2),
            "score": value,
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    return best


def _word_box(word: Dict[str, Any]) -> Optional[tuple]:
    raw = word.get("box")
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        return tuple(int(number) for number in raw[:4])
    keys = ("left", "top", "width", "height")
    if all(key in word for key in keys):
        try:
            return tuple(int(word[key]) for key in keys)
        except (TypeError, ValueError):
            return None
    return None


# ── Describing, for the model ───────────────────────────────────────────────

def summarise(observation: Optional[Observation] = None, limit: int = 25) -> str:
    """A short account of the screen that a model can reason about."""
    observation = observation or observe()
    if not observation.content_readable:
        return (f"I could not read the window in front of you"
                + (f" ({observation.window_title})" if observation.window_title else "")
                + ". " + "; ".join(observation.notes))

    lines = [f"Window: '{observation.window_title}'"]
    if observation.app:
        lines.append(f"Application: {observation.app}")
    if observation.elements:
        names = observation.names()
        shown = ", ".join(names[:limit])
        more = f" (+{len(names) - limit} more)" if len(names) > limit else ""
        lines.append(f"Controls: {shown}{more}")
    if observation.text:
        flattened = re.sub(r"\s+", " ", observation.text).strip()
        lines.append(f"Text: {flattened[:300]}")
    if observation.notes:
        lines.append("Notes: " + "; ".join(observation.notes))
    return "\n".join(lines)
