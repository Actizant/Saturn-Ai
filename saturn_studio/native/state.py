"""Saturn Studio -- the runtime state of the native engine.

One :class:`StudioState` is everything the canvas is between strokes: the
document, the selection, the active tool, the viewport, and the undo history.
It deliberately holds no painter: the Qt backend reads this model to draw and
writes back through it, exactly like the verbs do, and the same state keeps
working if a different painter is ever bolted on (the swappable-painter rule
from the engine plan).

Undo is honest snapshots, not a command log. A design document is small and a
snapshot cannot half-undo; Milestone 1 buys correctness for memory it can
afford. The cap keeps a long voice session from growing the past without end.
"""

from __future__ import annotations

import json
from typing import Optional

from saturn_studio.native import scene

#: The selectable active-tool modes, in the order the router lists them (in
#: ``main.py`` and ``agent/planner.py``). Each names a scene kind the engine
#: actually understands -- selection or one of the five node types it renders.
#: Freehand (PEN) and grab-pan (HAND) are deliberately absent: the gesture layer
#: implements select/move/resize and wheel-pan, so a tool that implied drawing a
#: path or dragging-to-pan would promise an interaction the canvas does not do.
TOOLS = ("SELECT", "FRAME", "RECTANGLE", "ELLIPSE", "LINE", "TEXT")

#: Undo history is capped; the oldest steps fall away first.
UNDO_LIMIT = 64


class Viewport:
    """Where the canvas looks. Painter-agnostic: pixels, zoom, pan origin.

    The Qt backend maps scene points to these pixels in paintEvent; nothing
    in this model knows or cares how the mapping gets rasterised.
    """

    __slots__ = ("width", "height", "zoom", "pan_x", "pan_y")

    def __init__(self, width: float = 1280.0, height: float = 720.0):
        self.width = float(width)
        self.height = float(height)
        self.zoom = 1.0
        self.pan_x = 0.0     # scene point drawn at the top-left corner
        self.pan_y = 0.0

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.pan_x) * self.zoom, (y - self.pan_y) * self.zoom)

    def to_scene(self, x: float, y: float) -> tuple[float, float]:
        return (x / self.zoom + self.pan_x, y / self.zoom + self.pan_y)

    def clamp_zoom(self, zoom: float) -> float:
        return max(0.02, min(64.0, zoom))

    def zoom_to_fit(self, bounds: tuple[float, float, float, float], *, pad: float = 40.0) -> None:
        """Frame a scene rectangle with breathing room, the whole viewport."""
        bx, by, bw, bh = bounds
        if bw <= 0 or bh <= 0:
            self.zoom, self.pan_x, self.pan_y = 1.0, 0.0, 0.0
            return
        usable_w = max(1.0, self.width - 2 * pad)
        usable_h = max(1.0, self.height - 2 * pad)
        self.zoom = self.clamp_zoom(min(usable_w / bw, usable_h / bh))
        # centre the fitted content in the leftover margin
        drawn_w, drawn_h = bw * self.zoom, bh * self.zoom
        self.pan_x = bx - ((self.width - drawn_w) / 2) / self.zoom
        self.pan_y = by - ((self.height - drawn_h) / 2) / self.zoom


class StudioState:
    """The whole native canvas between edits: document, selection, tool, view."""

    def __init__(self, document: Optional[scene.Document] = None):
        self.document = document or scene.Document()
        self.selection: list[str] = []      # node ids, first selected last moved
        self.tool = "SELECT"
        self.viewport = Viewport()
        # Optional text metric the painter backend installs so the flex pass
        # sizes text with real shaping; the model falls back to a deterministic
        # estimate when it is None. The state still holds no painter itself.
        self.measure = None
        # Milestone 3 export seams. The Qt backend (native/export.py) installs
        # two callables that rasterise a node subtree to PNG bytes and to an SVG
        # string, plus the folder the verbs write files into. When they are None
        # -- every Qt-free test, and any host with no painter attached -- the
        # export verbs refuse by name rather than inventing bytes. The model
        # still owns what is drawn; it only borrows the painter to put pixels down.
        self.export_png = None
        self.export_svg = None
        self.export_dir = None
        self.status = "BOOTING"
        self.ready = False
        self.doc_name = "untitled"
        self._undo: list[str] = []
        self._redo: list[str] = []

    # ------------------------------------------------------------------ #
    # Selection
    # ------------------------------------------------------------------ #

    def selected_nodes(self) -> list[scene.Node]:
        found = [self.document.find_by_id(nid) for nid in self.selection]
        return [node for node in found if node is not None]

    def primary(self) -> Optional[scene.Node]:
        nodes = self.selected_nodes()
        return nodes[-1] if nodes else None

    def select(self, node_ids: list[str] | str | None) -> None:
        if node_ids is None:
            self.selection = []
        elif isinstance(node_ids, str):
            self.selection = [node_ids]
        else:
            self.selection = list(node_ids)

    # ------------------------------------------------------------------ #
    # Undo history: snapshot before mutating, swap whole documents back
    # ------------------------------------------------------------------ #

    def snapshot(self) -> None:
        """Call before any mutation a user could ask to undo."""
        self._undo.append(_encode(self.document))
        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(_encode(self.document))
        self.document = _decode(self._undo.pop())
        self._drop_dead_selection()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(_encode(self.document))
        self.document = _decode(self._redo.pop())
        self._drop_dead_selection()
        return True

    def _drop_dead_selection(self) -> None:
        live = {node.id for node in self.document.all_nodes()}
        self.selection = [nid for nid in self.selection if nid in live]

    # ------------------------------------------------------------------ #
    # View verbs, the same three the web shell's viewAction answers for
    # ------------------------------------------------------------------ #

    def view(self, kind: str) -> dict:
        """undo | redo | zoom (zoom-to-fit). Answers {ok}/{ok,error}."""
        try:
            if kind == "zoom":
                bounds = self.document.content_bounds()
                if bounds is None:
                    self.viewport.zoom, self.viewport.pan_x, self.viewport.pan_y = 1.0, 0.0, 0.0
                else:
                    self.viewport.zoom_to_fit(bounds)
                return {"ok": True}
            if kind == "undo":
                if not self.undo():
                    return {"ok": False, "error": "nothing to undo"}
                return {"ok": True}
            if kind == "redo":
                if not self.redo():
                    return {"ok": False, "error": "nothing to redo"}
                return {"ok": True}
            return {"ok": False, "error": f"unknown view action '{kind}'"}
        except Exception as error:              # the engine's own words
            return {"ok": False, "error": str(error)}

    def set_tool(self, tool: str) -> dict:
        wanted = str(tool).strip().upper()
        if wanted not in TOOLS:
            return {"ok": False, "error": f"Saturn Studio has no {tool} tool"}
        self.tool = wanted
        self.status = f"TOOL {wanted}"
        return {"ok": True}


# --------------------------------------------------------------------------- #
# Snapshot codec: a document's dict form as JSON text. Text is what the undo
# stacks hold, so swapping a document back can never alias live nodes.
# --------------------------------------------------------------------------- #

def _encode(document: scene.Document) -> str:
    return json.dumps(document.to_dict())


def _decode(payload: str) -> scene.Document:
    return scene.Document.from_dict(json.loads(payload))
