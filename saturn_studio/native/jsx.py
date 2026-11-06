"""Saturn Studio -- the JSX reader for the native engine.

Saturn's brain answers a brief with one JSX element (see ``tool.py``); this
module turns that string into a tree of :class:`scene.Node` objects. It is a
deliberately small reader: the handful of element names and props the authoring
reference teaches, nothing more. An attribute it does not recognise is skipped,
not fatal -- a model that adds one flourish the reference never promised should
still get its drawing on the canvas.

Milestone 1 has no auto-layout, and this file is where that honesty lives. It
never pretends to run a flex pass. A child with explicit ``x``/``y`` is placed
where it says; a child without them is *stacked* in a column at a fixed gutter,
and its name is added to ``warnings`` so the ``render`` verb can tell the user
the layer was arranged in a column rather than designed with layout. The flex
engine arrives in Milestone 2, in the scene model -- not smuggled in here.

No painter import: this is model construction, so it stays Qt-free like the
scene it builds.
"""

from __future__ import annotations

import re
from typing import Optional

from saturn_studio.native import scene

#: Elements Saturn's authoring reference teaches, mapped to a scene kind.
ELEMENT_KIND = {
    "Frame": scene.FRAME,
    "Rectangle": scene.RECTANGLE,
    "Rect": scene.RECTANGLE,
    "Ellipse": scene.ELLIPSE,
    "Circle": scene.ELLIPSE,
    "Line": scene.LINE,
    "Text": scene.TEXT,
    "Icon": scene.FRAME,  # no glyph runs yet; a labelled box is the honest stand-in
    "Group": scene.FRAME,
}

#: How wide a stacked row of text is guessed before a painter can measure it.
#: Milestone 2 replaces this with real shaping; until then it is a plain guess.
_GUTTER = 16.0


class JsxError(ValueError):
    """The markup could not be read at all -- no root element to build."""


# --------------------------------------------------------------------------- #
# Tokenizing: split the single JSX string into a nested (name, attrs, children)
# tuple without a heavyweight parser. Tolerant of braces inside attributes,
# self-closing tags, and text content on Text/Icon nodes.
# --------------------------------------------------------------------------- #

_ATTR = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|\{([^}]*)\})""")
_TAG = re.compile(r"<\s*(/?)\s*([A-Za-z][\w]*)\s*([^>]*?)\s*(/?)\s*>", re.DOTALL)


def _attr_value(raw: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, quoted, braced in raw:
        out[name] = quoted if quoted != "" else (braced if braced != "" else quoted or braced)
    return out


def _parse_number(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    cleaned = text.strip()
    if cleaned.endswith("px"):          # rstrip("px") would eat '640'->'64'->'6'
        cleaned = cleaned[:-2].strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _split_tags(markup: str):
    """Yield events in document order.

    ``('open'|'close'|'self', name, attrs)`` for tags and ``('text', raw)``
    for any plain text sitting between two tags -- text belongs to whichever
    element is open at that point, never to the tag that follows it.
    """
    pos = 0
    for match in _TAG.finditer(markup):
        between = markup[pos:match.start()]
        if between:
            yield "text", between
        pos = match.end()
        closing, name, attrs_raw, self_closed = match.groups()
        # findall returns ('', '640') for a braced value and ('Live check', '')
        # for a quoted one; an empty string means "this arm did not match", so
        # the braced arm wins whenever the quoted arm came back empty.
        attrs = {k: (q if q != "" else b) for k, q, b in _ATTR.findall(attrs_raw or "")}
        if closing:
            yield "close", name, attrs
        elif self_closed:
            yield "self", name, attrs
        else:
            yield "open", name, attrs


# --------------------------------------------------------------------------- #
# Building: walk the tags, construct Nodes, apply the props we know.
# --------------------------------------------------------------------------- #

class _Built:
    __slots__ = ("node", "sizing", "flex", "gap", "pad", "explicit_xy", "inner",
                 "child_built")

    def __init__(self, node: scene.Node, sizing, flex: str, gap: float,
                 pad: tuple[float, float, float, float], explicit_xy: bool):
        self.node = node
        self.sizing = sizing            # (w_raw, h_raw) like '640'/'fill'/'hug'/None
        self.flex = flex                # 'row' | 'col' (recorded, not honoured in M1)
        self.gap = gap
        self.pad = pad                  # (top, right, bottom, left)
        self.explicit_xy = explicit_xy  # the author gave x/y
        self.inner: Optional[tuple[float, float]] = None
        self.child_built: list["_Built"] = []  # placement walks this, not the Node tree


def _read_element(name: str, attrs: dict[str, str]) -> _Built:
    kind = ELEMENT_KIND.get(name)
    if kind is None:
        kind = scene.FRAME
    node = scene.Node(type=kind, name=attrs.get("name") or name)

    opacity = _parse_number(attrs.get("opacity"))
    if opacity is not None:
        node.opacity = max(0.0, min(1.0, opacity))

    rounded = _parse_number(attrs.get("rounded")) or _parse_number(attrs.get("radius"))
    if rounded:
        node.corner_radius = rounded

    # Fill / background. ``bg`` and ``fill`` both paint a shape; on text, color paints.
    color_spec = attrs.get("bg") or attrs.get("fill")
    if kind == scene.TEXT:
        color_spec = attrs.get("color") or attrs.get("fill") or attrs.get("bg")
    if color_spec:
        node.fills = [scene.Fill.solid(color_spec)]

    stroke_spec = attrs.get("stroke") or attrs.get("border")
    if stroke_spec:
        weight = _parse_number(attrs.get("strokeWidth") or attrs.get("strokeWidth")) or 1.0
        node.strokes = [scene.Stroke(color=scene.Color.parse(stroke_spec), weight=weight)]

    # Text run properties.
    if kind == scene.TEXT:
        # chars fill in later, from the text events between the tags
        run = scene.Text(chars="")
        size = _parse_number(attrs.get("size") or attrs.get("fontSize"))
        if size:
            run.size = size
        weight = _parse_number(attrs.get("weight") or attrs.get("fontWeight"))
        if weight:
            run.weight = int(weight)
        if attrs.get("font") or attrs.get("family"):
            run.family = attrs.get("font") or attrs.get("family")
        if attrs.get("textAlign") or attrs.get("align"):
            run.align = (attrs.get("textAlign") or attrs.get("align")).lower()
        if node.fills:
            run.color = node.fills[0].color
        node.text = run

    pad = _padding(attrs)
    gap = _parse_number(attrs.get("gap")) or 0.0
    flex = (attrs.get("flex") or "").lower()
    if flex in ("column", "col"):
        flex = "col"
    elif flex in ("row", "horizontal"):
        flex = "row"

    # Auto layout lands on the node in the model's own vocabulary; the flex
    # pass in ``scene`` reads it back. ``none`` leaves Milestone 1 stacking.
    lay = node.layout
    lay.mode = {"col": scene.FLEX_COL, "row": scene.FLEX_ROW}.get(flex, scene.FLEX_NONE)
    lay.gap = gap
    lay.row_gap = _parse_number(attrs.get("rowGap")) or gap
    (lay.pad_top, lay.pad_right, lay.pad_bottom, lay.pad_left) = pad
    lay.primary_align = _main_align(attrs.get("justify") or attrs.get("justifyContent"))
    lay.counter_align = _cross_align(attrs.get("items") or attrs.get("alignItems"))
    lay.wrap = (attrs.get("wrap") or "").lower() in ("1", "true", "wrap")

    # Sizing tokens decide FIXED/HUG/FILL per axis; a number is a FIXED box.
    _apply_sizing(node, attrs.get("w") or attrs.get("width"), "horizontal")
    _apply_sizing(node, attrs.get("h") or attrs.get("height"), "vertical")
    node.grow = _parse_number(attrs.get("grow") or attrs.get("flexGrow")) or 0.0
    align_self = (attrs.get("alignSelf") or attrs.get("selfAlign") or "").lower()
    if align_self:
        node.align_self = _cross_align(align_self)
    node.absolute = (attrs.get("pos") or attrs.get("layoutPositioning") or "").lower() == "absolute"

    explicit_xy = any(k in attrs for k in ("x", "y"))
    for axis, key in (("x", "x"), ("y", "y")):
        value = _parse_number(attrs.get(key))
        if value is not None:
            setattr(node, axis, value)
    node._has_explicit_origin = explicit_xy

    return _Built(node, (attrs.get("w") or attrs.get("width"),
                         attrs.get("h") or attrs.get("height")),
                  flex, gap, pad, explicit_xy)


def _apply_sizing(node: scene.Node, token, axis: str) -> None:
    """One of FIXED / HUG / FILL from a width or height token, plus the literal
    size when it is a plain number. Absent tokens keep the node's default box;
    Milestone 1's ``fill`` behaviour (take the parent) is now the FILL mode the
    flex pass resolves against real leftover space, not a guessed column."""
    if token is None:
        return
    text = str(token).strip().strip("\"'")
    if text == "fill":
        mode = scene.SIZING_FILL
    elif text == "hug":
        mode = scene.SIZING_HUG
    else:
        number = _parse_number(text)
        if number is None:
            return
        mode = scene.SIZING_FIXED
        setattr(node, "width" if axis == "horizontal" else "height", number)
    setattr(node, "sizing_horizontal" if axis == "horizontal" else "sizing_vertical", mode)


_MAIN_ALIGNS = {"start": scene.ALIGN_MIN, "end": scene.ALIGN_MAX,
                "center": scene.ALIGN_CENTER, "between": scene.ALIGN_SPACE,
                "space-between": scene.ALIGN_SPACE}


def _main_align(token) -> str:
    if not token:
        return scene.ALIGN_MIN
    return _MAIN_ALIGNS.get(str(token).strip().lower(), scene.ALIGN_MIN)


_CROSS_ALIGNS = {"start": scene.ALIGN_MIN, "end": scene.ALIGN_MAX,
                 "center": scene.ALIGN_CENTER, "stretch": scene.ALIGN_STRETCH,
                 "baseline": scene.ALIGN_MIN}


def _cross_align(token) -> str:
    if not token:
        return scene.ALIGN_MIN
    return _CROSS_ALIGNS.get(str(token).strip().lower(), scene.ALIGN_MIN)


def _padding(attrs: dict[str, str]) -> tuple[float, float, float, float]:
    p = _parse_number(attrs.get("p")) or _parse_number(attrs.get("padding")) or 0.0
    px = _parse_number(attrs.get("px"))
    py = _parse_number(attrs.get("py"))
    left = right = p if px is None else px
    top = bottom = p if py is None else py
    return (top, right, bottom, left)


def build(markup: str, measure=None) -> tuple[scene.Node, list[str]]:
    """Parse one JSX element into a node tree, laid out and sized.

    Returns ``(root, warnings)``. ``warnings`` names every child that had to be
    stacked because it gave no coordinates and its container declared no
    ``flex`` -- the honest signal that the layer was arranged, not designed. A
    container that declares ``flex`` runs the real pass in :meth:`scene.Node.run_layout`
    and produces no such warning. ``measure`` is an optional painter text metric
    (``node -> (width, height)``); when absent the model measures deterministically.
    Raises :class:`JsxError` if there is no element.
    """
    markup = markup.strip()
    if "<" not in markup:
        raise JsxError("no JSX element to draw")

    stack: list[_Built] = []
    root_built: Optional[_Built] = None
    warnings: list[str] = []          # inline skips land here as they happen
    fragments: dict[int, list[str]] = {}   # id(_Built) -> raw text fragments

    def owner() -> Optional[_Built]:
        return stack[-1] if stack else root_built

    def finish(built: _Built) -> None:
        """Join the text an element accumulated while it was open."""
        if built.node.type != scene.TEXT or built.node.text is None:
            return
        joined = " ".join(" ".join(fragments.pop(id(built), [])).split())
        if joined:
            built.node.text.chars = joined

    for parsed in _split_tags(markup):
        event = parsed[0]
        if event == "text":
            current = owner()
            if current is not None:
                if current.node.type == scene.TEXT:
                    fragments.setdefault(id(current), []).append(parsed[1])
                continue
            continue

        name, attrs = parsed[1], parsed[2]
        if event == "close":
            if stack:
                finish(stack.pop())
            continue

        built = _read_element(name, attrs)
        if root_built is None:
            root_built = built
        else:
            parent = owner()
            if parent is not None and parent.node.type == scene.TEXT:
                # A text run cannot hold child nodes (M1 has no rich-text
                # inline layout). The element is not drawn -- its own text,
                # if any, still lands in the run through the text events --
                # and we say so. Never draw a lying subtree.
                warnings.append(
                    f"inline <{name}> inside text is not drawn yet (Milestone 1)"
                )
                continue
            parent.node.add_child(built.node)
            parent.child_built.append(built)
        if event == "open":
            stack.append(built)
        # 'self' elements are complete on arrival; nothing is pushed.

    if root_built is None:
        raise JsxError("no JSX element to draw")
    finish(root_built)          # covers a root that never opened a close tag
    for built in stack:         # unbalanced markup: close what is still open
        finish(built)

    for note in root_built.node.run_layout(measure):
        if note not in warnings:
            warnings.append(note)
    return root_built.node, warnings

