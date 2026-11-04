"""
Saturn's View -- the window Saturn looks through.

The engine in ``assets/saturn_view`` is deliberately unaware that Qt exists: it
is a browser application, and it runs in Chrome exactly as it runs here. This
module is the host, and it does three things and nothing else:

* owns an HTTP server on loopback, because ES modules will not load from
  ``file://`` and because a URL is what lets the same engine be opened in a
  normal browser when something needs debugging;
* owns one ``QWebEngineView`` with the same GPU flags the rest of Saturn uses,
  so the globe costs what a globe should cost;
* bridges the page's public API (``window.SaturnView``) into Python, both ways,
  so Saturn's brain drives the globe through the same surface the HUD uses.

There is no second implementation of anything in here. If Python needs to know
what the globe is doing, it asks the globe.

Run it on its own to see the engine with no assistant attached::

    .venv312\\\\Scripts\\\\python.exe -m saturn_view.window
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("saturn.view.window")

BASE_DIR = Path(__file__).resolve().parent.parent

# How close a "show me X" request puts the camera. Low enough that the ground
# track of a satellite crossing overhead still reads, high enough that a city
# arrives with its coastline and not its street grid.
DEFAULT_HEIGHT = 900_000.0

# Chromium must be told how to draw before Qt WebEngine starts, and it only
# reads this once. `import saturn_view.window` therefore has to happen before
# QApplication exists -- true in Saturn, which imports its windows lazily, and
# true in the standalone entry point at the bottom of this file.
_BASE_FLAGS = (
    "--enable-gpu-rasterization --enable-zero-copy --ignore-gpu-blocklist "
    "--enable-accelerated-2d-canvas --enable-webgl --enable-webgl2-compute-context "
    "--disable-frame-rate-limit --disable-gpu-vsync --num-raster-threads=4 "
    "--disable-gpu-driver-bug-workarounds"
)
if sys.platform == "win32":
    # Cesium renders through WebGL2; the default ANGLE backend on Windows is
    # D3D11 anyway, but saying so keeps a driver from choosing otherwise.
    _BASE_FLAGS += " --use-angle=d3d11"
elif sys.platform.startswith("linux"):
    _BASE_FLAGS += " --no-sandbox --disable-dev-shm-usage"
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", _BASE_FLAGS)

from PyQt6.QtCore import (  # noqa: E402  (after the flags, on purpose)
    QCoreApplication,
    QEventLoop,
    QFile,
    QIODevice,
    QObject,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QShortcut, QSurfaceFormat  # noqa: E402
from PyQt6.QtWebChannel import QWebChannel  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineScript, QWebEngineSettings  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QVBoxLayout, QWidget  # noqa: E402

from saturn_view import server as engine_server  # noqa: E402

try:  # Shared GL contexts and an unthrottled swap interval, as the rest of Saturn does.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    _format = QSurfaceFormat.defaultFormat()
    _format.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(_format)
except Exception:  # noqa: BLE001 -- already configured by another window
    pass

# --------------------------------------------------------------------------- #
# The page side of the bridge
# --------------------------------------------------------------------------- #

# Qt ships the channel shim as a compiled resource. Reading it here and injecting
# it as a document-creation script means the engine tree stays free of Qt code
# and the page works identically in a real browser, where this script never runs.
_QWEBCHANNEL_RESOURCE = ":/qtwebchannel/qwebchannel.js"

HOST_SCRIPT = r"""
(function () {
  'use strict';

  // The page loads before the engine's modules finish booting, so every stage
  // of this waits rather than assuming an order we do not control.
  var attempts = 0;

  function retry(step) {
    if (++attempts > 600) return;          // give up after a minute of silence
    setTimeout(step, 50);
  }

  function connect() {
    if (typeof QWebChannel === 'undefined' || !window.qt || !qt.webChannelTransport) {
      return retry(connect);
    }

    new QWebChannel(qt.webChannelTransport, function (channel) {
      var host = channel.objects.saturnView;
      if (!host) return retry(connect);

      window.__saturnHost = host;

      // A bridge that throws must never take the frame down with it. The globe
      // is the product; the host is a passenger.
      function post(kind, payload) {
        try {
          host.report(kind, JSON.stringify(payload === undefined ? null : payload));
        } catch (err) {
          console.warn('[saturn-view] host report dropped:', kind);
        }
      }

      function attach() {
        var sv = window.SaturnView;
        if (!sv) return retry(attach);

        window.__saturnHostReady = true;
        host.announce(
          JSON.stringify({
            version: sv.version,
            layers: sv.layers.list(),
            view: sv.camera.view(),
            metrics: sv.engine.metrics()
          })
        );

        sv.on('camera:moved', function (payload) { post('view', payload); });
        sv.on('layer:enabled', function (payload) { post('layer:enabled', payload); });
        sv.on('layer:disabled', function (payload) { post('layer:disabled', payload); });
        sv.on('layer:updated', function (payload) { post('layer:updated', payload); });
        sv.on('layer:failed', function (payload) { post('layer:failed', payload); });

        // Escape belongs to the window, not the document, so tell Python and
        // let it decide -- fullscreen first, then close.
        document.addEventListener('keydown', function (event) {
          if (event.key === 'Escape') {
            try { host.requestClose(); } catch (err) { /* nothing to do */ }
          }
        });
      }

      attempts = 0;
      attach();
    });
  }

  connect();
})();
"""

# One expression, one answer. Everything the assistant could want to know about
# the globe is read through the same public API the HUD uses -- no Python mirror
# of engine state, and therefore nothing to fall out of sync.
STATE_JS = """
(function () {
  var sv = window.SaturnView;
  if (!sv) return JSON.stringify({ ready: false });
  var live = document.getElementById('status-live');
  var boot = document.getElementById('boot');
  var credits = document.getElementById('credits-line');
  return JSON.stringify({
    ready: window.__saturnViewReady === true,
    version: sv.version,
    lens: sv.engine.lens ? sv.engine.lens() : null,
    lenses: sv.engine.lenses ? sv.engine.lenses() : null,
    lensReady: sv.engine.lensReady ? sv.engine.lensReady() : null,
    credits: credits ? credits.textContent : null,
    view: sv.camera.view(),
    viewport: sv.camera.viewport(),
    metrics: sv.engine.metrics(),
    terrain: sv.engine.terrain(),
    layers: sv.layers.list(),
    freshness: sv.freshness.snapshot(),
    status: live ? live.textContent : null,
    statusState: live ? live.className || null : null,
    boot: boot && !boot.hidden ? boot.textContent : null
  });
})()
"""


def _js(value: Any) -> str:
    """A Python value as a JavaScript literal, safely quoted."""
    return json.dumps(value, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    """Ask the operating system for a port nobody is using."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class EngineServer:
    """The engine's HTTP origin, for as long as the application lives.

    Loopback only. The engine is not a service and must never be one: it reads
    the machine it runs on and shows what it finds, which is not something to
    publish to a network.
    """

    def __init__(self, dist: Optional[str] = None, preferred_port: int = 8794):
        self._dist = dist
        self._preferred = preferred_port
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._url: Optional[str] = None

    @property
    def url(self) -> Optional[str]:
        return self._url

    @property
    def running(self) -> bool:
        return bool(self._server and self._server.started)

    def _candidates(self) -> list[int]:
        # The documented port first, so an operator can open the same view in a
        # browser and see exactly what Saturn sees; an ephemeral port after.
        seen: list[int] = []
        for port in (self._preferred, 0, 0):
            if port not in seen:
                seen.append(port)
        return seen

    def _bound_port(self, fallback: int) -> int:
        try:
            return int(self._server.servers[0].sockets[0].getsockname()[1])
        except Exception:  # noqa: BLE001 -- fall back to what we asked for
            return fallback

    def start(self, timeout: float = 10.0) -> str:
        """Start the server and return its base URL, or raise."""
        if self.running and self._url:
            return self._url

        try:
            import uvicorn
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"uvicorn is not available: {exc}") from exc

        failures: list[str] = []

        for port in self._candidates():
            app = engine_server.create_app(self._dist)
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                # Saturn owns its logging. uvicorn reconfiguring handlers from a
                # worker thread would silence or duplicate every other log line.
                log_config=None,
                log_level="warning",
                access_log=False,
            )
            candidate = uvicorn.Server(config)
            thread = threading.Thread(
                target=candidate.run, name="saturn-view-http", daemon=True
            )
            thread.start()

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if candidate.started:
                    self._server = candidate
                    self._thread = thread
                    self._url = f"http://127.0.0.1:{self._bound_port(port or _free_port())}/"
                    logger.info("engine served at %s", self._url)
                    return self._url
                if not thread.is_alive():
                    failures.append(f"port {port or 'auto'}: exited during startup")
                    break
                time.sleep(0.05)

            candidate.should_exit = True
            thread.join(timeout=2.0)
            if not failures:
                failures.append(f"port {port or 'auto'}: no answer in {timeout:.0f}s")

        raise RuntimeError("Saturn's View could not start its engine: " + "; ".join(failures))

    def stop(self, timeout: float = 3.0) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread, self._url = None, None, None
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)


# --------------------------------------------------------------------------- #
# The page's way back into Python
# --------------------------------------------------------------------------- #


def _load(payload: str) -> Any:
    try:
        return json.loads(payload) if payload else None
    except (TypeError, ValueError):
        return None


class ViewBridge(QObject):
    """Registered as ``saturnView`` in the page's channel.

    Slots are the page calling Python. Signals never cross the channel; they
    exist so the rest of Saturn can subscribe without knowing a page exists.
    """

    announced = pyqtSignal(dict)
    reported = pyqtSignal(str, dict)
    close_requested = pyqtSignal()
    console_line = pyqtSignal(str)

    @pyqtSlot(str)
    def announce(self, payload: str) -> None:
        self.announced.emit(_load(payload) or {})

    @pyqtSlot(str, str)
    def report(self, kind: str, payload: str) -> None:
        self.reported.emit(kind, _load(payload) or {})

    @pyqtSlot()
    def requestClose(self) -> None:
        self.close_requested.emit()


class ViewPage(QWebEnginePage):
    """The page, with its console pointed at Saturn's log instead of nowhere."""

    console_line = pyqtSignal(str)

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 (Qt)
        try:
            name = {0: "info", 1: "warn", 2: "error"}.get(int(level.value), "log")
        except Exception:  # noqa: BLE001
            name = "log"
        where = Path(str(source)).name if source else "page"
        self.console_line.emit(f"{name} {where}:{line} {message}")


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #


class SaturnViewWindow(QWidget):
    """One globe, owned by Saturn.

    The window is deliberately thin. Every instrument the operator sees -- the
    rail, the age of each eye, the attribution -- is drawn by the engine, in the
    engine's own type. Duplicating any of it in Qt would make the globe a widget
    inside a product instead of the product itself.
    """

    _instance: Optional["SaturnViewWindow"] = None

    ready = pyqtSignal(dict)
    failed = pyqtSignal(str)
    view_changed = pyqtSignal(dict)
    layer_changed = pyqtSignal(str, dict)
    closed = pyqtSignal()
    logged = pyqtSignal(str)

    _sync_request = pyqtSignal(str, object)
    # One of the window's own methods, asked for from a thread that is not
    # allowed to run it. Qt queues the signal to the GUI thread, which is the
    # only thread on which a widget may be shown, a page loaded or a pixel
    # grabbed. Saturn's tool calls arrive on the asyncio thread, so every
    # public method that touches the window goes through this.
    _ui_call = pyqtSignal(str, object)

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def get_instance(cls, parent=None) -> "SaturnViewWindow":
        if cls._instance is None:
            if threading.current_thread() is not threading.main_thread():
                raise RuntimeError(
                    "SaturnViewWindow must be created on the GUI thread."
                )
            cls._instance = SaturnViewWindow(parent)
        elif parent is not None and cls._instance.parent() is not parent:
            try:
                cls._instance.setParent(parent)
            except Exception:  # noqa: BLE001
                pass
        return cls._instance

    def __init__(self, parent=None):
        super().__init__(parent)
        SaturnViewWindow._instance = self

        self._dist = str(engine_server.resolve_dist() or "")
        self._engine = EngineServer(self._dist or None)
        self._bridge = ViewBridge()
        self._page: Optional[ViewPage] = None
        self._view: Optional[QWebEngineView] = None
        self._channel: Optional[QWebChannel] = None

        self._page_ready = False
        self._engine_ready = False
        self._pending: list[str] = []
        self._announced: dict[str, Any] = {}
        self._last_view: dict[str, Any] = {}
        self._ready_polls = 0
        self._fullscreen = False

        self._sync_request.connect(self._run_sync)
        self._ui_call.connect(self._run_ui_call)
        self._bridge.announced.connect(self._on_announced)
        self._bridge.reported.connect(self._on_reported)
        self._bridge.close_requested.connect(self._close_requested)
        self._bridge.console_line.connect(self.logged)

        self.setObjectName("SaturnViewWindow")
        self.setWindowTitle("Saturn's View")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#SaturnViewWindow { background: #04060A; }")
        self.setMinimumSize(880, 560)
        self.resize(1440, 900)

        icon = BASE_DIR / "assets" / "Saturn_Logo.png"
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._build_view(layout)

        for keys, slot in (
            (QKeySequence("F11"), self._toggle_fullscreen),
            (QKeySequence("Esc"), self._escape),
        ):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)

    def _build_view(self, layout: QVBoxLayout) -> None:
        try:
            view = QWebEngineView(self)
            page = ViewPage(view)
            page.console_line.connect(self.logged)
            view.setPage(page)

            settings = page.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False
            )
            # The globe is a still image until the first layer answers; there is
            # nothing to play, so no gesture should be required to start it.
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False
            )

            channel = QWebChannel(page)
            channel.registerObject("saturnView", self._bridge)
            page.setWebChannel(channel)

            self._inject_host(page)
            page.setBackgroundColor(QColor("#04060A"))
            page.loadFinished.connect(self._on_load_finished)

            layout.addWidget(view)
            self._view, self._page, self._channel = view, page, channel
        except Exception as exc:  # noqa: BLE001
            logger.exception("web view unavailable")
            self._fail(f"Renderer unavailable: {exc}")

    def _inject_host(self, page: QWebEnginePage) -> None:
        """Put the channel shim and the host script ahead of the page's own JS."""
        scripts = page.scripts()

        shim = QFile(_QWEBCHANNEL_RESOURCE)
        if not shim.open(QIODevice.OpenModeFlag.ReadOnly):
            # Without the shim there is no push channel. The window still works
            # -- commands and queries are one-way JS calls -- but the page could
            # not tell Python anything, so say so rather than fail silently.
            logger.warning("qwebchannel.js unavailable; running one-way")
        else:
            source = bytes(shim.readAll()).decode("utf-8", "replace")
            shim.close()
            bootstrap = QWebEngineScript()
            bootstrap.setName("saturn-view:qwebchannel")
            bootstrap.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            bootstrap.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            bootstrap.setRunsOnSubFrames(False)
            bootstrap.setSourceCode(source)
            scripts.insert(bootstrap)

        host = QWebEngineScript()
        host.setName("saturn-view:host")
        host.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        host.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        host.setRunsOnSubFrames(False)
        host.setSourceCode(HOST_SCRIPT)
        scripts.insert(host)

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self._fail("The engine failed to load.")
            return
        self._page_ready = True
        for command in self._pending:
            self._page.runJavaScript(command)
        self._pending.clear()
        self._poll_ready()

    def _poll_ready(self) -> None:
        """Wait for the engine to announce itself, then say so once."""
        if self._page is None or self._engine_ready:
            return
        self._ready_polls += 1

        def got(value):
            if value is True:
                self._engine_ready = True
                # Reading the full state *here* would deadlock: Qt does not
                # deliver a runJavaScript result re-entrantly from inside
                # another result's callback. Hand it back to the event loop.
                QTimer.singleShot(0, self._emit_ready)
            elif self._ready_polls < 120:
                QTimer.singleShot(250, self._poll_ready)
            else:
                self._fail("The engine did not start.")

        self._page.runJavaScript("window.__saturnViewReady === true", got)

    def _emit_ready(self) -> None:
        state = self.state() or {}
        if not state.get("ready"):
            # The page announced itself through the channel; the state read is
            # a convenience, not the proof. Fall back to the announcement.
            state = dict(self._announced, ready=True)
        self.ready.emit(state)
        self.logged.emit("saturn-view: engine ready")

    def _fail(self, message: str) -> None:
        logger.error("%s", message)
        self.logged.emit(f"saturn-view: {message}")
        self.failed.emit(message)
        if self._page is not None:
            # An honest failure the operator can read, not a blank black window.
            try:
                self._page.setHtml(
                    "<body style='margin:0;background:#04060A;color:#C9D2DE;"
                    "font:13px/1.6 Consolas,monospace;display:flex;align-items:center;"
                    "justify-content:center;height:100vh'>"
                    f"<div><b style='color:#E8B84B'>SATURN'S VIEW</b><br>{message}</div>"
                    "</body>"
                )
            except Exception:  # noqa: BLE001
                pass

    # -- page -> Python ----------------------------------------------------

    def _on_announced(self, payload: dict) -> None:
        self._announced = payload or {}
        self._last_view = payload.get("view") or {}

    def _on_reported(self, kind: str, payload: dict) -> None:
        if kind == "view":
            self._last_view = payload
            self.view_changed.emit(payload)
            return
        if kind == "layer:failed":
            self.logged.emit(
                f"saturn-view: {payload.get('id', 'layer')} failed -- {payload.get('error', 'no reason given')}"
            )
        self.layer_changed.emit(kind, payload)

    # -- JS calls ----------------------------------------------------------

    def _evaluate(self, expression: str) -> None:
        """Fire and forget, queued until the page is ready.

        Queued twice, in fact: once here if the caller is not on the GUI page,
        and once by the window itself if the document has not finished loading.
        Both queues exist so an instruction issued early is not silently lost.
        """
        self._on_gui_thread("evaluate", expression, wait=False)

    def _do_evaluate(self, expression: str) -> None:
        if self._page is None:
            return
        if not self._page_ready:
            self._pending.append(expression)
            return
        self._page.runJavaScript(expression)

    def _run_sync(self, expression: str, token: object) -> None:
        box, done = token  # type: ignore[misc]

        def got(value):
            if isinstance(box, dict):
                box["value"] = value
            done.set()

        if self._page is None:
            done.set()
            return
        self._page.runJavaScript(expression, got)

    # -- the GUI thread, reached from anywhere ----------------------------- #

    def _run_ui_call(self, name: str, token: object) -> None:
        """The receiving end of :meth:`_on_gui_thread`, always on the GUI thread.

        Nothing in here may raise. On Windows an exception that escapes a Qt
        slot does not unwind into Python, it takes the process down with exit
        code 0xC0000409, and a globe that kills Saturn because a tool asked it
        to look at somewhere is not a defensible trade. Whatever happens, the
        waiter is released: a thread left waiting on a dead slot is a hang,
        which is worse than a wrong answer.
        """
        args, kwargs, box, done = token  # type: ignore[misc]
        try:
            value = self._dispatch(name, args, kwargs)
            if isinstance(box, dict):
                box["value"] = value
        except Exception as exc:  # noqa: BLE001 -- reported, never propagated
            logger.exception("GUI call %s failed", name)
            if isinstance(box, dict):
                box["error"] = str(exc)
        finally:
            if done is not None:
                done.set()

    def _dispatch(self, name: str, args: tuple, kwargs: dict) -> Any:
        handler = getattr(self, "_do_" + name, None)
        if handler is None:
            raise AttributeError(f"no such GUI-thread call: {name}")
        return handler(*args, **kwargs)

    def _on_gui_thread(self, name: str, *args, timeout: float = 8.0, wait: bool = True, **kwargs):
        """Run ``_do_<name>`` on the GUI thread, from whichever thread we are on.

        On the GUI thread it is a plain call -- queueing to yourself is how a
        deadlock is spelled. From anywhere else the arguments travel with the
        signal and, when an answer is wanted, this waits for it. A timeout is
        reported as no answer rather than as a failure, because the call may
        still be queued and about to happen: the same distinction the engine
        draws between a layer that did not answer and a layer that is off.
        """
        if threading.current_thread() is threading.main_thread():
            return self._dispatch(name, args, kwargs)

        box: dict[str, Any] = {}
        done = threading.Event() if wait else None
        self._ui_call.emit(name, (args, kwargs, box, done))

        if done is None:
            return None
        if not done.wait(timeout):
            logger.warning("GUI thread did not run %s within %.0fs", name, timeout)
            return box.get("value")
        if "error" in box:
            logger.warning("GUI call %s failed: %s", name, box["error"])
        return box.get("value")

    def _call(self, expression: str, timeout: float = 2.5) -> Any:
        """Run an expression and wait for its value, from any thread."""
        if self._page is None or not self._page_ready or not self._engine_ready:
            return None

        if threading.current_thread() is threading.main_thread():
            # Blocking the GUI thread needs a nested loop, because the answer
            # arrives on that same thread through the event loop.
            box: dict[str, Any] = {}
            loop = QEventLoop()

            def got(value):
                box["value"] = value
                loop.quit()

            self._page.runJavaScript(expression, got)
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            timer.start(int(timeout * 1000))
            loop.exec()
            timer.stop()
            return box.get("value")

        box = {}
        done = threading.Event()
        self._sync_request.emit(expression, (box, done))
        if not done.wait(timeout):
            logger.warning("page did not answer within %.1fs", timeout)
            return None
        return box.get("value")

    def _call_json(self, expression: str, timeout: float = 2.5) -> Any:
        raw = self._call(expression, timeout)
        if not isinstance(raw, str):
            return raw
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # -- public surface ----------------------------------------------------

    def open(self, location: Optional[str] = None, *, height: Optional[float] = None) -> dict:
        """Bring the globe up, optionally focused on somewhere.

        Starting the engine is the slow part -- an HTTP server has to bind and
        Chromium has to load a page -- so the wait on the GUI thread is longer
        here than anywhere else in this file, and it is deliberately finite: a
        tool call that comes back saying nothing happened is better than a
        conversation that never comes back.
        """
        return self._on_gui_thread(
            "open", location, height=height, timeout=25.0
        ) or {}

    def _do_open(self, location: Optional[str] = None, *, height: Optional[float] = None) -> dict:
        if not self._engine.url:
            try:
                url = self._engine.start()
            except Exception as exc:  # noqa: BLE001
                self._fail(str(exc))
                return {"error": str(exc)}

            if self._page is not None:
                self._page.load(QUrl(url))

        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()

        if location:
            QTimer.singleShot(0, lambda: self.fly_to(location, height=height))
        return {"url": self._engine.url, "location": location}

    def _escape(self) -> None:
        if self._fullscreen:
            self._toggle_fullscreen()
        else:
            self.close()

    def _close_requested(self) -> None:
        self._escape()

    def _toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        self.showFullScreen() if self._fullscreen else self.showNormal()

    def fly_to(self, place: str, *, height: Optional[float] = None) -> dict:
        """Look at a named place.

        Geocoding happens here, in Python, because it is a data question and the
        engine is not allowed to guess. The camera move is the engine's, because
        it is the only thing that may move the camera.
        """
        from actions.geospatial_globe import geocode_location

        lat, lon, label = geocode_location(place)
        altitude = float(height) if height else DEFAULT_HEIGHT
        self._evaluate(
            "if (window.SaturnView) window.SaturnView.camera.flyTo("
            + _js({"lat": lat, "lon": lon, "height": altitude, "duration": 2.2})
            + ");"
        )
        return {"location": label, "lat": lat, "lon": lon, "height": altitude}

    def home(self) -> None:
        self._evaluate("if (window.SaturnView) window.SaturnView.camera.home({ duration: 2.0 });")

    def set_layer(self, layer_id: str, on: bool = True) -> bool:
        """Switch a layer on or off, and report whether it ended up that way.

        The page can be busy -- batching 60,000 coastline vertices, encoding
        the land image -- and a call that was not answered is not a call that
        failed. The probe caught exactly that: the first switch of a session
        stalled behind the land encode, missed the timeout, and was reported
        as False while the layer was in fact on. When no answer comes back the
        layer is asked directly instead of being declared dead.
        """
        value = self._call(
            "!!(window.SaturnView && window.SaturnView.layers.toggle("
            + _js(layer_id)
            + ", "
            + ("true" if on else "false")
            + "))"
        )
        if value is not None:
            return bool(value)

        return bool(
            self._call(
                "!!(window.SaturnView && window.SaturnView.layers.isEnabled("
                + _js(layer_id)
                + "))"
            )
        )

    def refresh_layer(self, layer_id: str) -> bool:
        """Ask a layer to poll now.

        The answer is a Promise, and a Promise does not survive the trip back
        into Python, so this reports what it can honestly report -- whether the
        layer was running when asked -- and lets the layer's own events carry
        the result.
        """
        known = {layer.get("id"): layer for layer in self.layers()}
        layer = known.get(layer_id)
        if not layer or not layer.get("enabled"):
            return False
        self._evaluate(
            "if (window.SaturnView) window.SaturnView.layers.refreshNow("
            + _js(layer_id)
            + ");"
        )
        return True

    def layers(self) -> list[dict]:
        state = self.state() or {}
        return state.get("layers") or []

    def snapshot(self, layer_id: str) -> Any:
        return self._call_json(
            "JSON.stringify(window.SaturnView.layers.snapshot(" + _js(layer_id) + "))"
        )

    def state(self) -> dict:
        """Everything the engine is willing to say about itself, in one call."""
        return self._call_json(STATE_JS) or {}

    def describe(self) -> str:
        """One sentence an assistant can say out loud.

        Built from the engine's own numbers rather than from a template, so it
        cannot claim a layer is current when the rail says otherwise.
        """
        state = self.state()
        if not state or not state.get("ready"):
            return "Saturn's View is not running."

        layers = [layer for layer in state.get("layers") or [] if layer.get("enabled")]
        if not layers:
            return "Saturn's View is open, with every layer switched off."

        figures = []
        for layer in layers:
            fresh = layer.get("freshness") or {}
            reading = layer.get("readout") or ""
            age = fresh.get("age")
            figures.append(
                f"{layer.get('title', layer.get('id'))} {reading}".strip()
                + (f", {age} old" if age else "")
            )

        view = state.get("view") or {}
        where = f"{view.get('lat', 0):.1f}, {view.get('lon', 0):.1f}"
        lens = state.get("lens")
        prefix = f"{lens.capitalize()} lens. " if lens else ""
        return prefix + "; ".join(figures) + f". Camera over {where}."

    def set_lens(self, name: str) -> dict:
        """Switch what the surface is made of, and report what came back.

        The same call the rail button makes, so a spoken instruction and a
        click cannot end up in different states.
        """
        return (
            self._call_json(
                f"JSON.stringify(window.SaturnView.engine.setLens({json.dumps(name)}))"
            )
            or {}
        )

    def pause(self) -> None:
        self._evaluate("if (window.SaturnView) window.SaturnView.setPaused(true);")

    def resume(self) -> None:
        self._evaluate("if (window.SaturnView) window.SaturnView.setPaused(false);")

    def capture(self, path: str) -> bool:
        """Write what the operator can see to a file.

        A globe that cannot show you a frame is hard to trust and harder to
        explain, so this is part of the host's surface rather than a test
        convenience. Grabbing a widget is a GUI-thread act, so it goes through
        the same door everything else that touches the window does.
        """
        return bool(self._on_gui_thread("capture", path, timeout=15.0))

    def _do_capture(self, path: str) -> bool:
        if self._view is None:
            return False
        try:
            return bool(self._view.grab().save(str(path)))
        except Exception:  # noqa: BLE001
            logger.warning("capture failed for %s", path)
            return False

    def show_window(self) -> dict:
        """Put the globe in front of the operator without moving the camera."""
        return self._on_gui_thread("show_window", timeout=15.0) or {}

    def _do_show_window(self) -> dict:
        self.show()
        self.raise_()
        self.activateWindow()
        return {"visible": self.isVisible()}

    def close_window(self) -> dict:
        """Put the globe away. The engine keeps its state; nothing is torn down."""
        return self._on_gui_thread("close_window", timeout=10.0) or {}

    def _do_close_window(self) -> dict:
        self.hide()
        return {"visible": self.isVisible()}

    def shutdown(self) -> None:
        """Release the renderer and the port. Called when Saturn exits."""
        try:
            self._evaluate("if (window.SaturnView) window.SaturnView.shutdown();")
        except Exception:  # noqa: BLE001
            pass
        self._engine.stop()
        if self._view is not None:
            try:
                self._view.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        self._view, self._page, self._channel = None, None, None
        SaturnViewWindow._instance = None

    # -- Qt events ---------------------------------------------------------

    def showEvent(self, event):  # noqa: N802 (Qt)
        super().showEvent(event)
        self.resume()

    def hideEvent(self, event):  # noqa: N802 (Qt)
        super().hideEvent(event)
        # Nobody is looking. Stop polling the feeds; the globe keeps its last
        # known state and its ages, which is exactly what the operator should
        # see on return.
        self.pause()

    def closeEvent(self, event):  # noqa: N802 (Qt)
        # Closing is putting it away, not shutting Saturn down. The window hides
        # and stays warm; `shutdown()` is the real teardown.
        event.ignore()
        self.hide()
        self.closed.emit()


def window() -> Optional[SaturnViewWindow]:
    """The window if one already exists. Never creates, never blocks."""
    return SaturnViewWindow._instance


class _WindowFactory(QObject):
    """Creates the window on the GUI thread at the request of another one.

    A widget may only be constructed on the thread that owns the event loop, so
    a tool call arriving on Saturn's asyncio thread cannot build the view
    itself -- ``SaturnViewWindow.get_instance`` says so rather than corrupting
    Qt. This object is built early, on the GUI thread, so it has that thread's
    affinity; a signal emitted at it from anywhere is queued by Qt and handled
    where construction is legal.
    """

    request = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.request.connect(self._on_request)

    def _on_request(self, token: object) -> None:
        # Same rule as every other slot in this file: an exception that escapes
        # here ends the process on Windows, so nothing is allowed to escape and
        # the waiter is always released.
        parent, box, done = token  # type: ignore[misc]
        try:
            box["window"] = SaturnViewWindow.get_instance(parent=parent)
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not create Saturn's View")
            box["error"] = str(exc)
        finally:
            done.set()


_FACTORY: Optional[_WindowFactory] = None


def install_creator() -> dict:
    """Put a creator on the GUI thread so a tool can open the globe later.

    Called once during startup, next to the other windows that need it. If it
    runs on some other thread the factory would take that thread's affinity and
    every later request would be queued to a loop that never runs, so it
    declines instead of installing a deadlock.
    """
    global _FACTORY

    if threading.current_thread() is not threading.main_thread():
        logger.warning("install_creator() off the GUI thread; not installing")
        return {"installed": False, "reason": "not on the GUI thread"}
    if _FACTORY is None:
        _FACTORY = _WindowFactory()
    return {"installed": True, "created": SaturnViewWindow._instance is not None}


def get_view(parent=None, *, timeout: float = 25.0) -> Optional[SaturnViewWindow]:
    """The window, built if necessary, from whichever thread is asking.

    This is the door the assistant's map tool comes through. It returns None
    rather than raising when there is no way to build one, because a tool call
    that answers "the globe is not available" beats one that faults inside a
    voice loop.
    """
    if threading.current_thread() is threading.main_thread():
        try:
            return SaturnViewWindow.get_instance(parent=parent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not create Saturn's View: %s", exc)
            return None

    if _FACTORY is None:
        logger.warning(
            "Saturn's View has not been installed on the GUI thread "
            "(saturn_view.window.install_creator is called during startup)"
        )
        return None

    box: dict[str, Any] = {}
    done = threading.Event()
    _FACTORY.request.emit((parent, box, done))
    if not done.wait(timeout):
        logger.warning("the GUI thread did not build Saturn's View within %.0fs", timeout)
    if "error" in box:
        logger.warning("Saturn's View could not be built: %s", box["error"])
    return box.get("window")


def main() -> int:
    """Standalone: the engine, a window, nothing else."""
    from PyQt6.QtWidgets import QApplication

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv[:1])

    view = SaturnViewWindow.get_instance()
    view.open()
    app.aboutToQuit.connect(view.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
