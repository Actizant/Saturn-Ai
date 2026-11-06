"""Saturn Studio -- the scene model. The authority for what is on the canvas.

This module knows nothing about drawing. It holds nodes, their geometry, their
paint and the tree that contains them, and it answers questions about them:
what is under this point, what are a node's bounds on the page, which layers
match a name, how do I serialize myself. A painter backend -- Qt today, maybe
Skia-GPU tomorrow -- projects this model; it never owns it. Keeping that true
is the whole point of the swappable painter.

Milestone 1 node kinds: PAGE, FRAME, RECTANGLE, ELLIPSE, LINE, TEXT. Geometry is
stored explicitly; auto-layout lives here too, in the model -- :meth:`Node.run_layout`
runs the flex pass, so a painter never owns placement and the swap stays clean.

Clean room: written from the verb contract and these rules, not ported from any
existing engine's source.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional


# --------------------------------------------------------------------------- #
# Node kinds
# --------------------------------------------------------------------------- #

PAGE = "PAGE"
FRAME = "FRAME"
RECTANGLE = "RECTANGLE"
ELLIPSE = "ELLIPSE"
LINE = "LINE"
TEXT = "TEXT"

#: Kinds that hold children. A page is a container; a frame is a container;
#: shapes and text are leaves. Auto-layout in M2 will only ever apply to these.
CONTAINER_KINDS = frozenset({PAGE, FRAME})
LEAF_KINDS = frozenset({RECTANGLE, ELLIPSE, LINE, TEXT})
ALL_KINDS = CONTAINER_KINDS | LEAF_KINDS


# --------------------------------------------------------------------------- #
# Auto-layout vocabulary (Milestone 2). The engine speaks the Figma words so a
# read tool reports the same facts the web engine reported; the flex pass that
# fills them lives in :meth:`Node.run_layout`, below, in the model -- never in a
# painter. Clean room: this is the public flexbox algorithm, written from the
# contract, not ported from any layout library's source.
# --------------------------------------------------------------------------- #

FLEX_NONE = "NONE"          # children keep their own x/y (or get stacked)
FLEX_ROW = "HORIZONTAL"     # main axis runs left-to-right
FLEX_COL = "VERTICAL"       # main axis runs top-to-bottom
FLEX_MODES = (FLEX_NONE, FLEX_ROW, FLEX_COL)

SIZING_FIXED = "FIXED"      # keep the declared width/height
SIZING_HUG = "HUG"          # shrink-wrap the content
SIZING_FILL = "FILL"        # take the parent's remaining space on this axis
SIZINGS = (SIZING_FIXED, SIZING_HUG, SIZING_FILL)

ALIGN_MIN = "MIN"
ALIGN_CENTER = "CENTER"
ALIGN_MAX = "MAX"
ALIGN_SPACE = "SPACE_BETWEEN"
ALIGN_STRETCH = "STRETCH"   # cross-axis only: fill the parent's cross extent

#: A line of text without an explicit line-height gets this multiple of its
#: point size. The painter overrides the whole metric with real shaping; this
#: keeps the Qt-free model deterministic when no painter is in the room.
TEXT_LINE_RATIO = 1.2


def new_id() -> str:
    """A stable, opaque id. Parity compares that ids exist and are unique, not
    that two hosts mint the same value, so randomness is correct here."""
    return uuid.uuid4().hex[:12]


#: Text alignment in the model is lowercase and terse; an engine tool reports
#: it in the Figma vocabulary. These map the stored value to the reported one.
_H_ALIGN = {"left": "LEFT", "center": "CENTER", "right": "RIGHT", "justify": "JUSTIFIED"}
_V_ALIGN = {"top": "TOP", "center": "CENTER", "bottom": "BOTTOM"}


def _auto_resize_mode(text: "Text") -> str:
    """The resize enum an engine tool reports, read off the two hug flags."""
    if text.auto_width and text.auto_height:
        return "WIDTH_AND_HEIGHT"
    if text.auto_height:
        return "HEIGHT"
    return "NONE"


# --------------------------------------------------------------------------- #
# Paint
# --------------------------------------------------------------------------- #

@dataclass
class Color:
    r: float = 0.0
    g: float = 0.0
    b: float = 0.0
    a: float = 1.0

    def to_tuple(self) -> tuple[float, float, float, float]:
        return (self.r, self.g, self.b, self.a)

    def to_dict(self) -> dict:
        return {"r": _round(self.r), "g": _round(self.g), "b": _round(self.b),
                "a": _round(self.a)}

    def to_hex(self) -> str:
        """``#rrggbb``, the way describe names a colour back to the user. The
        alpha channel is dropped unless it is partly transparent."""
        rr, gg, bb = (round(c * 255) for c in (self.r, self.g, self.b))
        if self.a >= 0.999:
            return f"#{rr:02x}{gg:02x}{bb:02x}"
        aa = round(self.a * 255)
        return f"#{rr:02x}{gg:02x}{bb:02x}{aa:02x}"

    @classmethod
    def parse(cls, value: Any) -> "Color":
        """Accept a Color, a ``#rgb``/``#rrggbb``/``#rrggbbaa`` string, a
        0-255 or 0-1 tuple, or a component dict. The engine's tools and Saturn's
        JSX both hand us colours in several of these shapes."""
        if isinstance(value, Color):
            return value
        if isinstance(value, str):
            return cls._from_hex(value)
        if isinstance(value, dict):
            return cls(
                _to_unit(value.get("r", 0)), _to_unit(value.get("g", 0)),
                _to_unit(value.get("b", 0)), _to_unit(value.get("a", 1)),
            )
        if isinstance(value, (list, tuple)):
            parts = list(value)
            while len(parts) < 4:
                parts.append(1)
            return cls(*(_to_unit(p) for p in parts[:4]))
        return cls(0, 0, 0, 1)

    @classmethod
    def _from_hex(cls, text: str) -> "Color":
        text = text.strip().lstrip("#")
        if len(text) in (3, 4):
            text = "".join(ch * 2 for ch in text)
        if len(text) == 6:
            text += "ff"
        if len(text) != 8 or any(ch not in "0123456789abcdefABCDEF" for ch in text):
            return cls(0, 0, 0, 1)
        return cls(
            int(text[0:2], 16) / 255, int(text[2:4], 16) / 255,
            int(text[4:6], 16) / 255, int(text[6:8], 16) / 255,
        )


@dataclass
class GradientStop:
    color: Color
    position: float = 0.0  # 0..1 along the gradient axis


@dataclass
class Fill:
    """A paint layer. Solid carries one colour; a gradient carries stops and a
    unit-square axis (``x1,y1`` -> ``x2,y2``) the renderer maps onto bounds."""

    kind: str = "solid"  # solid | linear | radial
    color: Color = field(default_factory=Color)
    stops: list[GradientStop] = field(default_factory=list)
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 1.0
    y2: float = 0.0
    visible: bool = True

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind, "visible": self.visible}
        if self.kind == "solid":
            out["color"] = self.color.to_dict()
        else:
            out["stops"] = [
                {"position": _round(s.position), "color": s.color.to_dict()}
                for s in self.stops
            ]
            out.update(x1=_round(self.x1), y1=_round(self.y1),
                       x2=_round(self.x2), y2=_round(self.y2))
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Fill":
        kind = data.get("kind", "solid")
        fill = cls(kind=kind, visible=data.get("visible", True))
        if kind == "solid":
            fill.color = Color.parse(data.get("color", "#000000"))
        else:
            fill.stops = [
                GradientStop(Color.parse(s.get("color")), float(s.get("position", 0)))
                for s in data.get("stops", [])
            ]
            fill.x1, fill.y1 = data.get("x1", 0), data.get("y1", 0)
            fill.x2, fill.y2 = data.get("x2", 1), data.get("y2", 0)
        return fill

    @classmethod
    def solid(cls, color: Any) -> "Fill":
        return cls(kind="solid", color=Color.parse(color))

    def to_json(self) -> dict:
        """The paint as an engine tool reports it: a SOLID entry with one
        colour, or a GRADIENT entry with its stops and the axis the renderer
        maps onto the node's bounds."""
        if self.kind == "solid":
            return {"type": "SOLID", "color": self.color.to_dict(),
                    "opacity": 1, "visible": self.visible}
        kind = "GRADIENT_LINEAR" if self.kind == "linear" else "GRADIENT_RADIAL"
        stops = [{"position": _round(s.position), "color": s.color.to_dict()}
                 for s in self.stops]
        start = stops[0]["color"] if stops else Color().to_dict()
        return {"type": kind, "color": start, "opacity": 1, "visible": self.visible,
                "gradientStops": stops,
                "gradientAxis": [_round(self.x1), _round(self.y1),
                                 _round(self.x2), _round(self.y2)]}


@dataclass
class Stroke:
    color: Color = field(default_factory=lambda: Color.parse("#000000"))
    weight: float = 1.0
    align: str = "inside"  # inside | center | outside
    visible: bool = True

    def to_dict(self) -> dict:
        return {"color": self.color.to_dict(), "weight": _round(self.weight),
                "align": self.align, "visible": self.visible}

    @classmethod
    def from_dict(cls, data: dict) -> "Stroke":
        return cls(
            color=Color.parse(data.get("color", "#000000")),
            weight=float(data.get("weight", 1)),
            align=data.get("align", "inside"),
            visible=data.get("visible", True),
        )

    def to_json(self) -> dict:
        return {"type": "SOLID", "color": self.color.to_dict(),
                "weight": _round(self.weight), "opacity": 1,
                "visible": self.visible, "align": self.align.upper()}


@dataclass
class Text:
    """Text content and its run properties. ``size`` is the point size; the box
    a text node occupies is the node's width/height, which the painter backend
    measures with its real shaper (Qt uses HarfBuzz via QTextLayout)."""

    chars: str = ""
    size: float = 16.0
    family: str = "Inter"
    weight: int = 400
    italic: bool = False
    color: Color = field(default_factory=lambda: Color.parse("#000000"))
    align: str = "left"  # left | center | right | justify
    valign: str = "top"  # top | center | bottom
    line_height: Optional[float] = None
    letter_spacing: Optional[float] = None
    auto_width: bool = True    # hug content horizontally
    auto_height: bool = True   # hug content vertically

    def to_dict(self) -> dict:
        return {
            "chars": self.chars, "size": _round(self.size), "family": self.family,
            "weight": self.weight, "italic": self.italic,
            "color": self.color.to_dict(), "align": self.align, "valign": self.valign,
            "lineHeight": self.line_height, "letterSpacing": self.letter_spacing,
            "autoWidth": self.auto_width, "autoHeight": self.auto_height,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Text":
        return cls(
            chars=data.get("chars", ""),
            size=float(data.get("size", 16)),
            family=data.get("family", "Inter"),
            weight=int(data.get("weight", 400)),
            italic=bool(data.get("italic", False)),
            color=Color.parse(data.get("color", "#000000")),
            align=data.get("align", "left"),
            valign=data.get("valign", "top"),
            line_height=data.get("lineHeight"),
            letter_spacing=data.get("letterSpacing"),
            auto_width=data.get("autoWidth", True),
            auto_height=data.get("autoHeight", True),
        )


# --------------------------------------------------------------------------- #
# Node
# --------------------------------------------------------------------------- #

@dataclass
class Layout:
    """A container's auto-layout settings, in the Figma vocabulary a read tool
    reports. ``mode`` is the only gate: while it is ``NONE`` the node lays its
    children out by their own ``x``/``y`` (Milestone 1 stacking), and nothing
    else in here is honoured."""

    mode: str = FLEX_NONE
    gap: float = 0.0                 # main-axis spacing between children
    row_gap: float = 0.0             # spacing between wrapped lines (rare)
    pad_top: float = 0.0
    pad_right: float = 0.0
    pad_bottom: float = 0.0
    pad_left: float = 0.0
    primary_align: str = ALIGN_MIN     # along the main axis
    counter_align: str = ALIGN_MIN     # across it
    wrap: bool = False

    @property
    def active(self) -> bool:
        return self.mode in (FLEX_ROW, FLEX_COL)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "gap": _round(self.gap), "rowGap": _round(self.row_gap),
            "paddingTop": _round(self.pad_top), "paddingRight": _round(self.pad_right),
            "paddingBottom": _round(self.pad_bottom), "paddingLeft": _round(self.pad_left),
            "primaryAlign": self.primary_align, "counterAlign": self.counter_align,
            "wrap": self.wrap,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Layout":
        return cls(
            mode=data.get("mode", FLEX_NONE),
            gap=float(data.get("gap", 0)), row_gap=float(data.get("rowGap", 0)),
            pad_top=float(data.get("paddingTop", 0)),
            pad_right=float(data.get("paddingRight", 0)),
            pad_bottom=float(data.get("paddingBottom", 0)),
            pad_left=float(data.get("paddingLeft", 0)),
            primary_align=data.get("primaryAlign", ALIGN_MIN),
            counter_align=data.get("counterAlign", ALIGN_MIN),
            wrap=bool(data.get("wrap", False)),
        )


@dataclass
class Node:
    """One thing on the canvas. Geometry is the node's own box in its parent's
    coordinate space; ``absolute_bounds`` folds the parent chain in."""

    type: str = RECTANGLE
    name: str = "Layer"
    id: str = field(default_factory=new_id)
    x: float = 0.0
    y: float = 0.0
    width: float = 100.0
    height: float = 100.0
    rotation: float = 0.0
    opacity: float = 1.0
    visible: bool = True
    corner_radius: float = 0.0
    # Individual corners; None means "use corner_radius". set_radius fills
    # them only when the user asks for corners that differ from each other.
    top_left_radius: Optional[float] = None
    top_right_radius: Optional[float] = None
    bottom_right_radius: Optional[float] = None
    bottom_left_radius: Optional[float] = None
    fills: list[Fill] = field(default_factory=list)
    strokes: list[Stroke] = field(default_factory=list)
    text: Optional[Text] = None
    children: list["Node"] = field(default_factory=list)
    parent: Optional["Node"] = None
    # -- auto layout (Milestone 2) ---------------------------------------- #
    layout: Layout = field(default_factory=Layout)     # this box as a container
    sizing_horizontal: str = SIZING_FIXED               # how I size in my parent
    sizing_vertical: str = SIZING_FIXED
    grow: float = 0.0                                    # main-axis fill weight
    align_self: Optional[str] = None                    # override parent counter
    absolute: bool = False                              # out of the flow (keep x/y)
    #: Authoring hint set by ``jsx``: the author gave this box its own x/y, so a
    #: non-flex parent must not restack it. Transient -- never serialized.
    _has_explicit_origin: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.type == TEXT and self.text is None:
            self.text = Text()

    # -- tree ------------------------------------------------------------- #

    @property
    def is_container(self) -> bool:
        return self.type in CONTAINER_KINDS

    def index_in_parent(self) -> int:
        if self.parent is None:
            return -1
        return self.parent.children.index(self)

    def add_child(self, child: "Node", index: Optional[int] = None) -> "Node":
        if not self.is_container:
            raise TypeError(f"{self.type} cannot hold children")
        child.detach()
        child.parent = self
        if index is None or index >= len(self.children):
            self.children.append(child)
        else:
            self.children.insert(max(0, index), child)
        return child

    def detach(self) -> "Node":
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)
        self.parent = None
        return self

    def remove_child(self, child: "Node") -> None:
        if child in self.children:
            child.detach()

    def reparent(self, new_parent: "Node", index: Optional[int] = None) -> None:
        new_parent.add_child(self, index)

    def walk(self) -> Iterator["Node"]:
        """Depth-first, parents before children -- document order."""
        yield self
        for child in self.children:
            yield from child.walk()

    def descendants(self) -> list["Node"]:
        return [node for node in self.walk() if node is not self]

    def ancestors(self) -> list["Node"]:
        chain, node = [], self.parent
        while node is not None:
            chain.append(node)
            node = node.parent
        return chain

    def root(self) -> "Node":
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    # -- geometry --------------------------------------------------------- #

    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)

    def absolute_bounds(self) -> tuple[float, float, float, float]:
        ax = ay = 0.0
        node: Optional[Node] = self
        while node is not None:
            ax += node.x
            ay += node.y
            node = node.parent
        return (ax, ay, self.width, self.height)

    def center(self) -> tuple[float, float]:
        ax, ay, w, h = self.absolute_bounds()
        return (ax + w / 2, ay + h / 2)

    def contains_point(self, px: float, py: float) -> bool:
        if not self.visible:
            return False
        ax, ay, w, h = self.absolute_bounds()
        return ax <= px <= ax + w and ay <= py <= ay + h

    def hit_test(self, px: float, py: float) -> Optional["Node"]:
        """Topmost visible node at this absolute point. Children are hit before
        their parent and later siblings (higher z) before earlier ones."""
        if not self.visible:
            return None
        for child in reversed(self.children):
            found = child.hit_test(px, py)
            if found is not None:
                return found
        return self if self.contains_point(px, py) else None

    # -- auto layout (Milestone 2) ---------------------------------------- #

    def is_layout_container(self) -> bool:
        return self.is_container and self.layout.active

    def _main_cross(self) -> tuple[bool, float, float, float, float]:
        """(is_row, pad_main_start, pad_main_end, pad_cross_start, pad_cross_end)."""
        lay = self.layout
        if lay.mode == FLEX_ROW:
            return (True, lay.pad_left, lay.pad_right, lay.pad_top, lay.pad_bottom)
        return (False, lay.pad_top, lay.pad_bottom, lay.pad_left, lay.pad_right)

    def measure_box(self, measure: Optional[Callable[["Node"], tuple[float, float]]] = None
                    ) -> tuple[float, float]:
        """The shrink-wrap size this node would take if it hugged its content.
        A leaf asks the painter (or the deterministic default); a non-flex box
        keeps its declared size; a flex box sums its children's main axis and
        takes the widest cross axis, plus padding. Pure -- it mutates nothing."""
        measure = measure or _default_text_measure
        if self.type == TEXT:
            return measure(self)
        if not self.layout.active or not self.children:
            return (self.width, self.height)
        is_row, ms, me, cs, ce = self._main_cross()
        flow = [c for c in self.children if not c.absolute]
        if not flow:
            return (self.width, self.height)
        total_main = 0.0
        max_cross = 0.0
        for child in flow:
            cw, ch = child._base_size(measure)
            main = cw if is_row else ch
            cross = ch if is_row else cw
            total_main += main
            max_cross = max(max_cross, cross)
        total_main += self.layout.gap * (len(flow) - 1)
        if is_row:
            return (total_main + ms + me, max_cross + cs + ce)
        return (max_cross + cs + ce, total_main + ms + me)

    def _base_size(self, measure) -> tuple[float, float]:
        """Size along each axis for intrinsic calculations: FILL counts as its
        declared floor, HUG shrinks to content, FIXED keeps the declared box."""
        w, h = self.width, self.height
        if self.sizing_horizontal == SIZING_HUG:
            w = self.measure_box(measure)[0]
        if self.sizing_vertical == SIZING_HUG:
            h = self.measure_box(measure)[1]
        if self.type == TEXT:
            tw, th = measure(self)
            if self.sizing_horizontal != SIZING_FILL:
                w = tw
            if self.sizing_vertical != SIZING_FILL:
                h = th
        return (max(0.0, w), max(0.0, h))

    def run_layout(self, measure: Optional[Callable[["Node"], tuple[float, float]]] = None
                   ) -> list[str]:
        """Run the flex pass over this subtree and place every descendant.

        The node's own outer box is taken as already decided (the caller -- or
        ``jsx`` for a root -- set it from its sizing); this lays out children
        inside it, resolving ``FILL`` against the leftover main space, ``HUG``
        containers against their content, and cross-axis ``STRETCH``. Returns
        the honest placement notes: a container with no ``flex`` whose children
        gave no coordinates still stacks them, and says so."""
        notes: list[str] = []
        self._place_children(measure or _default_text_measure, notes)
        return notes

    def _place_children(self, measure, notes: list[str]) -> None:
        if not self.children:
            return
        if not self.layout.active:
            self._stack_children(measure, notes)
            return

        is_row, ms, me, cs, ce = self._main_cross()
        outer_main = self.width if is_row else self.height
        outer_cross = self.height if is_row else self.width
        avail_main = max(0.0, outer_main - ms - me)
        avail_cross = max(0.0, outer_cross - cs - ce)

        flow = [c for c in self.children if not c.absolute]
        if not flow:
            return
        gap = self.layout.gap

        # 1. base main/cross per child (FILL contributes 0 to the sum on main).
        mains: list[float] = []
        crosses: list[float] = []
        for child in flow:
            bw, bh = child._base_size(measure)
            mains.append(bw if is_row else bh)
            crosses.append(bh if is_row else bw)

        # 2. grow the FILL / grow>0 children into the leftover main space.
        content = sum(mains) + gap * (len(flow) - 1)
        free = avail_main - content
        weights = [child.grow if child.grow > 0 else (1.0 if _is_fill(child, is_row) else 0.0)
                   for child in flow]
        total_weight = sum(weights)
        if free > 0 and total_weight > 0:
            for i, child in enumerate(flow):
                if weights[i] > 0:
                    mains[i] += free * weights[i] / total_weight

        # 3. main-axis start offset and spacing from the primary alignment.
        used_main = sum(mains) + gap * (len(flow) - 1)
        slack = max(0.0, avail_main - used_main)
        align = self.layout.primary_align
        if align == ALIGN_CENTER:
            cursor = ms + slack / 2
            gap_px = gap
        elif align == ALIGN_MAX:
            cursor = ms + slack
            gap_px = gap
        elif align == ALIGN_SPACE and len(flow) > 1:
            cursor = ms
            gap_px = gap + slack / (len(flow) - 1)
        else:  # MIN, or SPACE_BETWEEN with a single child
            cursor = ms
            gap_px = gap

        # 4. walk the main axis: resolve each child's final box, set its cross
        #    position, then recurse so a nested flex box lays out in turn.
        for i, child in enumerate(flow):
            main = mains[i]
            cross = avail_cross if _cross_stretch(child, is_row, self.layout.counter_align) \
                else crosses[i]
            offset = _cross_offset(child.align_self or self.layout.counter_align,
                                   cs, avail_cross, cross)
            if is_row:
                child.width, child.height = main, cross
                child.x, child.y = cursor, offset
            else:
                child.width, child.height = cross, main
                child.x, child.y = offset, cursor
            child._place_children(measure, notes)
            cursor += main + gap_px

    def _stack_children(self, measure, notes: list[str]) -> None:
        """Milestone 1 arrangement, kept honest: a container that declares no
        ``flex`` and gives its children no coordinates stacks them in a column
        at the gutter and warns, rather than pretending it designed a layout.
        Children that did give ``x``/``y`` are left exactly where they said. A
        child that asked to ``fill`` still takes the parent's inner box, as it
        did before auto-layout existed -- this is arrangement, not a layout."""
        lay = self.layout
        avail_w = max(0.0, self.width - lay.pad_left - lay.pad_right)
        avail_h = max(0.0, self.height - lay.pad_top - lay.pad_bottom)
        cursor = lay.pad_top
        for child in self.children:
            if child.absolute:
                child._place_children(measure, notes)
                continue
            bw, bh = child._base_size(measure)
            child.width = avail_w if child.sizing_horizontal == SIZING_FILL else bw
            child.height = avail_h if child.sizing_vertical == SIZING_FILL else bh
            if not child._has_explicit_origin:
                child.x = lay.pad_left
                child.y = round(cursor)
                notes.append(
                    f"'{child.name}' was stacked in a column (no coordinates given)"
                )
                cursor += child.height + (lay.gap or _GUTTER)
            child._place_children(measure, notes)

    # -- queries ---------------------------------------------------------- #

    def find(self, name: Optional[str] = None, type: Optional[str] = None,
             limit: Optional[int] = None) -> list["Node"]:
        """Descendants (and self) matching a name substring and/or exact kind."""
        want = (type or "").upper() if type else None
        needle = name.lower() if name else None
        hits: list[Node] = []
        for node in self.walk():
            if want and node.type != want:
                continue
            if needle and needle not in node.name.lower():
                continue
            hits.append(node)
            if limit is not None and len(hits) >= limit:
                break
        return hits

    # -- serialization ---------------------------------------------------- #

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "id": self.id, "name": self.name, "type": self.type,
            "x": _round(self.x), "y": _round(self.y),
            "width": _round(self.width), "height": _round(self.height),
            "rotation": _round(self.rotation), "opacity": _round(self.opacity),
            "visible": self.visible,
        }
        if self.type in (FRAME, PAGE, RECTANGLE, ELLIPSE, LINE):
            out["cornerRadius"] = _round(self.corner_radius)
            if self.top_left_radius is not None:
                out["topLeftRadius"] = _round(self.top_left_radius)
            if self.top_right_radius is not None:
                out["topRightRadius"] = _round(self.top_right_radius)
            if self.bottom_right_radius is not None:
                out["bottomRightRadius"] = _round(self.bottom_right_radius)
            if self.bottom_left_radius is not None:
                out["bottomLeftRadius"] = _round(self.bottom_left_radius)
        if self.fills:
            out["fills"] = [f.to_dict() for f in self.fills]
        if self.strokes:
            out["strokes"] = [s.to_dict() for s in self.strokes]
        if self.type == TEXT and self.text is not None:
            out["text"] = self.text.to_dict()
        # Auto layout is stored whenever it is switched on, so a .saturn file
        # round-trips a real layout, not just the pixels it happened to land on.
        if self.layout.active or self.absolute or self.grow or self.align_self \
                or self.sizing_horizontal != SIZING_FIXED or self.sizing_vertical != SIZING_FIXED:
            out["layout"] = self.layout.to_dict()
            out["sizingHorizontal"] = self.sizing_horizontal
            out["sizingVertical"] = self.sizing_vertical
            if self.grow:
                out["grow"] = _round(self.grow)
            if self.align_self:
                out["alignSelf"] = self.align_self
            if self.absolute:
                out["absolutePositioned"] = True
        if self.children:
            out["children"] = [c.to_dict() for c in self.children]
        return out

    def to_json(self, max_depth: Optional[int] = None, _depth: int = 0) -> dict:
        """The node the way an engine tool reports it -- the shape ``get_node``
        and ``find_nodes`` return, and the shape Saturn quotes back. It is a
        read view, distinct from :meth:`to_dict`, which is the on-disk form.
        Only fields that hold something are present, exactly as the reference
        serialization omits defaults. A container that runs auto-layout reports
        it in the Figma vocabulary; one that does not reports no layout at all.
        """
        out: dict[str, Any] = {
            "id": self.id, "type": self.type, "name": self.name,
            "x": _round(self.x), "y": _round(self.y),
            "width": _round(self.width), "height": _round(self.height),
        }
        if self.layout.active:
            out["layoutMode"] = self.layout.mode
            out["itemSpacing"] = _round(self.layout.gap)
            out["counterAxisSpacing"] = _round(self.layout.row_gap)
            out["primaryAxisAlignItems"] = self.layout.primary_align
            out["counterAxisAlignItems"] = self.layout.counter_align
            out["paddingTop"] = _round(self.layout.pad_top)
            out["paddingRight"] = _round(self.layout.pad_right)
            out["paddingBottom"] = _round(self.layout.pad_bottom)
            out["paddingLeft"] = _round(self.layout.pad_left)
            out["layoutWrap"] = "WRAP" if self.layout.wrap else "NO_WRAP"
        if self.parent is not None and self.parent.layout.active:
            out["layoutSizingHorizontal"] = self.sizing_horizontal
            out["layoutSizingVertical"] = self.sizing_vertical
            if self.grow:
                out["layoutGrow"] = _round(self.grow)
            if self.absolute:
                out["layoutPositioning"] = "ABSOLUTE"
        if self.fills:
            out["fills"] = [f.to_json() for f in self.fills]
        if self.strokes:
            out["strokes"] = [s.to_json() for s in self.strokes]
        if self.opacity != 1:
            out["opacity"] = _round(self.opacity)
        if self.corner_radius > 0:
            out["cornerRadius"] = _round(self.corner_radius)
        if not self.visible:
            out["visible"] = False
        if self.type == TEXT and self.text is not None:
            t = self.text
            out["characters"] = t.chars
            out.update(
                fontFamily=t.family, fontSize=_round(t.size), fontWeight=t.weight,
                italic=t.italic,
                textAlignHorizontal=_H_ALIGN.get(t.align, "LEFT"),
                textAlignVertical=_V_ALIGN.get(t.valign, "TOP"),
                textAutoResize=_auto_resize_mode(t), textDirection="AUTO",
            )
            if t.line_height is not None:
                out["lineHeight"] = _round(t.line_height)
            if t.letter_spacing:
                out["letterSpacing"] = _round(t.letter_spacing)
        if self.children:
            if max_depth is not None and _depth >= max_depth:
                out["childCount"] = len(self.children)
            else:
                out["children"] = [c.to_json(max_depth, _depth + 1) for c in self.children]
        return out

    @classmethod
    def from_dict(cls, data: dict, parent: Optional["Node"] = None) -> "Node":
        node = cls(
            type=data.get("type", RECTANGLE),
            name=data.get("name", "Layer"),
            id=data.get("id") or new_id(),
            x=float(data.get("x", 0)), y=float(data.get("y", 0)),
            width=float(data.get("width", 100)), height=float(data.get("height", 100)),
            rotation=float(data.get("rotation", 0)), opacity=float(data.get("opacity", 1)),
            visible=bool(data.get("visible", True)),
            corner_radius=float(data.get("cornerRadius", 0)),
            top_left_radius=_optional_float(data.get("topLeftRadius")),
            top_right_radius=_optional_float(data.get("topRightRadius")),
            bottom_right_radius=_optional_float(data.get("bottomRightRadius")),
            bottom_left_radius=_optional_float(data.get("bottomLeftRadius")),
            fills=[Fill.from_dict(f) for f in data.get("fills", [])],
            strokes=[Stroke.from_dict(s) for s in data.get("strokes", [])],
            text=Text.from_dict(data["text"]) if "text" in data else None,
            layout=Layout.from_dict(data["layout"]) if "layout" in data else Layout(),
            sizing_horizontal=data.get("sizingHorizontal", SIZING_FIXED),
            sizing_vertical=data.get("sizingVertical", SIZING_FIXED),
            grow=float(data.get("grow", 0)),
            align_self=data.get("alignSelf"),
            absolute=bool(data.get("absolutePositioned", False)),
            parent=parent,
        )
        for child in data.get("children", []):
            node.add_child(Node.from_dict(child))
        return node

    def effective_corners(self) -> tuple[float, float, float, float]:
        """The four radii, top-left/clockwise, with per-corner values winning
        over the shared one. The painter and ``set_radius`` both read this, so
        a rounded box is rounded the same way everywhere it is drawn."""
        base = self.corner_radius
        return (
            self.top_left_radius if self.top_left_radius is not None else base,
            self.top_right_radius if self.top_right_radius is not None else base,
            self.bottom_right_radius if self.bottom_right_radius is not None else base,
            self.bottom_left_radius if self.bottom_left_radius is not None else base,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.type} {self.name!r} {self.width:.0f}x{self.height:.0f} @ {self.x:.0f},{self.y:.0f}>"


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #

DEFAULT_PAGE = "Page 1"


class Document:
    """The whole file: one or more pages, the current page, and a name. Pages
    are containers; their children are the top-level layers a user sees."""

    def __init__(self, name: str = "Untitled") -> None:
        self.name = name
        self.pages: list[Node] = []
        self.current_index: int = 0
        self.new_page(DEFAULT_PAGE)

    # -- pages ------------------------------------------------------------ #

    @property
    def current_page(self) -> Node:
        return self.pages[self.current_index]

    def new_page(self, name: Optional[str] = None) -> Node:
        page = Node(type=PAGE, name=name or f"Page {len(self.pages) + 1}")
        self.pages.append(page)
        self.current_index = len(self.pages) - 1
        return page

    def set_current_page(self, index: int) -> None:
        if 0 <= index < len(self.pages):
            self.current_index = index

    @property
    def layers(self) -> list[Node]:
        """Top-level nodes on the current page, back to front."""
        return list(self.current_page.children)

    def all_nodes(self) -> Iterator[Node]:
        for page in self.pages:
            yield from page.walk()

    def find_by_id(self, node_id: str) -> Optional[Node]:
        for node in self.all_nodes():
            if node.id == node_id:
                return node
        return None

    def add(self, node: Node, index: Optional[int] = None) -> Node:
        return self.current_page.add_child(node, index)

    def content_bounds(self) -> Optional[tuple[float, float, float, float]]:
        """Union of every top-level layer's absolute box, or None if empty."""
        boxes = [layer.absolute_bounds() for layer in self.layers if layer.visible]
        if not boxes:
            return None
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes)
        y1 = max(b[1] + b[3] for b in boxes)
        return (x0, y0, x1 - x0, y1 - y0)

    # -- serialization ---------------------------------------------------- #

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "current": self.current_index,
            "pages": [page.to_dict() for page in self.pages],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Document":
        doc = cls.__new__(cls)
        doc.name = data.get("name", "Untitled")
        doc.pages = [Node.from_dict(page) for page in data.get("pages", [])]
        if not doc.pages:
            doc.pages = [Node(type=PAGE, name=DEFAULT_PAGE)]
        doc.current_index = min(max(0, int(data.get("current", 0))), len(doc.pages) - 1)
        return doc


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _to_unit(value: Any) -> float:
    """Interpret a colour component written 0-255 or 0-1 as 0-1."""
    v = float(value)
    return v / 255 if v > 1 else v


def _round(value: float, places: int = 6) -> float:
    """Round for storage so a saved file is stable and diffable."""
    return round(float(value), places)


def _optional_float(value: Any) -> Optional[float]:
    """A stored per-corner radius, or None when that corner was never set."""
    return None if value is None else float(value)


# --------------------------------------------------------------------------- #
# Auto-layout helpers (Milestone 2). The model owns these so a Qt-free test and
# a live Qt canvas run the exact same arithmetic; only ``measure`` differs.
# --------------------------------------------------------------------------- #

#: A stacked column of children with no gap of its own is separated by this.
_GUTTER = 16.0


def _default_text_measure(node: "Node") -> tuple[float, float]:
    """The deterministic box a run of text occupies when no painter is present
    to shape it. A width estimate from the glyph count and an explicit height
    from the point size (or line-height). A painter backend replaces the whole
    callback with real HarfBuzz metrics via ``QFontMetrics``; the flex pass
    never notices which one ran, which is the point of the seam."""
    t = node.text
    if t is None:
        return (node.width, node.height)
    lines = t.chars.count("\n") + 1 if t.chars else 1
    line_h = t.line_height if t.line_height else t.size * TEXT_LINE_RATIO
    height = round(line_h * lines, 6)
    width = round(max(len(t.chars), 1) * t.size * 0.55, 6)
    return (width, height)


def _is_fill(child: "Node", is_row: bool) -> bool:
    """Does the child stretch along the container's main axis?"""
    fill = (child.sizing_horizontal if is_row else child.sizing_vertical) == SIZING_FILL
    return fill or child.grow > 0


def _cross_stretch(child: "Node", is_row: bool, counter_align: str) -> bool:
    """Does the child stretch across the container (STRETCH or a cross FILL)?"""
    mode = child.align_self or counter_align
    if mode == ALIGN_STRETCH:
        return True
    cross_sizing = child.sizing_vertical if is_row else child.sizing_horizontal
    return cross_sizing == SIZING_FILL


def _cross_offset(mode: str, start: float, available: float, size: float) -> float:
    """Where a child sits on the cross axis inside the container's inner band."""
    if mode == ALIGN_CENTER:
        return start + (available - size) / 2
    if mode == ALIGN_MAX:
        return start + (available - size)
    return start
