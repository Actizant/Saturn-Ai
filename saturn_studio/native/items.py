"""Saturn Studio -- the Qt painter backend (Milestone 1).

This is the one place in the native engine that puts a pixel down. It reads the
Qt-free scene model and *projects* it into a ``QGraphicsView``: one item per
node, geometry and paint taken straight off the model, never measured twice
into something the model does not say. A future Skia-GPU backend would sit
beside this file and read the very same scene -- that boundary is what the
swappable-painter rule protects, and it is why nothing here owns state.

Interaction is click to select, drag to move, eight handles to resize, wheel
to zoom, Delete to remove. Auto-layout runs in the model (M2); the canvas only
installs a ``QFontMetrics`` text measurer so the flex pass sizes runs with real
shaping -- every number it draws is the model's, never measured twice here.

The accent colours are Saturn's own -- the same cyan and ink the rest of the
Command Center reads -- so the canvas looks like Saturn, not like a web page.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
    QPainterPath, QPen,
)
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QWidget

from saturn_studio.native import scene as model
from saturn_studio.native.state import StudioState

# --------------------------------------------------------------------------- #
# Palette -- Saturn's tokens, not a borrowed theme.
# --------------------------------------------------------------------------- #

INK = QColor("#0b0e14")          # the canvas well
ACCENT = QColor("#2bd6ff")       # selection, handles
ACCENT_DIM = QColor("#2bd6ff")
ACCENT_DIM.setAlpha(180)


def _qcolor(color: model.Color) -> QColor:
    """A model colour (0..1 float rgba) as a Qt colour (0..255 int rgba)."""
    return QColor(round(color.r * 255), round(color.g * 255),
                  round(color.b * 255), round(color.a * 255))


def _make_font(text: "model.Text") -> QFont:
    """The one font a run gets, shared by the painter and the measurer so the
    box the layout reserves is exactly the box the glyphs fill."""
    font = QFont(text.family)
    font.setPixelSize(max(1, int(round(text.size))))
    font.setWeight(_qweight(text.weight))
    font.setItalic(text.italic)
    return font


def qt_text_measure(node: "model.Node") -> tuple[float, float]:
    """Real shaping for the flex pass: the advance width of the widest line and
    the line count times the font's leading. Installed on the runtime state so
    :meth:`scene.Node.run_layout` measures text with HarfBuzz through Qt rather
    than the deterministic estimate it falls back to when no painter is present.
    This is the seam -- the model asks, the painter answers, the model still
    owns the arithmetic."""
    text = node.text
    if text is None or not text.chars:
        return (node.width, node.height)
    metrics = QFontMetrics(_make_font(text))
    lines = text.chars.split("\n")
    width = max(metrics.horizontalAdvance(line) for line in lines) if lines else 0
    line_h = text.line_height if text.line_height else metrics.lineSpacing()
    height = line_h * len(lines)
    return (max(1.0, float(width)), max(1.0, float(height)))


def _rounded_path(rect: QRectF, radii: tuple[float, float, float, float]) -> QPainterPath:
    """A path for a box with four independent corner radii, top-left clockwise.

    ``QPainterPath.addRoundedRect`` only takes one radius, and the model keeps
    per-corner values (``set_radius`` can set them apart), so the path is built
    from the four corner arcs. A radius larger than half the shorter side is
    clamped so a fat rounding can never fold the shape over itself.
    """
    w, h = rect.width(), rect.height()
    tl, tr, br, bl = radii
    limit = min(w, h) / 2.0
    tl, tr, br, bl = (max(0.0, min(v, limit)) for v in (tl, tr, br, bl))

    path = QPainterPath()
    path.moveTo(rect.left() + tl, rect.top())
    path.lineTo(rect.right() - tr, rect.top())
    if tr:
        path.arcTo(QRectF(rect.right() - 2 * tr, rect.top(), 2 * tr, 2 * tr), 90.0, -90.0)
    path.lineTo(rect.right(), rect.bottom() - br)
    if br:
        path.arcTo(QRectF(rect.right() - 2 * br, rect.bottom() - 2 * br, 2 * br, 2 * br), 0.0, -90.0)
    path.lineTo(rect.left() + bl, rect.bottom())
    if bl:
        path.arcTo(QRectF(rect.left(), rect.bottom() - 2 * bl, 2 * bl, 2 * bl), 270.0, -90.0)
    path.lineTo(rect.left(), rect.top() + tl)
    if tl:
        path.arcTo(QRectF(rect.left(), rect.top(), 2 * tl, 2 * tl), 180.0, -90.0)
    path.closeSubpath()
    return path


def _fill_brush(node: model.Node, rect: QRectF) -> Optional[QBrush]:
    """The first visible paint as a Qt brush. A solid becomes a colour; a
    gradient maps its unit-square axis onto the node's own box."""
    for fill in node.fills:
        if not fill.visible:
            continue
        if fill.kind == "solid":
            return QBrush(_qcolor(fill.color))
        gx1, gy1, gx2, gy2 = fill.x1, fill.y1, fill.x2, fill.y2
        gradient = QLinearGradient(
            rect.left() + gx1 * rect.width(), rect.top() + gy1 * rect.height(),
            rect.left() + gx2 * rect.width(), rect.top() + gy2 * rect.height(),
        )
        stops = fill.stops or [model.GradientStop(fill.color, 0.0),
                               model.GradientStop(model.Color(1, 1, 1), 1.0)]
        for stop in stops:
            gradient.setColorAt(max(0.0, min(1.0, stop.position)), _qcolor(stop.color))
        return QBrush(gradient)
    return None


# --------------------------------------------------------------------------- #
# One item per node -- a projection, never an owner of geometry.
# --------------------------------------------------------------------------- #

class NodeItem(QGraphicsItem):
    """Draws a single model node at its absolute scene position.

    The item holds no width/height/radius of its own: every value is read off
    ``node`` at paint time, so a model edit and a rebuild always agree. Items
    are flat in the scene and positioned by ``absolute_bounds``; the tree lives
    in the model, and keeping the projection flat is what lets selection and
    resize handles sit on the same numbers ``describe`` reports.
    """

    def __init__(self, node: model.Node):
        super().__init__()
        self.node = node
        self.setPos(node.absolute_bounds()[0], node.absolute_bounds()[1])
        self.setZValue(0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def boundingRect(self) -> QRectF:
        # Local box is (0,0,w,h); a margin leaves room for stroke and shadow.
        w, h = self.node.width, self.node.height
        bleed = max(4.0, self._stroke_weight() / 2 + 2)
        return QRectF(-bleed, -bleed, w + 2 * bleed, h + 2 * bleed)

    def _stroke_weight(self) -> float:
        for stroke in self.node.strokes:
            if stroke.visible:
                return stroke.weight
        return 0.0

    def paint(self, painter: QPainter, option, widget=None) -> None:
        node = self.node
        if not node.visible:
            return
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, node.opacity)))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if node.rotation:
            cx, cy = node.width / 2, node.height / 2
            painter.translate(cx, cy)
            painter.rotate(node.rotation)
            painter.translate(-cx, -cy)

        rect = QRectF(0, 0, node.width, node.height)
        if node.type == model.TEXT:
            self._paint_text(painter, node, rect)
        elif node.type == model.LINE:
            self._paint_line(painter, node, rect)
        elif node.type == model.ELLIPSE:
            self._paint_ellipse(painter, node, rect)
        else:  # FRAME, RECTANGLE, PAGE -- a (possibly rounded) box
            self._paint_box(painter, node, rect)
        painter.restore()

    # -- per-kind drawing -------------------------------------------------- #

    def _paint_box(self, painter: QPainter, node: model.Node, rect: QRectF) -> None:
        brush = _fill_brush(node, rect)
        if brush is not None:
            painter.setBrush(brush)
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        self._apply_stroke(painter, node)
        path = _rounded_path(rect, node.effective_corners())
        painter.drawPath(path)

    def _paint_ellipse(self, painter: QPainter, node: model.Node, rect: QRectF) -> None:
        brush = _fill_brush(node, rect)
        painter.setBrush(brush if brush is not None else Qt.BrushStyle.NoBrush)
        self._apply_stroke(painter, node)
        painter.drawEllipse(rect)

    def _paint_line(self, painter: QPainter, node: model.Node, rect: QRectF) -> None:
        # A line runs across its box from top-left to bottom-right; the model
        # stores the extents as width/height, which is exactly the segment.
        self._apply_stroke(painter, node, fallback=1.0)
        painter.drawLine(rect.topLeft(), rect.bottomRight())

    def _apply_stroke(self, painter: QPainter, node: model.Node, fallback: float = 0.0) -> None:
        for stroke in node.strokes:
            if stroke.visible:
                pen = QPen(_qcolor(stroke.color), stroke.weight)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                return
        if fallback > 0:
            painter.setPen(QPen(ACCENT_DIM, fallback))
        else:
            painter.setPen(Qt.PenStyle.NoPen)

    def _paint_text(self, painter: QPainter, node: model.Node, rect: QRectF) -> None:
        text = node.text
        if text is None or not text.chars:
            return
        font = _make_font(text)
        painter.setFont(font)

        # The run's own colour is the paint when set; otherwise the fill list.
        color = _qcolor(text.color)
        brush = _fill_brush(node, rect)
        painter.setPen(color if brush is None else QPen(color))

        align = Qt.AlignmentFlag.AlignLeft
        if text.align == "center":
            align = Qt.AlignmentFlag.AlignHCenter
        elif text.align == "right":
            align = Qt.AlignmentFlag.AlignRight
        elif text.align == "justify":
            align = Qt.AlignmentFlag.AlignJustify
        valign = Qt.AlignmentFlag.AlignTop
        if text.valign == "center":
            valign = Qt.AlignmentFlag.AlignVCenter
        elif text.valign == "bottom":
            valign = Qt.AlignmentFlag.AlignBottom

        flags = align | valign | Qt.TextFlag.TextWordWrap
        painter.drawText(rect, int(flags), text.chars)


def _qweight(weight: int) -> "QFont.Weight":
    """A CSS-ish 100..900 to the nearest Qt font-weight enum Qt will honour.

    PyQt6 moved these onto ``QFont.Weight``; ``Qt.QFontWeight`` does not exist,
    and it is a live path here because the text measurer builds a font on every
    flex ``render``, not only when the canvas paints."""
    if weight >= 700:
        return QFont.Weight.Bold
    if weight >= 500:
        return QFont.Weight.DemiBold
    if weight <= 300:
        return QFont.Weight.Light
    return QFont.Weight.Normal


# --------------------------------------------------------------------------- #
# The canvas widget: a QGraphicsView that projects one StudioState.
# --------------------------------------------------------------------------- #

_HANDLE = 8.0  # selection handle edge, in view pixels


class StudioCanvas(QGraphicsView):
    """The design surface. It owns no document of its own: it renders the
    :class:`StudioState` it is given and writes user gestures back through that
    same state, so a mouse drag and a ``render`` verb change exactly one thing
    -- the model -- and agree on every number afterwards."""

    def __init__(self, state: StudioState, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.state = state
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHints(QPainter.RenderHint.Antialiasing
                            | QPainter.RenderHint.TextAntialiasing
                            | QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QBrush(INK))
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)

        # Gesture bookkeeping: what a press started and how far it has moved.
        self._drag_mode: Optional[str] = None      # "move" | "resize" | None
        self._resize_corner: Optional[int] = None  # 0..7, or None for a move
        self._last_scene = QPointF()
        self._press_scene = QPointF()

        # Offer the real shaper to the model's flex pass before anything lays
        # out, so a ``render`` on this canvas measures text with Qt, not the
        # Qt-free estimate. The model still owns every number it places.
        self.state.measure = qt_text_measure

        self.rebuild()

    # -- projection -------------------------------------------------------- #

    def rebuild(self) -> None:
        """Tear the scene down and project the model again. Cheap enough for a
        design document, and it guarantees the pixels match the authority."""
        self._scene.clear()
        page = self.state.document.current_page
        for node in page.walk():
            if node is page:
                continue
            if not node.visible:
                continue
            self._scene.addItem(NodeItem(node))
        self.viewport().update()

    # -- selection geometry ------------------------------------------------ #

    def _selection_rect_scene(self) -> Optional[QRectF]:
        nodes = self.state.selected_nodes()
        if not nodes:
            return None
        boxes = [QRectF(*node.absolute_bounds()) for node in nodes]
        rect = boxes[0]
        for box in boxes[1:]:
            rect = rect.united(box)
        return rect

    def _handle_points(self) -> list[QPointF]:
        """The eight resize points in *view* coordinates, corner-first:
        tl, t, tr, r, br, b, bl, l. drawForeground and the mouse agree here."""
        rect = self._selection_rect_scene()
        if rect is None:
            return []
        tl = self.mapFromScene(rect.topLeft())
        br = self.mapFromScene(rect.bottomRight())
        left, top = tl.x(), tl.y()
        right, bottom = br.x(), br.y()
        cx, cy = (left + right) / 2, (top + bottom) / 2
        return [QPointF(left, top), QPointF(cx, top), QPointF(right, top),
                QPointF(right, cy), QPointF(right, bottom), QPointF(cx, bottom),
                QPointF(left, bottom), QPointF(left, cy)]

    def _handle_at(self, view_point: QPointF) -> Optional[int]:
        for index, point in enumerate(self._handle_points()):
            if abs(view_point.x() - point.x()) <= _HANDLE and abs(view_point.y() - point.y()) <= _HANDLE:
                return index
        return None

    # -- painting the chrome (selection + handles) ------------------------- #

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        box = self._selection_rect_scene()
        if box is None:
            return
        tl = self.mapFromScene(box.topLeft())
        br = self.mapFromScene(box.bottomRight())
        outline = QRectF(tl, br).normalized()

        painter.setPen(QPen(ACCENT, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(outline)

        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(ACCENT, 1.0))
        half = _HANDLE / 2
        for point in self._handle_points():
            painter.drawRect(QRectF(point.x() - half, point.y() - half, _HANDLE, _HANDLE))

    # -- gestures ---------------------------------------------------------- #

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            view_point = QPointF(event.position())
            corner = self._handle_at(view_point)
            if corner is not None and len(self.state.selection) == 1:
                self._drag_mode = "resize"
                self._resize_corner = corner
                self._last_scene = self.mapToScene(view_point.toPoint())
                event.accept()
                return

            scene_point = self.mapToScene(event.position().toPoint())
            node = self.state.document.current_page.hit_test(scene_point.x(), scene_point.y())
            add = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if node is None:
                if not add:
                    self.state.select([])
            else:
                if add:
                    ids = list(self.state.selection)
                    if node.id in ids:
                        ids.remove(node.id)
                    else:
                        ids.append(node.id)
                    self.state.select(ids)
                else:
                    self.state.select([node.id])
            self._drag_mode = "move" if node is not None else None
            self._press_scene = scene_point
            self._last_scene = scene_point
            self.rebuild()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_mode in ("move", "resize") and self.state.primary() is not None:
            scene_point = self.mapToScene(event.position().toPoint())
            node = self.state.primary()
            if self._drag_mode == "move":
                dx = scene_point.x() - self._last_scene.x()
                dy = scene_point.y() - self._last_scene.y()
                for selected in self.state.selected_nodes():
                    selected.x += dx
                    selected.y += dy
                self._last_scene = scene_point
            else:
                self._apply_resize(node, scene_point)
            self.rebuild()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_mode = None
        self._resize_corner = None
        super().mouseReleaseEvent(event)

    def _apply_resize(self, node: model.Node, scene_point: QPointF) -> None:
        corner = self._resize_corner
        if corner is None:
            return
        # Corner index: 0 tl, 1 t, 2 tr, 3 r, 4 br, 5 b, 6 bl, 7 l. Each edge
        # of the box is dragged by the handle on it; the far corner holds still.
        ax, ay, _, _ = node.absolute_bounds()
        right, bottom = ax + node.width, ay + node.height
        new_left = scene_point.x() if corner in (0, 6, 7) else ax
        new_right = scene_point.x() if corner in (2, 3, 4) else right
        new_top = scene_point.y() if corner in (0, 1, 2) else ay
        new_bottom = scene_point.y() if corner in (4, 5, 6) else bottom
        width = max(1.0, new_right - new_left)
        height = max(1.0, new_bottom - new_top)
        # The model stores position relative to the parent, so fold the new
        # absolute origin back down by the same delta the box's corner moved.
        node.x += new_left - ax
        node.y += new_top - ay
        node.width = width
        node.height = height

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.15 if delta > 0 else 1 / 1.15
        zoom = self.state.viewport.clamp_zoom(self.state.viewport.zoom * factor)
        self.state.viewport.zoom = zoom
        self.scale(factor, factor)
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete and self.state.selection:
            from saturn_studio.native import tools
            for nid in list(self.state.selection):
                tools.run(self.state, "delete_node", {"id": nid})
            self.rebuild()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Control):
            self.state.select([])
            self.rebuild()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- framing ----------------------------------------------------------- #

    def zoom_to_fit(self) -> None:
        bounds = self.state.document.content_bounds()
        if bounds is None:
            self.resetTransform()
            self.state.viewport.zoom = 1.0
            return
        self.state.viewport.width = self.viewport().width() or self.state.viewport.width
        self.state.viewport.height = self.viewport().height() or self.state.viewport.height
        self.state.viewport.zoom_to_fit(bounds)
        self.fitInView(QRectF(*bounds), Qt.AspectRatioMode.KeepAspectRatio)
