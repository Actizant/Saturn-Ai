"""Saturn Studio -- the engine verbs of the native canvas (Milestone 1).

``saturn_studio.tool`` speaks to the canvas through one seam: a named tool, a
dict of arguments, and a dict answer of the shape ``{ok, result}`` or
``{ok, error}``. The web engine filled that seam from ~1,290 files of
vendored TypeScript; this file fills it from a clean room, using only the
scene model, the JSX reader and the runtime state. It imports no painter --
every verb here is model logic, so the same answers hold whatever backend
eventually draws them.

Three promises, kept everywhere in this module:

* **Same names, same arguments, same answer keys** as the engine verbs the
  router already calls, so ``tool.py`` does not learn a new language.
* **Nothing claimed that the scene did not say.** Sizes, child ids, colours
  and roles are read off the nodes, never estimated into the reply.
* **Honest limits.** Milestone 2 runs a real auto-layout pass, so a ``flex``
  container is laid out and answered with its measured geometry; a container
  with no ``flex`` whose children gave no coordinates is still *stacked*, and
  the reply says so. What is still not shipped is answered by name, never with
  a number the engine never produced: ``eval`` says the sandbox is not there yet.

Reply shapes mirror, key for key, the reference tools they replace:
``render`` -> ``create/render.ts``, ``describe`` -> ``describe/*``,
``get_node`` -> ``read/nodes.ts`` + ``figma-api/serialization.ts``, the
paint and geometry verbs -> ``modify/*``.
"""

from __future__ import annotations

import ast
import base64
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from saturn_studio.native import jsx, scene
from saturn_studio.native.state import StudioState


class ToolError(Exception):
    """Arguments could not be read -- the engine's ``Invalid arguments`` path.
    :func:`run` turns it into ``{ok: false, error}``, exactly as the web
    shell's ``callTool`` catches a throw and answers through the mailbox."""


# --------------------------------------------------------------------------- #
# Small readers for the argument dictionaries the router hands over
# --------------------------------------------------------------------------- #

def _num(value: Any) -> Optional[float]:
    """A number that may arrive as a string (``toolNumber`` in the engine).
    None in, None out; garbage raises so the verb reports reading it failed."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ToolError(f"{value!r} is not a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{value!r} is not a number")


def _number(args: dict, key: str, *, required: bool = False,
             minimum: Optional[float] = None, maximum: Optional[float] = None,
             name: str = "") -> Optional[float]:
    if key not in args and required:
        raise ToolError(f"Invalid arguments for {name}: missing '{key}'")
    value = _num(args.get(key))
    if value is None and required:
        raise ToolError(f"Invalid arguments for {name}: '{key}' was not a number")
    if value is not None:
        if minimum is not None and value < minimum:
            raise ToolError(f"Invalid arguments for {name}: '{key}' must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ToolError(f"Invalid arguments for {name}: '{key}' must be <= {maximum}")
    return value


def _string(args: dict, key: str, *, required: bool = False, name: str = "") -> Optional[str]:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"Invalid arguments for {name}: missing '{key}'")
        return None
    return str(value)


def _one_of(args: dict, key: str, allowed: tuple[str, ...], name: str = "") -> Optional[str]:
    value = args.get(key)
    if value is None:
        return None
    up = str(value).upper()
    if up not in allowed:
        raise ToolError(f"Invalid arguments for {name}: '{key}' must be one of {list(allowed)}")
    return up


def _node(state: StudioState, node_id: str) -> Optional[scene.Node]:
    return state.document.find_by_id(str(node_id))


def _fmt(value: float) -> str:
    """Print a size the way JavaScript does: 640, not 640.0; 45.9 stays 45.9."""
    f = float(value)
    return str(int(f)) if f.is_integer() else repr(round(f, 6))


def _not_found(node_id: Any) -> dict:
    return {"error": f'Node "{node_id}" not found'}


# --------------------------------------------------------------------------- #
# Roles and visual summaries, ported from describe/{roles,summaries}.ts
# --------------------------------------------------------------------------- #

_NAME_ROLES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^icon$", re.I), "icon"), (re.compile(r"^icon[-_]", re.I), "icon"),
    (re.compile(r"^button$", re.I), "button"), (re.compile(r"^btn[-_\s]", re.I), "button"),
    (re.compile(r"[-_\s]btn$", re.I), "button"), (re.compile(r"^cta$", re.I), "button"),
    (re.compile(r"^icon[-_]?button$", re.I), "button"),
    (re.compile(r"^link$", re.I), "link"), (re.compile(r"^text[-_]?link$", re.I), "link"),
    (re.compile(r"^input$", re.I), "textbox"), (re.compile(r"^text[-_]?field$", re.I), "textbox"),
    (re.compile(r"^search$", re.I), "searchbox"), (re.compile(r"^checkbox$", re.I), "checkbox"),
    (re.compile(r"^toggle$", re.I), "switch"), (re.compile(r"^switch$", re.I), "switch"),
    (re.compile(r"^radio$", re.I), "radio"), (re.compile(r"^select$", re.I), "combobox"),
    (re.compile(r"^dropdown$", re.I), "combobox"), (re.compile(r"^slider$", re.I), "slider"),
    (re.compile(r"^tab$", re.I), "tab"), (re.compile(r"^tabs$", re.I), "tablist"),
    (re.compile(r"^nav(bar|igation)?$", re.I), "navigation"),
    (re.compile(r"^header$", re.I), "banner"), (re.compile(r"^footer$", re.I), "contentinfo"),
    (re.compile(r"^sidebar$", re.I), "complementary"), (re.compile(r"^modal$", re.I), "dialog"),
    (re.compile(r"^dialog$", re.I), "dialog"), (re.compile(r"^tooltip$", re.I), "tooltip"),
    (re.compile(r"^card$", re.I), "article"), (re.compile(r"^avatar$", re.I), "img"),
    (re.compile(r"^badge$", re.I), "status"), (re.compile(r"^toast$", re.I), "alert"),
    (re.compile(r"^alert$", re.I), "alert"), (re.compile(r"^list$", re.I), "list"),
    (re.compile(r"^menu$", re.I), "menu"), (re.compile(r"^breadcrumb", re.I), "navigation"),
    (re.compile(r"^progress$", re.I), "progressbar"), (re.compile(r"^spinner$", re.I), "progressbar"),
    (re.compile(r"^divider$", re.I), "separator"), (re.compile(r"^separator$", re.I), "separator"),
]

_BUTTON_MAX_W, _BUTTON_MAX_H, _BUTTON_MIN_H, _BUTTON_MIN_R = 200, 50, 28, 2


def _role_from_name(name: str) -> Optional[str]:
    base = re.split(r"[/,=]", name)[0].strip()
    for pattern, role in _NAME_ROLES:
        if pattern.search(base) or pattern.search(name):
            return role
    return None


def _heading_level(size: float) -> Optional[int]:
    if size >= 32:
        return 1
    if size >= 24:
        return 2
    if size >= 20:
        return 3
    if size >= 18:
        return 4
    return None


def _looks_like_separator(node: scene.Node) -> bool:
    if node.width <= 2 < node.height and node.height > 10:
        return True
    if node.height <= 2 < node.width and node.width > 10:
        return True
    lo, hi = min(node.width, node.height), max(node.width, node.height)
    return (hi / max(1, lo)) > 10 and lo <= 4


def _looks_like_button(node: scene.Node) -> bool:
    if node.type != scene.FRAME:
        return False
    if node.width > _BUTTON_MAX_W or node.height > _BUTTON_MAX_H or node.height < _BUTTON_MIN_H:
        return False
    if not node.fills and not node.strokes:
        return False
    if node.corner_radius < _BUTTON_MIN_R:
        return False
    return len(node.children) > 0


def detect_role(node: scene.Node) -> str:
    named = _role_from_name(node.name)
    if named:
        return named
    if node.type == scene.TEXT and node.text is not None:
        level = _heading_level(node.text.size)
        return f"heading({level})" if level else "StaticText"
    if _looks_like_separator(node):
        return "separator"
    if _looks_like_button(node):
        return "button"
    return "generic"


def _solid_fill(node: scene.Node) -> Optional[scene.Fill]:
    for fill in node.fills:
        if fill.kind == "solid" and fill.visible:
            return fill
    return None


def _visible_stroke(node: scene.Node) -> Optional[scene.Stroke]:
    for stroke in node.strokes:
        if stroke.visible:
            return stroke
    return None


def _stroke_summary(stroke: scene.Stroke) -> str:
    return f"{stroke.color.to_hex()} {round(stroke.weight * 100) / 100}px stroke"


def describe_visual(node: scene.Node) -> str:
    parts: list[str] = []
    fill = _solid_fill(node)
    if fill:
        parts.append(f"{fill.color.to_hex()} fill")
    stroke = _visible_stroke(node)
    if stroke:
        parts.append(_stroke_summary(stroke))
    if node.corner_radius > 0:
        parts.append("rounded")
    return ", ".join(parts) or "no visual styles"


def _summarize_container(node: scene.Node) -> str:
    parts = [f"{_fmt(node.width)}×{_fmt(node.height)}"]
    fill = _solid_fill(node)
    if fill:
        parts.append(fill.color.to_hex())
    stroke = _visible_stroke(node)
    if stroke:
        parts.append(_stroke_summary(stroke))
    if node.corner_radius > 0:
        parts.append("rounded")
    return ", ".join(parts)


def _summarize_text(node: scene.Node) -> str:
    t = node.text
    text = (t.chars or "")[:60]
    summary = f'"{text}" {_fmt(t.size)}px {t.family}'
    if t.weight >= 700:
        summary += " bold"
    elif t.weight >= 500:
        summary += " medium"
    fill = _solid_fill(node)
    if fill:
        summary += f", {fill.color.to_hex()}"
    mode = scene._auto_resize_mode(t)
    if mode == "HEIGHT":
        summary += ", wraps"
    elif mode == "NONE":
        summary += ", fixed-size"
    return summary


def _describe_child(node: scene.Node, depth: int) -> dict:
    summary = _summarize_text(node) if node.type == scene.TEXT else _summarize_container(node)
    result: dict[str, Any] = {
        "role": detect_role(node), "name": node.name,
        "summary": summary, "id": node.id,
    }
    if depth > 0 and node.children:
        kids = [_describe_child(c, depth - 1) for c in node.children if c.visible]
        if kids:
            result["children"] = kids
    return result


def _auto_depth(node: scene.Node) -> int:
    size = len(node.descendants())
    if size <= 15:
        return 4
    if size <= 40:
        return 3
    if size <= 100:
        return 2
    return 1


def _describe_one(node: scene.Node, depth: Optional[int] = None) -> dict:
    if depth is None:
        depth = _auto_depth(node)
    result: dict[str, Any] = {
        "id": node.id, "name": node.name, "type": node.type,
        "role": detect_role(node),
        "size": f"{_fmt(node.width)}×{_fmt(node.height)}",
        "visual": describe_visual(node),
    }
    # A real layout is reported as what it is: direction, spacing, padding.
    # A box that runs none gets no layout key -- not a guessed one.
    if node.layout.active:
        result["layout"] = _describe_layout(node)
    visible = [c for c in node.children if c.visible]
    if visible and depth > 0:
        result["children"] = [_describe_child(c, depth - 1) for c in visible]
    return result


def _describe_layout(node) -> str:
    """One honest line on how a container arranges its children, in the plain
    words Saturn quotes -- direction, gap, padding, and any distribution."""
    lay = node.layout
    direction = "row" if lay.mode == scene.FLEX_ROW else "column"
    parts = [f"auto-layout {direction}"]
    if lay.gap:
        parts.append(f"{_fmt(lay.gap)} gap")
    pads = {round(lay.pad_top), round(lay.pad_right), round(lay.pad_bottom), round(lay.pad_left)}
    if pads != {0}:
        parts.append(f"{_fmt(lay.pad_top)}/{_fmt(lay.pad_right)}/{_fmt(lay.pad_bottom)}/{_fmt(lay.pad_left)} padding")
    if lay.primary_align == scene.ALIGN_SPACE:
        parts.append("space-between")
    elif lay.primary_align == scene.ALIGN_CENTER:
        parts.append("centered on main axis")
    elif lay.primary_align == scene.ALIGN_MAX:
        parts.append("end-aligned")
    if lay.counter_align == scene.ALIGN_CENTER:
        parts.append("centered across")
    elif lay.counter_align == scene.ALIGN_STRETCH:
        parts.append("stretched across")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# The gradient directions set_fill understands, mapped to the unit-square axis
# --------------------------------------------------------------------------- #

_GRADIENT_ENDS = {
    "TOP-BOTTOM": (0.5, 0.0, 0.5, 1.0),
    "BOTTOM-TOP": (0.5, 1.0, 0.5, 0.0),
    "LEFT-RIGHT": (0.0, 0.5, 1.0, 0.5),
    "RIGHT-LEFT": (1.0, 0.5, 0.0, 0.5),
}


# --------------------------------------------------------------------------- #
# The verbs. Each is registered below with its description and mutates flag.
# --------------------------------------------------------------------------- #

@dataclass
class Verb:
    name: str
    description: str
    mutates: bool
    handler: Callable[[StudioState, dict], Any]


REGISTRY: dict[str, Verb] = {}


def verb(name: str, description: str, mutates: bool):
    def deco(fn: Callable[[StudioState, dict], Any]) -> Callable[..., Any]:
        REGISTRY[name] = Verb(name, description, mutates, fn)
        return fn
    return deco


# -- read: selection -------------------------------------------------------- #

@verb("get_selection",
      "Get the selected nodes, each with its direct children by default (deeper "
      "levels counted as childCount). The place to start when the user points at layers.",
      False)
def _get_selection(state, args):
    depth = _number(args, "depth", name="get_selection")
    depth = 1 if depth is None else int(depth)
    return {"selection": [n.to_json(depth) for n in state.selected_nodes()]}


@verb("select_nodes", "Select one or more nodes by ID.", True)
def _select_nodes(state, args):
    ids = args.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ToolError("Invalid arguments for select_nodes: 'ids' must be a non-empty list")
    state.select([str(i) for i in ids if _node(state, i) is not None])
    return {"selected": [str(i) for i in ids]}


# -- read: nodes ------------------------------------------------------------ #

@verb("get_node",
      "Detailed properties of a node by ID. depth limits child recursion "
      "(0 = node only, 1 = direct children). Default: unlimited.",
      False)
def _get_node(state, args):
    node_id = _string(args, "id", required=True, name="get_node")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    depth = _number(args, "depth", name="get_node")
    return node.to_json(None if depth is None else int(depth))


@verb("find_nodes", "Find nodes by name substring and/or type.", False)
def _find_nodes(state, args):
    name = args.get("name")
    type_ = _one_of(args, "type", (
        "FRAME", "RECTANGLE", "ELLIPSE", "TEXT", "LINE", "PAGE",
    ), name="find_nodes")
    needle = str(name).lower() if name else None
    matches = []
    for node in state.document.current_page.descendants():
        if type_ and node.type != type_:
            continue
        if needle and needle not in node.name.lower():
            continue
        matches.append({"id": node.id, "name": node.name, "type": node.type})
    return {"count": len(matches), "nodes": matches}


# -- create ----------------------------------------------------------------- #

@verb("render",
      "Render design JSX into a new layer. Returns the created node's id, name, "
      "type and child ids, plus a warning for anything that had to be stacked "
      "because its container declared no flex and gave no coordinates.",
      True)
def _render(state, args):
    markup = _string(args, "jsx", required=True, name="render")
    try:
        root, warnings = jsx.build(markup, getattr(state, "measure", None))
    except jsx.JsxError as exc:
        raise ToolError(f"render could not read the JSX: {exc}") from exc

    state.snapshot()
    parent_id = _string(args, "parent_id", name="render")
    replace_id = _string(args, "replace_id", name="render")
    insert_index = _number(args, "insert_index", name="render")

    parent = (_node(state, parent_id) if parent_id else None) or state.document.current_page
    if not parent.is_container:
        raise ToolError(f"render: parent '{parent_id}' cannot hold children")

    index = None if insert_index is None else int(insert_index)
    if replace_id:
        target = _node(state, replace_id)
        if target is None:
            return _not_found(replace_id)
        index = target.index_in_parent()
        host = target.parent or state.document.current_page
        if not host.is_container:
            host = state.document.current_page
        parent = host
        target.detach()
        root.x, root.y = target.x, target.y

    x = _number(args, "x", name="render")
    y = _number(args, "y", name="render")
    if x is not None:
        root.x = x
    if y is not None:
        root.y = y

    parent.add_child(root, index)

    reply: dict[str, Any] = {
        "id": root.id, "name": root.name, "type": root.type,
        "children": [c.id for c in root.children],
    }
    if warnings:
        reply["warnings"] = warnings
    return reply


# -- modify ----------------------------------------------------------------- #

def _apply_props(node: scene.Node, args: dict, *, name: str) -> list[str]:
    """Write every recognised property from ``args`` onto ``node`` and name the
    ones that changed. Shared verbatim by ``update_node`` and ``batch_update``
    so a batch cannot quietly mean something different from a single edit."""
    updated: list[str] = []

    for key, field in (("x", "x"), ("y", "y")):
        value = _number(args, key, name=name)
        if value is not None:
            setattr(node, field, value)
            updated.append(key)

    width = _number(args, "width", minimum=1, name=name)
    height = _number(args, "height", minimum=1, name=name)
    if width is not None or height is not None:
        node.width = width if width is not None else node.width
        node.height = height if height is not None else node.height
        updated.append("size")

    opacity = _number(args, "opacity", minimum=0, maximum=1, name=name)
    if opacity is not None:
        node.opacity = opacity
        updated.append("opacity")

    radius = _number(args, "corner_radius", minimum=0, name=name)
    if radius is not None:
        node.corner_radius = radius
        updated.append("cornerRadius")

    if "visible" in args:
        node.visible = bool(args["visible"])
        updated.append("visible")

    name_value = _string(args, "name", name=name)
    if name_value is not None:
        node.name = name_value
        updated.append("name")

    text = _string(args, "text", name=name)
    if text is not None and node.text is not None:
        node.text.chars = text
        updated.append("text")

    fs = _number(args, "font_size", minimum=1, name=name)
    if fs is not None and node.text is not None:
        node.text.size = fs
        updated.append("fontSize")

    fw = _number(args, "font_weight", minimum=100, maximum=900, name=name)
    if fw is not None and node.text is not None:
        node.text.weight = int(fw)
        updated.append("fontWeight")

    return updated


@verb("update_node",
      "Update properties of an existing node: position, size, opacity, corner "
      "radius, visibility, text, font, name.",
      True)
def _update_node(state, args):
    node_id = _string(args, "id", required=True, name="update_node")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    state.snapshot()
    updated = _apply_props(node, args, name="update_node")
    return {"id": node.id, "updated": updated}


@verb("batch_update",
      "Apply update_node-style property changes to many nodes in one step, "
      "undoable as a single change. Pass 'updates': a list of objects each with "
      "an 'id' plus any of x, y, width, height, opacity, corner_radius, "
      "visible, name, text, font_size, font_weight.",
      True)
def _batch_update(state, args):
    updates = args.get("updates")
    if not isinstance(updates, list) or not updates:
        raise ToolError("Invalid arguments for batch_update: 'updates' must be a non-empty list")
    state.snapshot()
    results: list[dict] = []
    for item in updates:
        if not isinstance(item, dict):
            raise ToolError("Invalid arguments for batch_update: each update must be an object with an 'id'")
        node_id = _string(item, "id", required=True, name="batch_update")
        node = _node(state, node_id)
        if node is None:
            results.append({"id": node_id, "error": f'Node "{node_id}" not found'})
            continue
        results.append({"id": node.id, "updated": _apply_props(node, item, name="batch_update")})
    return {"count": len(results), "results": results}


@verb("set_fill",
      'Set fill on a node. Solid: color="#ff0000". Linear gradient: '
      'gradient="top-bottom" or "left-right" with color (start) and color_end (end).',
      True)
def _set_fill(state, args):
    node_id = _string(args, "id", required=True, name="set_fill")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    color = _string(args, "color", required=True, name="set_fill")
    color_end = _string(args, "color_end", name="set_fill")
    gradient = _one_of(args, "gradient",
                       ("TOP-BOTTOM", "BOTTOM-TOP", "LEFT-RIGHT", "RIGHT-LEFT"), name="set_fill")
    state.snapshot()

    start = scene.Color.parse(color)
    if gradient and color_end:
        end = scene.Color.parse(color_end)
        x1, y1, x2, y2 = _GRADIENT_ENDS[gradient]
        fill = scene.Fill(
            kind="linear", color=start,
            stops=[scene.GradientStop(start, 0.0), scene.GradientStop(end, 1.0)],
            x1=x1, y1=y1, x2=x2, y2=y2,
        )
        node.fills = [fill]
        return {"id": node.id, "gradient": gradient.lower(),
                "start": start.to_dict(), "end": end.to_dict()}

    node.fills = [scene.Fill.solid(start)]
    return {"id": node.id, "color": start.to_dict()}


@verb("set_stroke", "Set the stroke (border) of a node.", True)
def _set_stroke(state, args):
    node_id = _string(args, "id", required=True, name="set_stroke")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    color = _string(args, "color", required=True, name="set_stroke")
    weight = _number(args, "weight", minimum=0.1, name="set_stroke")
    align = _one_of(args, "align", ("INSIDE", "CENTER", "OUTSIDE"), name="set_stroke")
    state.snapshot()
    c = scene.Color.parse(color)
    node.strokes = [scene.Stroke(color=c, weight=weight if weight is not None else 1.0,
                                 align=(align or "INSIDE").lower())]
    return {"id": node.id, "color": c.to_dict(), "weight": weight if weight is not None else 1}


@verb("set_text", "Set text content of a text node.", True)
def _set_text(state, args):
    node_id = _string(args, "id", required=True, name="set_text")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    text = _string(args, "text", required=True, name="set_text")
    if node.type != scene.TEXT:
        return {"error": f'Node "{node_id}" is not a TEXT node'}
    state.snapshot()
    node.text.chars = text
    return {"id": node.id, "text": text}


_H_PROP = {"LEFT": "left", "CENTER": "center", "RIGHT": "right", "JUSTIFIED": "justify"}
_V_PROP = {"TOP": "top", "CENTER": "center", "BOTTOM": "bottom"}


@verb("set_text_properties",
      "Set text layout properties: horizontal and vertical alignment, auto-resize.",
      True)
def _set_text_properties(state, args):
    node_id = _string(args, "id", required=True, name="set_text_properties")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    if node.type != scene.TEXT:
        return {"error": f'Node "{node_id}" is not a TEXT node'}
    state.snapshot()
    t = node.text
    updated: list[str] = []

    ah = _one_of(args, "align_horizontal", ("LEFT", "CENTER", "RIGHT", "JUSTIFIED"),
                name="set_text_properties")
    if ah:
        t.align = _H_PROP[ah]
        updated.append("textAlignHorizontal")
    av = _one_of(args, "align_vertical", ("TOP", "CENTER", "BOTTOM"), name="set_text_properties")
    if av:
        t.valign = _V_PROP[av]
        updated.append("textAlignVertical")
    resize = _one_of(args, "auto_resize",
                     ("NONE", "WIDTH_AND_HEIGHT", "HEIGHT", "TRUNCATE"),
                     name="set_text_properties")
    if resize:
        t.auto_width = resize in ("WIDTH_AND_HEIGHT",)
        t.auto_height = resize in ("WIDTH_AND_HEIGHT", "HEIGHT")
        updated.append("textAutoResize")
    return {"id": node.id, "updated": updated}


@verb("set_radius", "Set corner radius. Use individual corners for independent values.", True)
def _set_radius(state, args):
    node_id = _string(args, "id", required=True, name="set_radius")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    radius = _number(args, "radius", minimum=0, name="set_radius")
    tl = _number(args, "top_left", minimum=0, name="set_radius")
    tr = _number(args, "top_right", minimum=0, name="set_radius")
    br = _number(args, "bottom_right", minimum=0, name="set_radius")
    bl = _number(args, "bottom_left", minimum=0, name="set_radius")
    state.snapshot()

    if radius is not None:
        node.corner_radius = radius
        node.top_left_radius = node.top_right_radius = None
        node.bottom_right_radius = node.bottom_left_radius = None
        return {"id": node.id, "cornerRadius": radius}

    individual = {"top_left": tl, "top_right": tr, "bottom_right": br, "bottom_left": bl}
    if any(v is not None for v in individual.values()):
        node.top_left_radius = tl
        node.top_right_radius = tr
        node.bottom_right_radius = br
        node.bottom_left_radius = bl
        return {
            "id": node.id,
            "topLeftRadius": node.top_left_radius, "topRightRadius": node.top_right_radius,
            "bottomRightRadius": node.bottom_right_radius, "bottomLeftRadius": node.bottom_left_radius,
        }
    return {"id": node.id, "cornerRadius": node.corner_radius}


# -- structure -------------------------------------------------------------- #

@verb("delete_node", "Delete a node by ID.", True)
def _delete_node(state, args):
    node_id = _string(args, "id", required=True, name="delete_node")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    state.snapshot()
    node.detach()
    state.select([i for i in state.selection if i != node_id])
    return {"deleted": node.id}


@verb("node_resize", "Resize a node.", True)
def _node_resize(state, args):
    node_id = _string(args, "id", required=True, name="node_resize")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    width = _number(args, "width", required=True, minimum=1, name="node_resize")
    height = _number(args, "height", required=True, minimum=1, name="node_resize")
    state.snapshot()
    node.width, node.height = width, height
    return {"id": node.id, "width": width, "height": height}


@verb("reparent_node", "Move a node into a different parent.", True)
def _reparent_node(state, args):
    node_id = _string(args, "id", required=True, name="reparent_node")
    parent_id = _string(args, "parent_id", required=True, name="reparent_node")
    node = _node(state, node_id)
    parent = _node(state, parent_id)
    if node is None:
        return _not_found(node_id)
    if parent is None:
        return {"error": f'Parent "{parent_id}" not found'}
    if not parent.is_container:
        return {"error": f'Parent "{parent_id}" cannot hold children'}
    state.snapshot()
    node.reparent(parent)
    return {"id": node.id, "parent_id": parent.id}


# -- layer rail: stacking order --------------------------------------------- #

@verb("reorder_node",
      "Move a layer within its parent's stacking order -- the layer rail. Pass "
      "'to' as front, back, forward or backward, or an explicit 'index' (0 = "
      "back-most). The node stays in the same parent.",
      True)
def _reorder_node(state, args):
    node_id = _string(args, "id", required=True, name="reorder_node")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    parent = node.parent
    if parent is None:
        return {"error": f'Node "{node_id}" has no parent to reorder within'}
    state.snapshot()

    siblings = list(parent.children)
    current = siblings.index(node)
    siblings.pop(current)                       # remaining slots, indices after shift down
    explicit = _number(args, "index", minimum=0, name="reorder_node")
    keyword = str(args["to"]).strip().lower() if args.get("to") is not None else None
    if explicit is not None:
        dest = int(explicit)
    elif keyword == "front":
        dest = len(siblings)
    elif keyword == "back":
        dest = 0
    elif keyword == "forward":
        dest = min(current + 1, len(siblings))
    elif keyword == "backward":
        dest = max(current - 1, 0)
    else:
        raise ToolError("Invalid arguments for reorder_node: pass 'to' "
                        "(front/back/forward/backward) or an 'index'")
    dest = max(0, min(dest, len(siblings)))
    siblings.insert(dest, node)
    parent.children = siblings
    return {"id": node.id, "index": dest, "previousIndex": current}


# -- auto layout: the container and its children (Milestone 3 verbs) --------- #

def _reflow(state, node) -> list[str]:
    """Re-run the flex pass over the top-level layer that owns ``node``.

    This is the exact scope a ``render`` lays out -- the design node whose
    parent is the page -- never the page itself, so a live edit reflows the
    same subtree the engine drew and nothing outside it moves."""
    top = node
    while top.parent is not None and top.parent.type != scene.PAGE:
        top = top.parent
    return top.run_layout(getattr(state, "measure", None))


def _direction_mode(token, *, name: str) -> str:
    t = str(token).strip().lower()
    if t in ("row", "horizontal"):
        return scene.FLEX_ROW
    if t in ("col", "column", "vertical"):
        return scene.FLEX_COL
    if t in ("none", ""):
        return scene.FLEX_NONE
    raise ToolError(f"Invalid arguments for {name}: 'mode' must be row, column or none")


@verb("set_layout",
      "Turn auto-layout on (or off) for a container and set its direction, gap, "
      "padding, alignment and wrap. Then the flex pass re-runs and repositions "
      "the container's children. mode: row|column|none; justify: start|center|"
      "end|between; items: start|center|end|stretch.",
      True)
def _set_layout(state, args):
    node_id = _string(args, "id", required=True, name="set_layout")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    if not node.is_container:
        return {"error": f'Node "{node_id}" is a {node.type} and cannot hold auto-layout'}
    state.snapshot()

    lay = node.layout
    if args.get("mode") is not None:
        lay.mode = _direction_mode(args["mode"], name="set_layout")
    gap = _number(args, "gap", minimum=0, name="set_layout")
    if gap is not None:
        lay.gap = gap
        if args.get("rowGap") is None:
            lay.row_gap = gap
    row_gap = _number(args, "rowGap", minimum=0, name="set_layout")
    if row_gap is not None:
        lay.row_gap = row_gap
    padding = _number(args, "padding", minimum=0, name="set_layout")
    if padding is not None:
        lay.pad_top = lay.pad_right = lay.pad_bottom = lay.pad_left = padding
    for key, field in (("paddingTop", "pad_top"), ("paddingRight", "pad_right"),
                       ("paddingBottom", "pad_bottom"), ("paddingLeft", "pad_left")):
        value = _number(args, key, minimum=0, name="set_layout")
        if value is not None:
            setattr(lay, field, value)
    if args.get("justify") is not None:
        lay.primary_align = jsx._main_align(args["justify"])
    if args.get("items") is not None:
        lay.counter_align = jsx._cross_align(args["items"])
    if "wrap" in args:
        lay.wrap = bool(args["wrap"])

    notes = _reflow(state, node)
    reply: dict[str, Any] = {"id": node.id, "layout": lay.to_dict()}
    if notes:
        reply["warnings"] = notes
    return reply


@verb("set_layout_child",
      "Set how a node sizes and aligns inside its auto-layout parent: "
      "sizing_horizontal / sizing_vertical (fill|hug|fixed), grow (main-axis "
      "weight), align_self (start|center|end|stretch|auto), absolute (leave the "
      "flow and keep x/y). Then the flex pass re-runs.",
      True)
def _set_layout_child(state, args):
    node_id = _string(args, "id", required=True, name="set_layout_child")
    node = _node(state, node_id)
    if node is None:
        return _not_found(node_id)
    if node.parent is None:
        return {"error": f'Node "{node_id}" is not inside a container'}
    state.snapshot()
    updated: list[str] = []

    sizing = _one_of(args, "sizing_horizontal", ("FIXED", "HUG", "FILL"), name="set_layout_child")
    if sizing:
        node.sizing_horizontal = sizing
        updated.append("sizingHorizontal")
    sizing_v = _one_of(args, "sizing_vertical", ("FIXED", "HUG", "FILL"), name="set_layout_child")
    if sizing_v:
        node.sizing_vertical = sizing_v
        updated.append("sizingVertical")

    grow = _number(args, "grow", minimum=0, name="set_layout_child")
    if grow is not None:
        node.grow = grow
        updated.append("grow")

    if args.get("align_self") is not None:
        token = str(args["align_self"]).strip().lower()
        node.align_self = None if token == "auto" else jsx._cross_align(token)
        updated.append("alignSelf")

    if "absolute" in args:
        node.absolute = bool(args["absolute"])
        updated.append("absolute")

    notes = _reflow(state, node)
    reply: dict[str, Any] = {"id": node.id, "updated": updated}
    if notes:
        reply["warnings"] = notes
    return reply


# -- export: paint to PNG / SVG through the attached painter ----------------- #

def _export_target(state, args, *, name: str):
    """The node to export, or a soft error. Defaults to the whole page."""
    node_id = _string(args, "id", name=name)
    if not node_id:
        return state.document.current_page, None
    node = _node(state, node_id)
    if node is None:
        return None, _not_found(node_id)
    return node, None


def _deliver(state, data: bytes, *, ext: str, args: dict, name: str,
             scale: Optional[float] = None) -> dict:
    """Hand exported bytes back through the truth: write to the studio folder
    when the host attached one, otherwise base64 the bytes into the reply. The
    size is always the byte count actually produced, never a claim."""
    out_dir = getattr(state, "export_dir", None)
    filename = _string(args, "filename", name=name)
    reply: dict[str, Any] = {"format": ext, "bytes": len(data)}
    if scale is not None:
        reply["scale"] = scale
    if out_dir is not None:
        if not filename:
            stem = (getattr(state, "doc_name", None) or "untitled").strip() or "untitled"
            suffix = f"@{scale:g}x" if (ext == "png" and scale and scale != 1) else ""
            filename = f"{stem}{suffix}.{ext}"
        target = Path(str(out_dir)) / filename
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            return {"error": f"could not write {filename}: {exc}"}
        reply.update({"saved": True, "path": str(target)})
    else:
        reply.update({"saved": False, "data": base64.b64encode(data).decode("ascii")})
    return reply


@verb("export_image",
      "Export a node (by id) or the whole page as a PNG at 1x or 2x "
      "(scale: 1-8, background: optional colour or 'transparent'). Draws "
      "through the live Qt painter; without one attached the verb refuses by "
      "name rather than inventing pixels.",
      False)
def _export_image(state, args):
    exporter = getattr(state, "export_png", None)
    if exporter is None:
        return {"error": "export_image needs the native Qt painter attached; the "
                         "headless engine has no pixels to write"}
    node, err = _export_target(state, args, name="export_image")
    if err:
        return err
    scale = _number(args, "scale", minimum=1, maximum=8, name="export_image")
    scale = 1.0 if scale is None else float(scale)
    background = _string(args, "background", name="export_image")
    data = exporter(node, scale=scale, background=background)
    if not data:
        return {"error": "there is nothing visible to export in that selection"}
    return _deliver(state, data, ext="png", args=args, name="export_image", scale=scale)


@verb("export_svg",
      "Export a node (by id) or the whole page as a standalone SVG (vector; "
      "shapes stay paths, text stays text). background is optional. Draws "
      "through the live Qt painter; without one attached the verb refuses by "
      "name.",
      False)
def _export_svg(state, args):
    exporter = getattr(state, "export_svg", None)
    if exporter is None:
        return {"error": "export_svg needs the native Qt painter attached; the "
                         "headless engine has no painter to draw vectors with"}
    node, err = _export_target(state, args, name="export_svg")
    if err:
        return err
    background = _string(args, "background", name="export_svg")
    text = exporter(node, background=background)
    if not text or not text.strip():
        return {"error": "there is nothing visible to export in that selection"}
    return _deliver(state, text.encode("utf-8"), ext="svg", args=args, name="export_svg")


# -- viewport --------------------------------------------------------------- #

@verb("viewport_zoom_to_fit", "Zoom the viewport to fit specified nodes.", True)
def _viewport_zoom_to_fit(state, args):
    ids = args.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ToolError("Invalid arguments for viewport_zoom_to_fit: 'ids' must be a non-empty list")
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for nid in ids:
        node = _node(state, nid)
        if node is None:
            continue
        ax, ay, w, h = node.absolute_bounds()
        min_x, min_y = min(min_x, ax), min(min_y, ay)
        max_x, max_y = max(max_x, ax + w), max(max_y, ay + h)
    if min_x == math.inf:
        return {"error": "No valid nodes found"}
    center = {"x": (min_x + max_x) / 2, "y": (min_y + max_y) / 2}
    state.viewport.zoom_to_fit((min_x, min_y, max_x - min_x, max_y - min_y))
    return {"center": center,
            "bounds": {"x": min_x, "y": min_y, "width": max_x - min_x, "height": max_y - min_y}}


# -- describe --------------------------------------------------------------- #

@verb("describe",
      "Semantic description of one node (by id) or several (by ids): role, size, "
      "visual style and a summary of its children.",
      False)
def _describe(state, args):
    node_id = _string(args, "id", name="describe")
    ids = args.get("ids")
    depth = _number(args, "depth", name="describe")
    depth = None if depth is None else int(depth)

    if node_id:
        node = _node(state, node_id)
        if node is None:
            return {"id": node_id, "error": f'Node "{node_id}" not found'}
        return _describe_one(node, depth)

    if isinstance(ids, list) and ids:
        described = []
        for nid in ids:
            node = _node(state, nid)
            if node is None:
                described.append({"id": nid, "error": f'Node "{nid}" not found'})
            else:
                described.append(_describe_one(node, depth))
        return {"descriptions": described}

    return {"error": "describe needs an 'id' or an 'ids' list"}


# -- calc ------------------------------------------------------------------- #

_CALC_FUNCS = {
    "floor": math.floor, "ceil": math.ceil, "round": round, "abs": abs,
    "min": min, "max": max, "sqrt": math.sqrt, "pow": pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "exp": math.exp,
}
_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b,
}


def _eval_ast(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval_ast(node.left), _eval_ast(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval_ast(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _CALC_FUNCS:
        return _CALC_FUNCS[node.func.id](*[_eval_ast(a) for a in node.args])
    raise ValueError(f"unsupported expression near {ast.dump(node)[:40]}")


def _eval_expr(expr: str) -> dict:
    try:
        result = _eval_ast(ast.parse(expr, mode="eval"))
    except Exception as exc:  # noqa: BLE001 -- the calculator reports its own words
        return {"expr": expr, "error": str(exc)}
    if not math.isfinite(result):
        return {"expr": expr, "error": f"Produced {result}"}
    return {"expr": expr, "result": int(result) if float(result).is_integer() else result}


@verb("calc",
      "Arithmetic calculator. Pass one expression or a JSON array of expressions. "
      "Supports + - * / % ** ( ) and floor ceil round abs min max sqrt pow sin cos tan log exp.",
      False)
def _calc(state, args):
    expr = _string(args, "expr", required=True, name="calc")
    try:
        parsed = json.loads(expr)
        exprs = parsed if isinstance(parsed, list) else [expr]
    except (json.JSONDecodeError, TypeError):
        exprs = [expr]
    if len(exprs) == 1:
        return _eval_expr(str(exprs[0]))
    return {"results": [_eval_expr(str(e)) for e in exprs]}


# -- eval: honest Milestone 1 limit ----------------------------------------- #

@verb("eval",
      "Execute JavaScript with full engine API access. Not available in the "
      "native engine yet.",
      True)
def _eval(state, args):
    return {"error": "eval needs the JavaScript sandbox, which the native Milestone 1 "
                     "engine does not ship yet"}


# --------------------------------------------------------------------------- #
# The seam the router uses
# --------------------------------------------------------------------------- #

def catalog() -> list[dict]:
    """``[{name, description, mutates}]`` in registration order -- what the
    ``tools`` verb counts and names, read straight off what is registered."""
    return [{"name": v.name, "description": v.description, "mutates": v.mutates}
            for v in REGISTRY.values()]


def has(name: str) -> bool:
    return name in REGISTRY


def run(state: StudioState, name: str, args: Optional[dict] = None) -> dict:
    """Answer one engine verb. Returns ``{ok: true, result}`` or
    ``{ok: false, error}`` -- the exact shape ``window.run_tool`` hands back and
    ``tool.py._report`` reads. A verb that returns an ``error`` key is a soft
    failure (a node that was not there); a raised :class:`ToolError` is a hard
    one (arguments it could not read), and both become ``ok: false``."""
    entry = REGISTRY.get(name)
    if entry is None:
        return {"ok": False, "error": f"Saturn Studio has no {name} tool"}
    payload = args or {}
    try:
        result = entry.handler(state, payload)
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- a tool call answers, never raises
        return {"ok": False, "error": str(exc)}
    # A verb that hands back a dict carrying an ``error`` key is a soft failure
    # (a node that was not there), and the web engine throws on exactly that in
    # ``atomic-tool.ts``. ``tool.py._report`` reads a top-level ``error``, so lift
    # it out of the result into the envelope -- the reply the router sees matches.
    if isinstance(result, dict) and "error" in result:
        return {"ok": False, "error": str(result["error"])}
    return {"ok": True, "result": result}
