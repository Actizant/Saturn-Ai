"""Saturn Studio -- the seam between Saturn's voice and the design canvas.

"Open your studio and design this" is two problems wearing one sentence: get
the canvas on screen, then get something drawn on it that the user actually
asked for. This module is where those meet. It owns no drawing of its own --
every mark on the board is made by the engine's tools, called through the page's
``window.saturnStudio`` surface, and the verbs below are named for what a person
says rather than for what the engine exports.

Two rules hold everywhere in here, as they do in Saturn's View. Nothing is
claimed that the engine did not say: the name, the type, the child count in a
returned sentence all came out of the answer the canvas gave back. And a call
that did not answer is reported as not having answered, not as a failure -- a
page that is still measuring a font is busy, not broken.

The one thing this module does invent is the design itself. The brief goes to
Saturn's brain with the engine's own authoring reference attached, and what come
back are the JSX strings the ``render`` tool reads. If no brain answers, nothing
is drawn and Saturn says so.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("saturn.studio.tool")

BASE_DIR = Path(__file__).resolve().parent.parent

#: Saturn's own authoring reference for the native engine, shipped in the tree.
#: Attached to the request rather than summarised from memory, so the vocabulary
#: the canvas understands and the vocabulary Saturn writes from are one document.
AUTHORING_REFERENCE = BASE_DIR / "saturn_studio" / "native" / "AUTHORING.md"

#: Only used if the reference file is missing, which would mean an incomplete
#: checkout. Enough to draw a legible frame without pretending to be the manual.
AUTHORING_FALLBACK = (
    "Design JSX is scene creation, not HTML. Elements: Frame, Text, Rectangle, "
    "Ellipse, Line, Icon. Props: w, h (numbers, 'hug', 'fill'), flex='row'|'col', "
    "gap, p, px, py, justify, items, bg, fill, stroke, strokeWidth, rounded, "
    "opacity, size, font, weight, color, textAlign. One root element."
)

_SYSTEM_PROMPT = (
    "You write design JSX for Saturn Studio: one JSX element as a string, and "
    "nothing else. Follow the authoring reference exactly. No markdown fences, "
    "no comments, no explanation, no code outside the returned element. Every "
    "attribute must be one the reference lists.\n\n"
    "AUTHORING REFERENCE\n{reference}"
)

_FENCE = re.compile(r"^```(?:jsx|tsx|javascript|js|html)?\s*|\s*```$", re.MULTILINE)

#: The verbs, in the order they are taught. Answered from ``HANDLERS`` so a
#: mistyped request lists what really exists rather than a stale sentence.
VOCABULARY = (
    "open | show | hide | status | tools | design | draw | read | describe | "
    "clear | tool | save | open_file | zoom | undo | redo"
)


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _number(args: dict, *keys: str) -> Optional[float]:
    for key in keys:
        raw = args.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


def _brief_of(args: dict) -> str:
    """What the user asked for, whichever word the caller put it under."""
    for key in ("brief", "subject", "description", "text", "what", "query"):
        value = _clean(args.get(key))
        if value:
            return value
    return ""


def _authoring_reference() -> str:
    try:
        text = AUTHORING_REFERENCE.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("studio authoring reference unreadable: %s", exc)
        return AUTHORING_FALLBACK
    return text if len(text) > 200 else AUTHORING_FALLBACK


def _strip_jsx(raw: str) -> str:
    """The element out of whatever the model wrapped around it.

    A fence is stripped, then the text is cut to its first and last angle
    bracket: a model that explains before it draws is common, and the
    explanation is not part of the design.
    """
    text = _FENCE.sub("", _clean(raw)).strip()
    if not text:
        return ""
    start, end = text.find("<"), text.rfind(">")
    if start == -1 or end <= start:
        return ""
    return text[start : end + 1]


def _compose_jsx(brief: str, style: str = "") -> tuple[str, str]:
    """Ask Saturn's brain for the design. Returns (jsx, error)."""
    from llm_client import client

    prompt = f"Design this: {brief}"
    if _clean(style):
        prompt += f"\n\nStyle notes from the user: {style}"
    try:
        raw = client.chat(
            prompt,
            system=_SYSTEM_PROMPT.format(reference=_authoring_reference()),
            max_tokens=3000,
            temperature=0.4,
        )
    except Exception as exc:  # noqa: BLE001 -- no brain is an answer, not a crash
        logger.warning("studio design request failed: %s", exc)
        return "", f"Saturn's brain did not answer the design request ({exc})"

    jsx = _strip_jsx(_clean(raw))
    if not jsx:
        return "", "Saturn's brain answered, but not with JSX Saturn could draw"
    return jsx, ""


def _report(answer: dict) -> str:
    """The engine's own failure words, or the empty result it handed back."""
    if answer.get("ok"):
        return ""
    error = _clean(answer.get("error"))
    if answer.get("unanswered"):
        return "Saturn Studio did not answer in time."
    return f"Saturn Studio could not do that: {error}" if error else \
        "Saturn Studio returned nothing."


def _ready(studio) -> Optional[str]:
    if studio.wait_ready(timeout=30.0):
        return None
    return (
        "Saturn Studio is on screen but its canvas has not finished starting, "
        "so nothing was drawn yet."
    )


def _placement(args: dict) -> dict:
    placement = {}
    for key in ("x", "y"):
        value = _number(args, key)
        if value is not None:
            placement[key] = value
    parent = _clean(args.get("parent_id")) or _clean(args.get("into"))
    if parent:
        placement["parent_id"] = parent
    return placement


# --------------------------------------------------------------------------- #
# The verbs that need no canvas at all
# --------------------------------------------------------------------------- #


def _open(studio, args: dict) -> str:
    result = studio.open_page()
    error = _clean(result.get("error"))
    if error:
        return f"Saturn Studio could not start: {error}"

    waiting = _ready(studio)
    if waiting:
        return waiting

    state = studio.state()
    layers = state.get("layers") or []
    where = "on the Design page" if result.get("hosted") else "in its own window"
    if layers:
        return (
            f"Opened Saturn Studio {where}. {state.get('doc', 'untitled')} has "
            f"{len(layers)} layer{'s' if len(layers) != 1 else ''} on it."
        )
    return (
        f"Opened Saturn Studio {where}. The canvas is blank -- say what to "
        f"design and it will be drawn."
    )


def _show(studio, args: dict) -> str:
    result = studio.open_page()
    if _clean(result.get("error")):
        return f"Saturn Studio could not come on screen: {result['error']}"
    if studio.is_on_screen():
        return "Saturn Studio is on screen."
    return "Saturn Studio was put on screen, but the page has not confirmed it."


def _hide(studio, args: dict) -> str:
    studio.put_away()
    return (
        "Put Saturn Studio away. It keeps the document and its undo history, "
        "so it comes back exactly as it was."
    )


def _status(studio, args: dict) -> str:
    if not studio.wait_ready(timeout=6.0):
        return "Saturn Studio has not reported its canvas as ready."
    state = studio.state()
    if not state:
        return "Saturn Studio did not answer the status request."
    layers = state.get("layers") or []
    return (
        f"Saturn Studio is ready: {state.get('doc', 'untitled')}, page "
        f"{state.get('page', 'unknown')}, {len(layers)} top-level layer"
        f"{'s' if len(layers) != 1 else ''}, {state.get('nodes', 0)} nodes in "
        f"all, holding the {state.get('tool', 'unknown')} tool."
    )


def _tools(studio, args: dict) -> str:
    if not studio.wait_ready(timeout=6.0):
        return "Saturn Studio has not reported its canvas as ready."
    catalog = studio.catalog()
    if not catalog:
        return "Saturn Studio did not say which tools it has."
    names = ", ".join(_clean(tool.get("name")) for tool in catalog if tool.get("name"))
    return f"Saturn Studio's engine answers {len(catalog)} tools: {names}."


# --------------------------------------------------------------------------- #
# The verbs that draw
# --------------------------------------------------------------------------- #


def _design(studio, args: dict) -> str:
    """The one the user asked for: hear a brief, draw it, say what happened."""
    brief = _brief_of(args)
    if not brief:
        return "Tell Saturn what to design, and it will draw it on the canvas."

    result = studio.open_page()
    if _clean(result.get("error")):
        return f"Saturn Studio could not start: {result['error']}"
    waiting = _ready(studio)
    if waiting:
        return waiting

    jsx, failure = _compose_jsx(brief, _clean(args.get("style")))
    if not jsx:
        return f"{failure}, so nothing was drawn."

    answer = studio.run_tool("render", {"jsx": jsx, **_placement(args)})
    said = _report(answer)
    if said:
        return said

    drawn = answer.get("result") or {}
    name = _clean(drawn.get("name")) or "a layer"
    kind = _clean(drawn.get("type")) or "unknown"
    children = len(drawn.get("children") or [])
    studio.view("zoom")

    report = (
        f"Designed {name} on the canvas as a {kind}, with {children} "
        f"child layer{'s' if children != 1 else ''}."
    )
    warnings = drawn.get("warnings") or []
    if warnings:
        report += (
            f" The engine noted {len(warnings)} thing{'s' if len(warnings) != 1 else ''} "
            f"to look at: {'; '.join(_clean(w) for w in warnings[:3])}."
        )
    return report


def _run_engine_tool(studio, args: dict) -> str:
    """One engine tool, called by name, with the arguments passed straight."""
    name = _clean(args.get("tool") or args.get("name") or args.get("verb"))
    if not name:
        return "Saturn Studio needs the name of the engine tool to run."
    payload = args.get("arguments") or args.get("args") or {}
    if not isinstance(payload, dict):
        return (
            f"Saturn Studio could not read the arguments for {name}: "
            f"they were not a set of named values."
        )

    answer = studio.run_tool(name, payload)
    said = _report(answer)
    if said:
        return said
    result = answer.get("result")
    if isinstance(result, dict) and result:
        summary = ", ".join(f"{key} {_clean(value)}" for key, value in list(result.items())[:4])
        return f"Saturn Studio ran {name} and answered: {summary}."
    return f"Saturn Studio ran {name}."


def _clear(studio, args: dict) -> str:
    if not studio.new_document().get("ok"):
        return "Saturn Studio did not answer the request for a new document."
    return (
        "Started a blank document. The engine keeps the previous one in its own "
        "memory only while Studio is open, so save first if it mattered."
    )


def _save(studio, args: dict) -> str:
    answer = studio.save_document()
    said = _report(answer)
    if said:
        return said
    return f"Wrote the document to {answer.get('path')}."


def _open_file(studio, args: dict) -> str:
    from pathlib import Path as _Path

    raw = _clean(args.get("path") or args.get("file"))
    if not raw:
        return "Saturn Studio needs a .saturn document to open."
    path = _Path(raw).expanduser()
    if not path.is_file():
        return f"There is no {path} to open."
    if path.suffix.lower() != ".saturn":
        return (f"{path.name} is not a .saturn document, so Saturn Studio will not "
                f"open it. It reads its own format, not .fig files.")

    answer = studio.open_document(path)
    said = _report(answer)
    if said:
        return said
    doc = _clean(answer.get("doc")) or path.stem
    state = studio.state()
    layers = state.get("layers") or []
    return (
        f"Opened {doc} in Saturn Studio: {len(layers)} top-level layer"
        f"{'s' if len(layers) != 1 else ''}."
    )


def _set_tool(studio, args: dict) -> str:
    name = _clean(args.get("tool") or args.get("name")).upper()
    if not name:
        state = studio.state()
        return f"The canvas is holding the {state.get('tool', 'unknown')} tool."
    if not studio.set_tool(name).get("ok"):
        return "Saturn Studio did not answer the tool change."
    return f"Saturn Studio is holding {name}."


def _view(studio, args: dict, kind: str) -> str:
    answer = studio.view(kind)
    said = _report(answer)
    if said:
        return said
    words = {"zoom": "Framed the whole document on screen.",
             "undo": "Undone the last change on the canvas.",
             "redo": "Redone the change on the canvas."}
    return words.get(kind, f"Ran {kind} on the canvas.")


# --------------------------------------------------------------------------- #
# The verbs that read the canvas back
# --------------------------------------------------------------------------- #


def _read(studio, args: dict) -> str:
    """What is on the board, in the graph's own words."""
    state = studio.state()
    if not state:
        return "Saturn Studio did not answer the request to read the canvas."
    layers = state.get("layers") or []
    if not layers:
        return f"{state.get('doc', 'The document')} is empty -- nothing drawn on it yet."

    parts = []
    for layer in layers[:8]:
        children = layer.get("children") or 0
        parts.append(
            f"{_clean(layer.get('name')) or 'unnamed'} "
            f"({_clean(layer.get('type')) or 'layer'}"
            f"{f', {children} children' if children else ''})"
        )
    line = "; ".join(parts)
    if len(layers) > 8:
        line += f"; and {len(layers) - 8} more"
    return f"{state.get('doc', 'The document')} has {len(layers)} top-level layers: {line}."


def _describe(studio, args: dict) -> str:
    """One layer, as the engine describes it."""
    target = _clean(args.get("id") or args.get("node"))
    if not target:
        layers = studio.state().get("layers") or []
        if not layers:
            return "There is nothing on the canvas to describe."
        target = _clean(layers[-1].get("id"))
        if not target:
            return "The canvas did not say which layer to describe."

    answer = studio.run_tool("describe", {"id": target})
    said = _report(answer)
    if said:
        return said

    described = answer.get("result") or {}
    if not isinstance(described, dict) or described.get("error"):
        return _clean(described.get("error")) or "Saturn Studio had nothing to say about that layer."

    name = _clean(described.get("name")) or "A layer"
    kind = _clean(described.get("type")) or "unknown"
    size = _clean(described.get("size"))
    role = _clean(described.get("role"))
    line = f"{name} is a {kind}"
    if role:
        line += f" that reads as {role}"
    line += "."
    if size:
        line += f" It measures {size}."
    children = described.get("children")
    if isinstance(children, list) and children:
        line += f" It holds {len(children)} layer{'s' if len(children) != 1 else ''}."
    issues = described.get("issues")
    if isinstance(issues, list) and issues:
        line += f" The engine flags {len(issues)} thing{'s' if len(issues) != 1 else ''}."
    return line


HANDLERS: dict[str, Callable[..., str]] = {
    "open": _open,
    "show": _show,
    "close": _hide,
    "hide": _hide,
    "status": _status,
    "tools": _tools,
    # The verb the whole feature is for.
    "design": _design,
    "draw": _design,
    "sketch": _design,
    # One engine tool, named.
    "run": _run_engine_tool,
    "exec": _run_engine_tool,
    "apply": _run_engine_tool,
    "clear": _clear,
    "new": _clear,
    "save": _save,
    "open_file": _open_file,
    "load": _open_file,
    "tool": _set_tool,
    "zoom": lambda studio, args: _view(studio, args, "zoom"),
    "fit": lambda studio, args: _view(studio, args, "zoom"),
    "undo": lambda studio, args: _view(studio, args, "undo"),
    "redo": lambda studio, args: _view(studio, args, "redo"),
    "read": _read,
    "look": _read,
    "describe": _describe,
    "inspect": _describe,
}


# --------------------------------------------------------------------------- #


def saturn_studio(parameters: Optional[dict] = None, *, parent: Any = None) -> str:
    """Answer Saturn's design tool, from any thread.

    The window is not built here: Saturn's rail constructs the Studio page while
    the app comes up, so asking for a canvas that does not exist is a fact worth
    saying out loud rather than a widget built on a thread that owns none.
    """
    args = dict(parameters or {})
    action = _clean(args.get("action") or args.get("verb")).lower() or "open"

    handler = HANDLERS.get(action)
    if handler is None:
        # Answered before the host is imported: a mistyped verb is a vocabulary
        # problem, and bringing up the canvas to report one is an expensive way
        # to say "no".
        return (
            f"Saturn Studio has no {action} action. It answers {VOCABULARY}."
        )

    from saturn_studio import window as studio_host

    studio = studio_host.get_window(parent=parent)
    if studio is None:
        return (
            "Saturn Studio is not available on this machine right now, "
            "so nothing was drawn."
        )

    try:
        return handler(studio, args)
    except Exception as exc:  # noqa: BLE001 -- a tool call must answer, not raise
        logger.exception("studio action %s failed", action)
        return f"Saturn Studio did not answer the {action} request: {exc}"
