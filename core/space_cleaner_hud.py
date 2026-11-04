"""
core/space_cleaner_hud.py
=========================
Saturn - Storage Sentinel PySide6 QWebEngine HUD Window.

Provides a frameless, glassmorphic HUD displaying the interactive
Concentric Sunburst Disk Map, live telemetry, and safe purging controls.
Equipped with cross-platform WebEngine safety flags.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Configure cross-platform Chromium flags before WebEngine imports
CHROMIUM_FLAGS = [
    "--enable-gpu-rasterization",
    "--enable-zero-copy",
    "--ignore-gpu-blocklist",
    "--enable-webgl",
]
if sys.platform == "win32":
    CHROMIUM_FLAGS.append("--use-angle=d3d11")
elif sys.platform.startswith("linux"):
    CHROMIUM_FLAGS.extend(["--no-sandbox", "--disable-dev-shm-usage"])

os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(CHROMIUM_FLAGS)

try:
    from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal as Signal, pyqtSlot as Slot
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import (
        QApplication,
        QDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QLabel,
        QVBoxLayout,
        QWidget,
    )
    try:
        from PyQt6.QtWebChannel import QWebChannel
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        _WEBENGINE_OK = True
    except Exception:
        QWebEngineView = None
        _WEBENGINE_OK = False
except ImportError:
    from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QLabel,
        QVBoxLayout,
        QWidget,
    )
    try:
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineWidgets import QWebEngineView
        _WEBENGINE_OK = True
    except Exception:
        QWebEngineView = None
        _WEBENGINE_OK = False

from core.space_cleaner import SpaceCleanerEngine, get_space_cleaner

logger = logging.getLogger("SpaceCleanerHUD")
logger.setLevel(logging.INFO)

HUD_HTML_FILE = Path(__file__).resolve().parent.parent / "assets" / "space_cleaner" / "index.html"


class SpaceCleanerBridge(QObject):
    """Bridge communicating between JavaScript Sunburst HUD and Python SpaceCleanerEngine."""

    close_requested = Signal()
    scan_completed = Signal(str)      # JSON payload
    purge_completed = Signal(str)     # JSON payload
    scan_progress = Signal(str, int)  # status_msg, percent

    def __init__(self, parent_widget: Optional[QWidget] = None):
        super().__init__(parent_widget)
        self._widget = parent_widget
        self._cleaner = get_space_cleaner()

    @Slot()
    def triggerScan(self) -> None:
        """Triggers a live multi-threaded storage scan in a background thread."""
        logger.info("Storage Sentinel scan requested from UI bridge.")

        def _worker():
            def _on_prog(msg: str, pct: int):
                self.scan_progress.emit(msg, pct)

            report = self._cleaner.scan_storage(progress_cb=_on_prog)
            report_dict = report.to_dict()
            json_str = json.dumps(report_dict)
            self.scan_completed.emit(json_str)

        import threading
        threading.Thread(target=_worker, daemon=True, name="storage-scan-worker").start()

    @Slot(str)
    def switchDrive(self, drive_path: str) -> None:
        """Switches active drive and triggers background scan on selected drive."""
        logger.info(f"Storage Sentinel drive switched to: {drive_path}")

        def _worker():
            def _on_prog(msg: str, pct: int):
                self.scan_progress.emit(msg, pct)

            report = self._cleaner.scan_storage(drive_path=drive_path, progress_cb=_on_prog)
            report_dict = report.to_dict()
            json_str = json.dumps(report_dict)
            self.scan_completed.emit(json_str)

        import threading
        threading.Thread(target=_worker, daemon=True, name="storage-scan-worker").start()

    @Slot()
    def openStorageSense(self) -> None:
        """Opens native Windows Storage Sense settings page."""
        logger.info("Opening native Windows Storage Sense settings.")
        self._cleaner.open_windows_storage_sense()

    @Slot(list, str)
    def purgeCategoriesOnDrive(self, category_ids: List[str], drive_path: str) -> None:
        """Purges checked categories safely on the designated drive."""
        logger.info(f"Purge requested for categories on {drive_path}: {category_ids}")

        def _worker():
            res = self._cleaner.purge_selected(category_ids, drive_path=drive_path)
            json_str = json.dumps(res)
            self.purge_completed.emit(json_str)

        import threading
        threading.Thread(target=_worker, daemon=True, name="storage-purge-worker").start()

    @Slot(list)
    def purgeCategories(self, category_ids: List[str]) -> None:
        """Purges checked categories safely on active drive."""
        self.purgeCategoriesOnDrive(category_ids, "")

    @Slot()
    def closeWindow(self) -> None:
        """Closes the Storage Sentinel HUD window."""
        self.close_requested.emit()


class SpaceCleanerPopupOverlay(QWidget):
    """
    Sleek, glassmorphic Storage Sentinel HUD overlay that mounts
    inside the Saturn central interface or as a standalone dialog.
    """
    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("SpaceCleanerPopupOverlay")

        # Dynamic size clamped to parent geometry
        p_w = parent.width() if parent else 1020
        p_h = parent.height() if parent else 700
        w = min(max(860, p_w - 40), 1020)
        h = min(max(540, p_h - 40), 660)
        self.setFixedSize(w, h)

        self._frame = QFrame(self)
        self._frame.setObjectName("SpaceCleanerFrame")
        self._frame.setFixedSize(self.size())
        self._frame.setStyleSheet("""
            QFrame#SpaceCleanerFrame {
                background: rgba(7, 9, 14, 0.98);
                border: 2px solid rgba(0, 243, 255, 0.45);
                border-radius: 16px;
            }
        """)

        layout = QVBoxLayout(self._frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if _WEBENGINE_OK and QWebEngineView is not None:
            self.web_view = QWebEngineView(self._frame)
            self.web_view.setStyleSheet("background: transparent;")
            self.web_view.page().setBackgroundColor(QColor("#07090e"))

            self.bridge = SpaceCleanerBridge(self)
            self.bridge.close_requested.connect(self._do_close)
            self.bridge.scan_progress.connect(self.set_scan_status)
            self.bridge.scan_completed.connect(self.notify_scan_finished)
            self.bridge.purge_completed.connect(self.notify_purge_finished)

            self.channel = QWebChannel(self.web_view.page())
            self.channel.registerObject("spaceCleanerBridge", self.bridge)
            self.web_view.page().setWebChannel(self.channel)

            settings = self.web_view.settings()
            try:
                settings.setAttribute(settings.WebAttribute.Accelerated2dCanvasEnabled, True)
                settings.setAttribute(settings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
                settings.setAttribute(settings.WebAttribute.LocalContentCanAccessFileUrls, True)
            except Exception:
                pass

            if HUD_HTML_FILE.exists():
                self.web_view.load(QUrl.fromLocalFile(str(HUD_HTML_FILE)))
            else:
                self.web_view.setHtml("<h2 style='color:red;'>Storage Sentinel UI template not found.</h2>")

            layout.addWidget(self.web_view)
        else:
            fallback = QLabel("PySide6 WebEngine is not available on this platform.", self._frame)
            fallback.setStyleSheet("color: #00f3ff; font-family: monospace; font-size: 14px; padding: 24px;")
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(fallback)

    def set_scan_status(self, msg: str, percent: int) -> None:
        """Pushes scan status ticker update to JS."""
        if hasattr(self, "web_view") and self.web_view:
            safe_msg = msg.replace("'", "\\'")
            js = f"if (window.setStatus) window.setStatus('{safe_msg}', true);"
            self.web_view.page().runJavaScript(js)

    def notify_scan_finished(self, json_payload: str) -> None:
        """Invokes updateScanReport(data) in web view."""
        if hasattr(self, "web_view") and self.web_view:
            encoded = json.dumps(json_payload)
            js = f"if (window.updateScanReport) window.updateScanReport(JSON.parse({encoded}));"
            self.web_view.page().runJavaScript(js)

    def notify_purge_finished(self, json_payload: str) -> None:
        """Invokes onPurgeCompleted(data) in web view."""
        if hasattr(self, "web_view") and self.web_view:
            encoded = json.dumps(json_payload)
            js = f"if (window.onPurgeCompleted) window.onPurgeCompleted(JSON.parse({encoded}));"
            self.web_view.page().runJavaScript(js)

    def _do_close(self) -> None:
        self.closed.emit()
        self.hide()
        self.deleteLater()


class SpaceCleanerWindow(QWidget):
    """Frameless translucent desktop HUD window for Storage Sentinel."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(1020, 680)

        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            self.move(
                geom.x() + (geom.width() - self.width()) // 2,
                geom.y() + (geom.height() - self.height()) // 2
            )

        self._drag_pos = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setColor(QColor(0, 243, 255, 60))
        shadow.setOffset(0, 10)
        self.setGraphicsEffect(shadow)

        self.overlay = SpaceCleanerPopupOverlay(self)
        self.overlay.closed.connect(self.close)
        layout.addWidget(self.overlay)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


_GLOBAL_HUD_WINDOW: Optional[QWidget] = None


def show_space_cleaner_hud(parent: Optional[QWidget] = None) -> Optional[QWidget]:
    """
    Displays the Storage Sentinel HUD inside the active Saturn window,
    or opens a standalone frameless HUD dialog if no main window is available.
    """
    global _GLOBAL_HUD_WINDOW

    app = QApplication.instance()
    main_win = parent

    if not main_win and app:
        for w in app.topLevelWidgets():
            if hasattr(w, "_apply_space_cleaner_overlay"):
                main_win = w
                break

    if main_win and hasattr(main_win, "_apply_space_cleaner_overlay"):
        main_win._apply_space_cleaner_overlay({})
        return getattr(main_win, "_space_cleaner_overlay", None)

    # Standalone modal dialog fallback
    if _GLOBAL_HUD_WINDOW is None or not _GLOBAL_HUD_WINDOW.isVisible():
        _GLOBAL_HUD_WINDOW = SpaceCleanerWindow(parent=parent)
        _GLOBAL_HUD_WINDOW.show()
    else:
        _GLOBAL_HUD_WINDOW.raise_()
        _GLOBAL_HUD_WINDOW.activateWindow()

    return _GLOBAL_HUD_WINDOW
