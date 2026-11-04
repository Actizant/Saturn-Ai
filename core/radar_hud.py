"""
core/radar_hud.py
Saturn Tactical Net-Radar & Plan Position Indicator (PPI) HUD.
Provides a minimal, sleek, non-AI-slop holographic radar popup overlay that displays
live subnet sweeps, discovered LAN devices, and hardware controls in real time.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtWebChannel import QWebChannel
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    _WEBENGINE_OK = True
except Exception:
    _WEBENGINE_OK = False

from core.net_sentinel import get_net_sentinel

logger = logging.getLogger("RadarHUD")

RADAR_HTML_FILE = Path(__file__).resolve().parent.parent / "assets" / "radar" / "index.html"


class RadarBridge(QObject):
    """Bridge for bi-directional communication between JavaScript and Python."""
    close_requested = pyqtSignal()
    rescan_completed = pyqtSignal(dict)
    ping_completed = pyqtSignal(str, float)
    speed_completed = pyqtSignal(float)
    mesh_connected = pyqtSignal(dict)
    device_notified = pyqtSignal(str, dict)

    def __init__(self, parent_overlay: "RadarPopupOverlay") -> None:
        super().__init__()
        self._overlay = parent_overlay
        self._sentinel = get_net_sentinel()

        # Connect signals to overlay GUI methods with QueuedConnection for 100% thread safety
        self.rescan_completed.connect(self._overlay.update_radar_data)
        self.ping_completed.connect(self._overlay.set_latency_result)
        self.speed_completed.connect(self._overlay.update_speed)
        self.mesh_connected.connect(self._overlay.set_mesh_connected)
        self.device_notified.connect(self._overlay.set_device_notified)

    @pyqtSlot()
    def closeRadar(self) -> None:
        """Called by JS when close button or Esc is clicked."""
        self.close_requested.emit()

    @pyqtSlot()
    def triggerRescan(self) -> None:
        """Runs a background subnet sweep and streams new devices to the radar scope."""
        threading.Thread(target=self._run_rescan_worker, daemon=True).start()

    def _run_rescan_worker(self) -> None:
        try:
            res = self._sentinel.scan_local_network(deep=False)
            self.rescan_completed.emit(res)
        except Exception as e:
            logger.error(f"Rescan failed: {e}")

    @pyqtSlot(str)
    def pingDevice(self, ip: str) -> None:
        """Measures live round-trip latency to the target device."""
        threading.Thread(target=self._run_ping_worker, args=(ip,), daemon=True).start()

    def _run_ping_worker(self, ip: str) -> None:
        try:
            res = self._sentinel.ping_target(ip, count=2)
            lat = res.get("avg_latency_ms", 1.0)
            self.ping_completed.emit(ip, lat)
        except Exception as e:
            logger.error(f"Ping failed: {e}")

    @pyqtSlot(str)
    def wakeDevice(self, mac: str) -> None:
        """Broadcasts Wake-on-LAN magic packet."""
        try:
            self._sentinel.wake_on_lan(mac)
        except Exception as e:
            logger.error(f"Wake-on-LAN failed: {e}")

    @pyqtSlot()
    def inspectRouter(self) -> None:
        """Probes gateway router and updates UI."""
        threading.Thread(target=self._run_router_worker, daemon=True).start()

    def _run_router_worker(self) -> None:
        try:
            res = self._sentinel.inspect_router()
            logger.info(f"Router inspected from Radar: {res.get('vendor')}")
        except Exception as e:
            logger.error(f"Router inspection failed: {e}")

    @pyqtSlot(str)
    def openUrl(self, url: str) -> None:
        """Opens router web GUI in the default browser."""
        try:
            webbrowser.open(url)
        except Exception as e:
            logger.error(f"Failed to open URL: {e}")

    @pyqtSlot()
    def testSpeed(self) -> None:
        """Measures live download speed via Cloudflare CDN benchmark."""
        threading.Thread(target=self._run_speed_worker, daemon=True).start()

    def _run_speed_worker(self) -> None:
        try:
            res = self._sentinel.measure_network_speed()
            mbps = res.get("download_speed_mbps", 0.0)
            self.speed_completed.emit(mbps)
        except Exception as e:
            logger.error(f"Speed test error: {e}")

    @pyqtSlot()
    def connectAllDevices(self) -> None:
        """Connects to all devices on local network and broadcasts Saturn Online beacon."""
        threading.Thread(target=self._run_connect_all_worker, daemon=True).start()

    def _run_connect_all_worker(self) -> None:
        try:
            res = self._sentinel.connect_all_devices()
            self.mesh_connected.emit(res)
        except Exception as e:
            logger.error(f"Connect all devices failed: {e}")

    @pyqtSlot(str)
    def notifyDevice(self, ip: str) -> None:
        """Sends targeted notification packet to a specific device."""
        threading.Thread(target=self._run_notify_device_worker, args=(ip,), daemon=True).start()

    def _run_notify_device_worker(self, ip: str) -> None:
        try:
            res = self._sentinel.notify_single_device(ip)
            self.device_notified.emit(ip, res)
        except Exception as e:
            logger.error(f"Notify device failed: {e}")


class RadarPopupOverlay(QWidget):
    """
    Sleek, minimalist tactical radar overlay that floats directly inside
    the Saturn interface or as a standalone dialog.
    """
    closed = pyqtSignal()

    def __init__(self, radar_data: Optional[Dict[str, Any]] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("RadarPopupOverlay")

        # Dynamic size clamped to parent geometry
        p_w = parent.width() if parent else 980
        p_h = parent.height() if parent else 700
        w = min(max(820, p_w - 40), 920)
        h = min(max(500, p_h - 40), 550)
        self.setFixedSize(w, h)

        self._radar_data = radar_data or get_net_sentinel().last_scan_result or {}

        # Frame with acrylic / cyan neon styling
        self._frame = QFrame(self)
        self._frame.setObjectName("RadarFrame")
        self._frame.setFixedSize(self.size())
        self._frame.setStyleSheet("""
            QFrame#RadarFrame {
                background: rgba(5, 8, 17, 0.98);
                border: 2px solid rgba(0, 229, 255, 0.45);
                border-radius: 16px;
            }
        """)

        layout = QVBoxLayout(self._frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Setup WebEngine & WebChannel
        if _WEBENGINE_OK:
            self._web = QWebEngineView(self._frame)
            self._web.setStyleSheet("background: transparent;")
            self._web.page().setBackgroundColor(QColor("#050811"))

            self._bridge = RadarBridge(self)
            self._bridge.close_requested.connect(self._do_close)

            self._channel = QWebChannel(self._web.page())
            self._channel.registerObject("qtBridge", self._bridge)
            self._web.page().setWebChannel(self._channel)

            settings = self._web.settings()
            try:
                settings.setAttribute(settings.WebAttribute.Accelerated2dCanvasEnabled, True)
                settings.setAttribute(settings.WebAttribute.JavascriptEnabled, True)
                settings.setAttribute(settings.WebAttribute.LocalContentCanAccessFileUrls, True)
            except Exception:
                pass

            self._web.loadFinished.connect(self._on_page_loaded)
            self._web.load(QUrl.fromLocalFile(str(RADAR_HTML_FILE)))
            layout.addWidget(self._web, 1)
        else:
            fallback = QLabel(
                "<h3 style='color:#00e5ff; margin:20px;'>Radar HUD (WebEngine Unavailable)</h3>",
                self._frame
            )
            layout.addWidget(fallback, 1)

        # Close button in top-right corner
        self._close_btn = QPushButton("✕", self._frame)
        self._close_btn.setFixedSize(28, 28)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.05);
                color: #94a3b8;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 14px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: rgba(239, 68, 68, 0.25);
                border-color: #ef4444;
                color: #ffffff;
            }
        """)
        self._close_btn.move(self.width() - 40, 14)
        self._close_btn.clicked.connect(self._do_close)

        QShortcut(QKeySequence("Escape"), self, self._do_close)

    def _on_page_loaded(self, ok: bool) -> None:
        if ok and self._radar_data:
            self.update_radar_data(self._radar_data)
        elif ok and not self._radar_data:
            # Trigger immediate fast sweep on first open if no previous scan
            self._web.page().runJavaScript("setScanningState(true);")
            self._bridge.triggerRescan()

    def _run_js(self, script: str) -> None:
        """Safely executes JavaScript string if WebEngine page is valid."""
        try:
            if hasattr(self, "_web") and self._web:
                page = getattr(self._web, "page", lambda: None)()
                if page:
                    page.runJavaScript(script)
        except Exception as exc:
            logger.debug(f"Radar HUD runJavaScript error: {exc}")

    def update_radar_data(self, data: Dict[str, Any]) -> None:
        """Pushes new subnet scan telemetry into the JavaScript radar canvas."""
        self._radar_data = data
        clean_json = json.dumps(data)
        self._run_js(f"updateRadarData({clean_json});")

    def set_latency_result(self, ip: str, latency_ms: float) -> None:
        """Updates real-time ping readout for a target."""
        self._run_js(
            f"if (selectedDevice && selectedDevice.ip === '{ip}') {{ "
            f"document.getElementById('insLatency').innerText = '{latency_ms:.1f} ms'; }}"
        )

    def update_speed(self, mbps: float) -> None:
        """Updates internet download speed readout on the radar UI."""
        self._run_js(f"updateSpeedReadout({mbps});")

    def set_mesh_connected(self, res: dict) -> None:
        """Pushes mesh connection confirmation to JS."""
        clean_json = json.dumps(res)
        self._run_js(f"if (window.onMeshConnected) onMeshConnected({clean_json});")

    def set_device_notified(self, ip: str, res: dict) -> None:
        """Pushes single device notification confirmation to JS."""
        clean_json = json.dumps(res)
        self._run_js(f"if (window.onDeviceNotified) onDeviceNotified('{ip}', {clean_json});")

    def _do_close(self) -> None:
        self.closed.emit()
        self.hide()
        self.deleteLater()


def show_radar_hud(radar_data: Optional[Dict[str, Any]] = None, parent: Optional[QWidget] = None) -> Optional[QWidget]:
    """
    Displays the Saturn Net-Radar popup overlay inside the active Saturn window,
    or opens a standalone frameless modal dialog if no main window exists.
    """
    app = QApplication.instance()
    main_win = parent

    if not main_win and app:
        for w in app.topLevelWidgets():
            if hasattr(w, "_apply_radar_overlay"):
                main_win = w
                break

    if main_win and hasattr(main_win, "_apply_radar_overlay"):
        main_win._apply_radar_overlay(radar_data or {})
        return getattr(main_win, "_radar_overlay", None)

    # Standalone modal dialog fallback
    dialog = QDialog(parent)
    dialog.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.SubWindow)
    dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    lay = QVBoxLayout(dialog)
    lay.setContentsMargins(0, 0, 0, 0)
    overlay = RadarPopupOverlay(radar_data, parent=dialog)
    dialog.setFixedSize(overlay.size())
    overlay.closed.connect(dialog.close)
    lay.addWidget(overlay)
    dialog.show()
    dialog.raise_()
    return overlay
