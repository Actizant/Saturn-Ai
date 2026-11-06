"""Saturn Studio -- the native canvas host.

``NativeStudioWindow`` is Saturn's clean-room design engine: a ``QWidget``
canvas that satisfies the *host surface* ``saturn_studio/tool.py`` calls --
``open_page``, ``put_away``, ``is_on_screen``, ``wait_ready``, ``state``,
``catalog``, ``run_tool``, ``set_tool``, ``new_document``, ``view``,
``open_document``, ``open_fig``, ``save_document`` -- so the voice dispatcher
and the agents cannot tell which engine answers.

What it is, and why it is built this way:

* No loopback server, no Chromium, no JS bridge. The verbs run synchronously
  in Python through :mod:`saturn_studio.native.tools` against the Qt-free
  scene; the pixels come from :mod:`saturn_studio.native.items`, and PNG/SVG
  export through :mod:`saturn_studio.native.export`.
* Every method answers from whichever thread called it. A voice loop waiting
  on the canvas is normal, so anything that touches a widget is marshalled to
  the GUI thread through one signal. A call that cannot reach the GUI thread
  is reported as not having answered, never as a crash.
* It reads Saturn's own ``.saturn`` format (``open_document``) and refuses
  ``.fig`` in plain words (``open_fig``). Pretending to open a Figma file it
  cannot understand would be the kind of fake the whole project exists to avoid.

The runtime :class:`StudioState` lives on ``self.rt`` so the host surface can
own the ``state`` name the router calls. Run standalone:
    .venv312\\Scripts\\python.exe -m saturn_studio.native.window
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from saturn_studio.native import document as saturn_document
from saturn_studio.native import export as native_export
from saturn_studio.native import scene as model
from saturn_studio.native import tools
from saturn_studio.native.items import StudioCanvas
from saturn_studio.native.state import StudioState

logger = logging.getLogger("saturn.studio.native.window")

BASE_DIR = Path(__file__).resolve().parents[2]

# The dark chrome, shared with the canvas well so the studio reads as one app.
_INK = "#0b0e14"
_PANEL = "#141a22"
_ACCENT = "#2bd6ff"
_TEXT = "#e7edf3"
_DIM = "#5c6b78"


class NativeStudioWindow(QWidget):
    """The design canvas as one widget, speaking the host surface."""

    _instance: Optional["NativeStudioWindow"] = None

    #: (callable, box, done). Delivered on the GUI thread, which owns widgets.
    _gui_call = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        NativeStudioWindow._instance = self

        self.rt = StudioState()          # the Qt-free runtime authority
        self._host_index: Optional[int] = None
        self._last_path: Optional[str] = None

        self.setWindowTitle("Saturn Studio")
        self.setStyleSheet(f"background:{_INK};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        self.canvas = StudioCanvas(self.rt, self)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.canvas, 1)

        # The GUI-thread door for calls made from a voice or worker thread.
        self._gui_call.connect(self._on_gui_call)

        # Milestone 3: the very Qt painter that draws the canvas also answers
        # the export verbs. Attach it to the runtime state so tools.export_image
        # and tools.export_svg rasterise through the live engine -- writing into
        # the same Studio folder save_document already uses.
        native_export.attach(self.rt, str(self._studio_dir()))

        # The engine is in this process; it is ready as soon as the widget is.
        self.rt.ready = True
        self.rt.status = "READY"

    # -- chrome ------------------------------------------------------------ #

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(34)
        bar.setStyleSheet(f"background:{_PANEL};border-bottom:1px solid #060a0f;")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(10)

        brand = QLabel("SATURN STUDIO")
        brand.setStyleSheet(f"color:{_TEXT};font-weight:600;letter-spacing:1px;")
        layout.addWidget(brand)

        self._doc_label = QLabel("untitled")
        self._doc_label.setStyleSheet(f"color:{_ACCENT};")
        layout.addWidget(self._doc_label)

        self._status_label = QLabel("READY")
        self._status_label.setStyleSheet(f"color:{_DIM};")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        layout.addWidget(self._status_label)
        return bar

    def _refresh_chrome(self) -> None:
        self._doc_label.setText(self.rt.doc_name or "untitled")
        self._status_label.setText(str(self.rt.status))

    # -- the GUI-thread door ----------------------------------------------- #

    def _on_gui_call(self, payload) -> None:
        fn, box, done = payload
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 -- a slot that raises kills Qt
            logger.exception("native studio GUI call failed")
            box["error"] = str(exc)
        finally:
            if done is not None:
                done.set()

    def _gui(self, fn: Callable[[], Any], *, timeout: float = 8.0) -> Any:
        """Run ``fn`` on the thread that owns the widgets, from anywhere.

        On the GUI thread it simply runs. From another thread it is queued on
        ``_gui_call`` and waited out; no answer within ``timeout`` returns
        ``None`` so the caller reports "did not answer", never a crash.
        """
        if threading.current_thread() is threading.main_thread():
            return fn()
        box: dict[str, Any] = {}
        done = threading.Event()
        self._gui_call.emit((fn, box, done))
        if not done.wait(timeout):
            logger.warning("native studio GUI thread did not answer within %.0fs", timeout)
            return None
        if "error" in box:
            logger.warning("native studio GUI call failed: %s", box["error"])
        return box.get("value")

    def _repaint(self) -> None:
        self._gui(self._do_repaint, timeout=4.0)

    def _do_repaint(self) -> None:
        self.canvas.rebuild()
        self._refresh_chrome()

    # -- the host surface -------------------------------------------------- #

    def open_page(self, *, timeout: float = 30.0) -> dict:
        return self._gui(self._do_open_page, timeout=timeout) or {}

    def _do_open_page(self) -> dict:
        stack = self.parentWidget()
        hosted = isinstance(stack, QStackedWidget)
        if hosted and stack.currentWidget() is not self:
            self._host_index = stack.currentIndex()
            stack.setCurrentIndex(stack.indexOf(self))
        top = self.window()
        if top is not None and not top.isVisible():
            top.show()
        if top is not None:
            top.raise_()
            top.activateWindow()
        self.rt.status = "READY"
        self._refresh_chrome()
        return {"shown": True, "hosted": hosted, "booted": True}

    def put_away(self) -> dict:
        return self._gui(self._do_put_away) or {}

    def _do_put_away(self) -> dict:
        stack = self.parentWidget()
        if isinstance(stack, QStackedWidget):
            index = self._host_index if self._host_index is not None else 0
            stack.setCurrentIndex(index)
            return {"hidden": True, "restored": index}
        self.hide()
        return {"hidden": True}

    def is_on_screen(self) -> bool:
        return bool(self._gui(self.isVisible))

    def wait_ready(self, *, timeout: float = 25.0) -> bool:
        # The engine lives in this process; once the widget exists it is ready.
        return bool(self.rt.ready)

    def state(self) -> dict:
        """The board as ``tool.py`` reads it. Counts come off the model; the
        layer list is the current page's top-level children, back to front."""
        document = self.rt.document
        page = document.current_page
        nodes = [n for n in page.walk() if n.type != model.PAGE]
        layers = [{"id": node.id, "name": node.name, "type": node.type,
                   "children": len(node.children)} for node in page.children]
        return {
            "ready": self.rt.ready,
            "doc": self.rt.doc_name,
            "status": self.rt.status,
            "tool": self.rt.tool,
            "page": page.name,
            "pages": [p.name for p in document.pages],
            "nodes": len(nodes),
            "layers": layers,
        }

    def catalog(self) -> list:
        return tools.catalog()

    def run_tool(self, name: str, args: Optional[dict] = None, *, timeout: float = 25.0) -> dict:
        """Run one verb against the scene and repaint. The verb is Qt-free and
        mutates the model directly; only the projection hops to the GUI thread.
        An unknown verb answers through the same ``{ok, error}`` envelope the
        router reads."""
        if not tools.has(name):
            return {"ok": False, "error": f"Saturn Studio has no {name} tool"}
        answer = tools.run(self.rt, name, args)
        self._repaint()
        return answer

    def set_tool(self, tool: str) -> dict:
        result = self.rt.set_tool(tool)
        if result.get("ok"):
            self._repaint()
        return result

    def new_document(self) -> dict:
        def _do() -> dict:
            self.rt.snapshot()
            self.rt.document = model.Document()
            self.rt.select([])
            self.rt.doc_name = "untitled"
            self.rt.status = "NEW DOCUMENT"
            self.canvas.rebuild()
            self._refresh_chrome()
            return {"ok": True}
        return self._gui(_do) or {"ok": False, "error": "the canvas did not answer"}

    def view(self, kind: str) -> dict:
        answer = self.rt.view(kind)
        if answer.get("ok"):
            self._gui(self._do_view_repaint, timeout=6.0)
        return answer

    def _do_view_repaint(self) -> None:
        self.canvas.zoom_to_fit()
        self.canvas.rebuild()
        self._refresh_chrome()

    def open_fig(self, path: Path) -> dict:
        # Honest limit: the native engine reads Saturn's format, not Figma's.
        return {"ok": False,
                "error": "the native Saturn engine reads .saturn documents, not .fig files"}

    def open_document(self, path: Path) -> dict:
        """Open a saved ``.saturn`` document onto the canvas.

        Saturn's own format round-trips what ``save_document`` wrote: the reader
        runs first, so a missing or corrupt file comes back as an error and never
        leaves a half-opened canvas. On success the undo snapshot is taken before
        the swap, so opening is reversible like every other mutation."""
        def _do() -> dict:
            try:
                loaded, _media = saturn_document.load_document(path)
            except Exception as exc:  # noqa: BLE001 -- a bad file is an answer
                return {"ok": False, "error": str(exc)}
            self.rt.snapshot()
            self.rt.document = loaded
            self.rt.select([])
            self.rt.doc_name = Path(path).stem
            self.rt.status = f"OPENED {Path(path).name}"
            self.canvas.rebuild()
            self._refresh_chrome()
            return {"ok": True, "doc": self.rt.doc_name,
                    "layers": len(loaded.current_page.children)}
        return self._gui(_do, timeout=15.0) or {"ok": False, "error": "the canvas did not answer"}

    def _studio_dir(self) -> Path:
        """The desktop ``Studio`` folder documents and exports land in, so users
        look in one predictable place for everything Saturn draws."""
        try:
            from core.user_paths import get_desktop_output_dir

            return get_desktop_output_dir() / "Studio"
        except Exception:  # noqa: BLE001 -- fall back to the repo output folder
            logger.exception("could not resolve the Saturn Studio output folder")
            return BASE_DIR / "Studio"

    def save_document(self, *, timeout: float = 15.0) -> dict:
        """Write the current scene to Saturn's Studio folder as ``.saturn``.

        The desktop ``Studio`` directory is the single destination, so "save"
        lands where users already look -- the same folder the export verbs write
        into."""
        name = (self.rt.doc_name or "untitled").strip() or "untitled"
        target = self._studio_dir()
        try:
            written = saturn_document.save_document(self.rt.document, target / name)
        except Exception as exc:  # noqa: BLE001 -- a failed save is an answer
            return {"ok": False, "error": str(exc)}
        self._last_path = str(written)
        self.rt.status = f"SAVED {written.name}"
        self._repaint()
        return {"ok": True, "path": str(written)}

    # -- housekeeping ------------------------------------------------------ #

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if NativeStudioWindow._instance is self:
            NativeStudioWindow._instance = None
        super().closeEvent(event)


def get_window(parent=None, *, timeout: float = 30.0) -> Optional[NativeStudioWindow]:
    """The native canvas, or None. Never build a widget on a thread that owns
    none just to answer a tool -- the running instance is reused."""
    return NativeStudioWindow._instance


def main() -> int:
    """Standalone smoke entry: open the native studio in its own window."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    window = NativeStudioWindow()
    window.resize(1400, 860)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
