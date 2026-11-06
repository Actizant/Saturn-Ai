"""Saturn Studio -- the Milestone 3 painter export (PNG and SVG).

Everything the user sees on the live canvas is drawn by :meth:`items.NodeItem.paint`.
Export borrows that exact routine so a PNG or an SVG is *the canvas at another
destination*, never a second renderer that could drift from it. This module is
Qt through and through -- alongside ``items.py`` and the host it is one of the
only three places that touches ``QPainter`` -- and it changes nothing: it reads
the model's numbers and projects them, exactly like the view does.

The verbs in ``tools.py`` never import this file. The host calls :func:`attach`
to install two callables on the runtime state (``export_png`` / ``export_svg``)
in the same spirit as the Milestone 2 text-measure seam, so the Qt-free engine
stays Qt-free and, when no painter is attached, honestly refuses to export
rather than inventing bytes it never drew.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QImage, QPainter

from saturn_studio.native import scene as model
from saturn_studio.native.items import NodeItem


def _content_rect(root: "model.Node") -> QRectF:
    """The absolute box that bounds the exported subtree.

    A page exports the union of its visible layers -- that is what "the design"
    means on a page -- while any other node exports its own absolute box. Every
    number here is the model's own ``absolute_bounds``, so the crop is exactly
    what ``describe`` would report for the same subtree.
    """
    if root.type == model.PAGE:
        boxes = [c.absolute_bounds() for c in root.children if c.visible]
        if not boxes:
            return QRectF()
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes)
        y1 = max(b[1] + b[3] for b in boxes)
        return QRectF(x0, y0, x1 - x0, y1 - y0)
    ax, ay, w, h = root.absolute_bounds()
    return QRectF(ax, ay, w, h)


def _paint_subtree(painter: QPainter, root: "model.Node") -> None:
    """Replay the live canvas's own ``paint`` for every visible node under root.

    The scene keeps items flat and positions each by its absolute origin, so the
    export walks the tree and translates the same way -- a node that moved in
    the model moves here too, because there is only one source of the number.
    """
    for node in root.walk():
        if node is root and node.type == model.PAGE:
            continue
        if not node.visible:
            continue
        ax, ay, _, _ = node.absolute_bounds()
        painter.save()
        painter.translate(ax, ay)
        NodeItem(node).paint(painter, None)
        painter.restore()


def _fill_background(painter: QPainter, rect: QRectF, background: Optional[str]) -> None:
    """Clear to transparent, or flood the crop with a solid colour.

    A design export is transparent by default, the way Figma and the web engine
    hand off a layer. ``background`` names a colour only when the caller asks.
    """
    if not background or background.lower() == "transparent":
        return
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(background))
    painter.drawRect(rect)
    painter.restore()


def render_png(root: "model.Node", *, scale: float = 1.0,
               background: Optional[str] = None) -> bytes:
    """Rasterise a node subtree to PNG bytes at ``scale`` (1x, 2x, ...).

    Returns empty bytes for an empty crop; the verb turns that into an honest
    "there is nothing to export" rather than writing a zero-byte file.
    """
    rect = _content_rect(root)
    if rect.isEmpty() or rect.width() <= 0 or rect.height() <= 0:
        return b""
    width = max(1, int(round(rect.width() * scale)))
    height = max(1, int(round(rect.height() * scale)))
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    _fill_background(painter, QRectF(0, 0, width, height), background)
    painter.scale(scale, scale)
    painter.translate(-rect.x(), -rect.y())
    _paint_subtree(painter, root)
    painter.end()

    buffer = QByteArray()
    device = QBuffer(buffer)
    device.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(device, "PNG")
    device.close()
    return bytes(buffer.data())


def render_svg(root: "model.Node", *, background: Optional[str] = None) -> str:
    """Draw a node subtree to a standalone SVG string, in the model's units.

    Vector, so no scale factor: a shape stays a path and text stays text. The
    viewBox is the same crop ``render_png`` uses, so SVG and PNG agree.

    ``QSvgGenerator`` reaches a ``QIODevice`` only through ``setDevice`` on some
    PyQt builds and not others, so this writes through a temporary file path --
    portable, and the bytes never leave the process.
    """
    import os
    import tempfile

    from PyQt6.QtSvg import QSvgGenerator

    rect = _content_rect(root)
    if rect.isEmpty() or rect.width() <= 0 or rect.height() <= 0:
        return ""
    width = max(1, int(round(rect.width())))
    height = max(1, int(round(rect.height())))

    handle, path = tempfile.mkstemp(suffix=".svg")
    os.close(handle)
    try:
        generator = QSvgGenerator()
        generator.setFileName(path)
        generator.setTitle("Saturn Studio export")
        generator.setDescription("Exported from Saturn's native design engine")
        generator.setSize(QSize(width, height))
        generator.setViewBox(QRectF(0, 0, width, height))

        painter = QPainter(generator)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        _fill_background(painter, QRectF(0, 0, width, height), background)
        painter.translate(-rect.x(), -rect.y())
        _paint_subtree(painter, root)
        painter.end()          # finishing the painter flushes the generator
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass



def attach(state, export_dir: Optional[str] = None):
    """Install the export callables onto a :class:`StudioState`.

    The host does this once, after it builds its canvas, mirroring how the
    canvas installs ``qt_text_measure`` for the flex pass. From then on the
    ``export_image`` / ``export_svg`` verbs draw through the very painter that
    is on screen.
    """
    state.export_png = lambda node, *, scale=1.0, background=None: render_png(
        node, scale=scale, background=background
    )
    state.export_svg = lambda node, *, background=None: render_svg(
        node, background=background
    )
    state.export_dir = Path(export_dir) if export_dir else None
    return state
