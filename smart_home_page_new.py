from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QEasingCurve, QDateTime, QPropertyAnimation, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap, QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
    QGraphicsDropShadowEffect,
)

from smart_home import SmartHomeService


# ── this page's place in the design system ───────────────────────────────────
#
# The module used to carry its own frozen palette - a cyan accent, white ink
# and about thirty hand-written stylesheets that no theme change could reach -
# so it stayed blue on a gold brand while the rest of the shell recoloured.
# Every colour, radius and font below now comes from the Aurum tokens the shell
# uses. Those live on class C in ui.py, and ui.py imports this module while it
# is still executing, so C cannot be imported here at module scope without a
# cycle; it is resolved per call instead. That is also what lets a theme change
# reach this page: nothing is captured at import time any more.


def _aurum():
    """The shell's live tokens and QSS builders, as ``(C, S)``.

    Call sites do ``C, S = _aurum()`` at the top of the method that styles
    something, then read ``C.ACC`` / ``S.ghost()`` the way ui.py does.
    """
    import ui
    return ui.C, ui.S


@dataclass(frozen=True)
class PlatformRow:
    key: str
    name: str
    available: bool
    auth_fields: list[tuple[str, str, str, bool]]


class ClickableFrame(QFrame):
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


def _panel_style(radius: int | None = None, selector: str = "QFrame") -> str:
    """A glass card, on the shell's spec rather than this page's own.

    The selector is a parameter because a bare ``QFrame`` rule set on a widget
    also reaches every QFrame inside it, which the tiles and the drawer need
    to keep for their own children.
    """
    _C, S = _aurum()
    return f"{selector} {{ {S.glass(radius)} }}"


def _soft_button_style() -> str:
    """The outlined pill. Its previous string had a stray ``}`` that made the
    whole sheet unparseable to Qt, so the buttons it was given to painted as
    plain system grey."""
    _C, S = _aurum()
    return S.ghost()


def _link_style() -> str:
    """A bare text action - 'View All Activity' and friends."""
    C, _S = _aurum()
    return (
        f"QPushButton {{ background: transparent; color: {C.ACC}; border: none; "
        f"font-family: '{C.F_BODY}'; font-size: 12px; font-weight: 600; "
        f"text-align: left; padding-left: 4px; }} "
        f"QPushButton:hover {{ color: {C.ACC2}; }}"
    )


def _pick_card_style(selected: bool, selector: str = "QFrame") -> str:
    """A selectable card in a list - platform picker and device tile alike.

    Both used to carry their own copy of the same four declarations, in cyan,
    which is how a single screen ended up with three different hover colours.
    """
    C, S = _aurum()
    if selected:
        return f"{selector} {{ {S.glass()} border: 1px solid {C.ACC}; }}"
    return (
        f"{selector} {{ {S.glass()} }} "
        f"{selector}:hover {{ border: 1px solid {C.BORDER_A}; }}"
    )


def _badge_style(radius: int) -> str:
    """The round glyph badge beside a platform or activity row.

    Each platform used to be painted in its own brand colour, so the picker
    looked like eight unrelated apps rather than one screen; the design tints
    every badge with the accent and lets the glyph carry the identity.
    """
    C, _S = _aurum()
    return (
        f"background: {C.PRI_GHO}; color: {C.TEXT}; "
        f"border: 1px solid {C.BORDER_B}; border-radius: {radius}px;"
    )


class AddDeviceDialog(QDialog):
    def __init__(self, service: SmartHomeService, parent=None):
        C, S = _aurum()
        super().__init__(parent)
        self._service = service
        self._provider_key: str | None = None
        self._provider_label = ""
        self._preview: dict[str, Any] = {}
        self._result: dict[str, Any] = {}
        self._fields: dict[str, QLineEdit] = {}
        self._checks: list[tuple[str, QCheckBox]] = []
        self._platform_buttons: list[ClickableFrame] = []

        self.setWindowTitle("Add Device")
        self.setModal(True)
        self.setMinimumSize(780, 620)
        self.setObjectName("AddDeviceWizard")
        # The wizard sheet is a settings screen, so it wears the same sheet the
        # System & Connect pages do - fields, combos, checks and scrollbars all
        # come from one place instead of being re-invented per widget. The
        # object name is set first: Qt matches the selector when the sheet is
        # polished, so a name applied afterwards would not be seen.
        self.setStyleSheet(S.settings_sheet("QDialog#AddDeviceWizard"))

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("ADD DEVICE")
        title.setFont(QFont(C.F_DISPLAY, 15, QFont.Weight.Bold))
        title.setStyleSheet(S.display(22, 600, 2))
        sub = QLabel("Choose a platform, authenticate, scan, and save your devices locally.")
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        root.addWidget(title)
        root.addWidget(sub)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_platform_page())
        self._stack.addWidget(self._build_auth_page())
        self._stack.addWidget(self._build_scan_page())
        self._stack.addWidget(self._build_finish_page())
        root.addWidget(self._stack, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self._back_btn = QPushButton("Back")
        self._next_btn = QPushButton("Next")
        self._cancel_btn = QPushButton("Cancel")
        for btn in (self._back_btn, self._next_btn, self._cancel_btn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_soft_button_style())
        # The wizard's forward action is the one thing on the sheet that should
        # read as primary, so it wears the gold fill the rest of the shell uses.
        self._next_btn.setStyleSheet(S.primary())
        self._back_btn.clicked.connect(self._go_back)
        self._next_btn.clicked.connect(self._go_next)
        self._cancel_btn.clicked.connect(self.reject)
        row.addWidget(self._back_btn)
        row.addWidget(self._next_btn)
        row.addWidget(self._cancel_btn)
        root.addLayout(row)
        self._sync_buttons()

    def result_payload(self) -> dict[str, Any]:
        return dict(self._result)

    def _card(self, title: str) -> QFrame:
        C, _S = _aurum()
        frame = QFrame()
        frame.setStyleSheet(_panel_style(22))
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lbl = QLabel(title)
        lbl.setFont(QFont(C.F_DISPLAY, 11, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(lbl)
        return frame

    def _brand_glyph(self, key: str) -> str:
        return {
            "atomberg": "🌀",
            "kasa": "🔌",
            "hue": "💡",
            "lg": "📺",
            "daikin": "❄️",
            "tuya": "⚙️",
            "nest": "🏠",
            "smartthings": "📡"
        }.get(key, "⚙️")

    def _build_platform_page(self) -> QWidget:
        C, _S = _aurum()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(self._card("Step 1: Choose Platform"))

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        
        for idx, platform in enumerate(self._platforms()):
            card = ClickableFrame()
            card.setObjectName(f"PlatformCard_{platform.key}")
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            
            lay_c = QHBoxLayout(card)
            lay_c.setContentsMargins(16, 14, 16, 14)
            lay_c.setSpacing(14)
            
            badge = QLabel(self._brand_glyph(platform.key))
            badge.setFixedSize(36, 36)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFont(QFont(C.F_BODY, 13))
            badge.setStyleSheet(_badge_style(18))
            lay_c.addWidget(badge)
            
            text_lay = QVBoxLayout()
            text_lay.setSpacing(2)
            
            p_name = QLabel(platform.name)
            p_name.setFont(QFont(C.F_BODY, 10, QFont.Weight.Bold))
            p_name.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
            
            subtitle = {
                "atomberg": "Ceiling fans & smart air flow",
                "kasa": "Plugs, switches, & smart sockets",
                "hue": "Smart lights, strips, & hue sync",
                "lg": "LG webOS smart TVs & apps",
                "daikin": "Split ACs & climate controls",
                "tuya": "IoT switches & generic appliances",
                "nest": "Thermostats & home security",
                "smartthings": "Samsung hub & IoT integrations",
            }.get(platform.key, "Smart home cloud connection")
            p_sub = QLabel(subtitle)
            p_sub.setFont(QFont(C.F_BODY, 8))
            p_sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            
            text_lay.addWidget(p_name)
            text_lay.addWidget(p_sub)
            lay_c.addLayout(text_lay, 1)
            
            card.setMinimumHeight(64)
            card.setStyleSheet(_pick_card_style(False))
            
            # Setup lambda for click
            card.clicked.connect(lambda key=platform.key, label=platform.name: self._choose_provider(key, label))
            self._platform_buttons.append(card)
            grid.addWidget(card, idx // 2, idx % 2)
            
        lay.addWidget(grid_host)
        return page

    def _build_auth_page(self) -> QWidget:
        C, _S = _aurum()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(self._card("Step 2: Authenticate"))
        self._auth_desc = QLabel("Select a platform to build the right authentication form.")
        self._auth_desc.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(self._auth_desc)
        self._auth_form_host = QFrame()
        self._auth_form_host.setStyleSheet("background: transparent;")
        self._auth_form = QFormLayout(self._auth_form_host)
        self._auth_form.setContentsMargins(12, 0, 12, 12)
        self._auth_form.setVerticalSpacing(10)
        lay.addWidget(self._auth_form_host)
        return page

    def _build_scan_page(self) -> QWidget:
        C, _S = _aurum()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(self._card("Step 3: Scan"))
        self._scan_label = QLabel("Searching for devices...")
        self._scan_label.setFont(QFont(C.F_BODY, 12, QFont.Weight.Bold))
        self._scan_label.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(self._scan_label)
        self._scan_bar = QProgressBar()
        self._scan_bar.setRange(0, 100)
        self._scan_bar.setValue(12)
        self._scan_bar.setTextVisible(False)
        self._scan_bar.setFixedHeight(6)
        self._scan_bar.setStyleSheet(
            f"QProgressBar {{ background: {C.BAR_BG}; border: none; border-radius: 3px; }} "
            f"QProgressBar::chunk {{ background: {C.ACC}; border-radius: 3px; }}"
        )
        lay.addWidget(self._scan_bar)
        self._scan_scroll = QScrollArea()
        self._scan_scroll.setWidgetResizable(True)
        self._scan_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scan_host = QWidget()
        self._scan_host.setStyleSheet("background: transparent;")
        self._scan_list = QVBoxLayout(self._scan_host)
        self._scan_list.setSpacing(10)
        self._scan_list.addStretch(1)
        self._scan_scroll.setWidget(self._scan_host)
        lay.addWidget(self._scan_scroll, 1)
        return page

    def _build_finish_page(self) -> QWidget:
        C, _S = _aurum()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(self._card("Step 4: Finish"))
        self._finish_summary = QLabel("")
        self._finish_summary.setWordWrap(True)
        self._finish_summary.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(self._finish_summary)
        return page

    def _platforms(self) -> list[PlatformRow]:
        return [
            PlatformRow("atomberg", "Atomberg Home", True, [("api_key", "API Key", "Atomberg developer API key", False), ("refresh_token", "Refresh Token", "Atomberg refresh token", True)]),
            PlatformRow("kasa", "TP-Link Kasa", True, [("host", "Device IP / Host", "Optional - leave empty to scan the network", False), ("username", "Username", "Optional cloud username", False), ("password", "Password", "Optional cloud password", True)]),
            PlatformRow("hue", "Philips Hue", True, [("bridge_ip", "Bridge IP Address", "Optional - bridge IP address", False), ("api_key", "API Key / Token", "Optional developer API key", False)]),
            PlatformRow("lg", "LG ThinQ", True, [("username", "Email / Username", "LG ThinQ account email", False), ("password", "Password", "LG ThinQ password", True)]),
            PlatformRow("daikin", "Daikin Smart AC", True, [("ip", "AC IP Address", "Optional local IP of AC unit", False), ("username", "Daikin Username", "Optional cloud account username", False), ("password", "Password", "Optional cloud password", True)]),
            PlatformRow("tuya", "Tuya / Smart Life", True, [("access_id", "Access ID / Client ID", "Tuya IoT platform developer ID", False), ("secret", "Access Secret / Client Secret", "Tuya IoT access secret key", True)]),
            PlatformRow("nest", "Nest / Google Home", True, [("project_id", "Project ID", "Nest device access project ID", False), ("client_secret", "Client Secret", "Nest OAuth client secret", True)]),
            PlatformRow("smartthings", "Samsung SmartThings", True, [("token", "Access Token", "SmartThings personal access token", True)]),
        ]

    def _choose_provider(self, key: str, label: str):
        self._provider_key = key
        self._provider_label = label
        for card in self._platform_buttons:
            card.setStyleSheet(_pick_card_style(card.objectName() == f"PlatformCard_{key}"))
        self._build_auth_form()
        self._stack.setCurrentIndex(1)
        self._sync_buttons()

    def _build_auth_form(self):
        while self._auth_form.count():
            item = self._auth_form.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._fields.clear()
        if not self._provider_key:
            self._auth_desc.setText("Select a platform to build the right authentication form.")
            return
        self._auth_desc.setText(f"Authenticate {self._provider_label}. Credentials are stored locally and encrypted.")
        for key, label, placeholder, secret in next((p.auth_fields for p in self._platforms() if p.key == self._provider_key), []):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            if secret:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._fields[key] = edit
            self._auth_form.addRow(label, edit)
        self._account_label = QLineEdit()
        self._account_label.setPlaceholderText("Example: Home, User, Bedroom Hub")
        self._auth_form.addRow("Device label", self._account_label)

    def _build_scan_results(self):
        C, _S = _aurum()
        while self._scan_list.count():
            item = self._scan_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._checks.clear()
        if not self._preview:
            self._scan_list.addWidget(QLabel("No devices discovered."))
            self._scan_list.addStretch(1)
            return
        for device in self._preview.get("devices", []):
            box = QCheckBox(f"{device['name']}  •  {device.get('manufacturer', '')}")
            box.setChecked(True)
            box.setStyleSheet(
                f"QCheckBox {{ color: {C.TEXT}; background: transparent; padding: 10px; }}"
            )
            self._scan_list.addWidget(box)
            self._checks.append((str(device["external_id"]), box))
        self._scan_list.addStretch(1)

    def _go_next(self):
        idx = self._stack.currentIndex()
        if idx == 0:
            if not self._provider_key:
                return
            self._stack.setCurrentIndex(1)
        elif idx == 1:
            if not self._provider_key:
                return
            credentials = {k: e.text().strip() for k, e in self._fields.items()}
            try:
                self._preview = self._service.preview_discovery(self._provider_key, credentials)
            except Exception as exc:
                self._auth_desc.setText(f"Authentication failed: {exc}")
                return
            self._scan_bar.setValue(55)
            self._scan_label.setText("Discovering devices...")
            self._build_scan_results()
            self._stack.setCurrentIndex(2)
        elif idx == 2:
            selected = [ext_id for ext_id, check in self._checks if check.isChecked()]
            self._result = {
                "provider_key": self._provider_key,
                "account_label": self._account_label.text().strip() or self._provider_label,
                "credentials": {k: e.text().strip() for k, e in self._fields.items()},
                "selected_external_ids": selected,
            }
            self._finish_summary.setText(
                f"{len(selected)} device(s) selected from {self._provider_label}.\nConnected devices are saved locally and will appear under MY DEVICES."
            )
            self._stack.setCurrentIndex(3)
        else:
            self.accept()
        self._sync_buttons()

    def _go_back(self):
        idx = self._stack.currentIndex()
        if idx > 0:
            self._stack.setCurrentIndex(idx - 1)
        self._sync_buttons()

    def _sync_buttons(self):
        idx = self._stack.currentIndex()
        self._back_btn.setEnabled(idx > 0)
        self._next_btn.setText("Connect" if idx == 2 else "Next" if idx < 2 else "Finish")
        self._next_btn.setEnabled(True if idx != 0 else bool(self._provider_key))


class _DeviceTile(ClickableFrame):
    action_requested = pyqtSignal(str, str, dict)
    select_requested = pyqtSignal(str)

    def __init__(self, device: dict[str, Any], parent=None):
        C, _S = _aurum()
        super().__init__(parent)
        self.device = device
        self.setObjectName("DeviceTileFrame")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMinimumHeight(180)
        self.setMaximumHeight(194)
        
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)

        # Header Row (Power Button in top right)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addStretch(1)
        
        self.power_btn = QPushButton("⏻")
        self.power_btn.setFixedSize(26, 26)
        self.power_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.power_btn.setFont(QFont(C.F_BODY, 10))
        
        is_on = bool(device.get("is_on"))
        self.power_btn.clicked.connect(lambda: self.action_requested.emit(str(self.device.get("id", "")), "power", {"is_on": not is_on}))
        header_row.addWidget(self.power_btn)
        lay.addLayout(header_row)
        
        # Product Image Center Container
        self.img_lbl = QLabel()
        self.img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_lbl.setFixedHeight(58)
        
        kind = str(device.get("device_type", "device")).lower()
        img_path = f"assets/{kind}.jpg"
        from pathlib import Path
        if Path(img_path).exists():
            pix = QPixmap(img_path).scaled(88, 58, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.img_lbl.setPixmap(pix)
        else:
            self.img_lbl.setText(self._glyph(kind))
            self.img_lbl.setFont(QFont(C.F_BODY, 18))
            self.img_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(self.img_lbl)
        
        # Device details
        self.name_lbl = QLabel(str(device.get("name", "Device")))
        self.name_lbl.setFont(QFont(C.F_BODY, 10, QFont.Weight.Bold))
        self.name_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        
        self.sub_lbl = QLabel(str(device.get("manufacturer", "")))
        self.sub_lbl.setFont(QFont(C.F_MONO, 8))
        self.sub_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        
        lay.addWidget(self.name_lbl)
        lay.addWidget(self.sub_lbl)
        
        # Controls area
        self._control_area = QVBoxLayout()
        self._control_area.setSpacing(3)
        self._control_area.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self._control_area)
        self._build_controls()
        
        self.set_active(False)

    def _glyph(self, kind: str) -> str:
        return {"fan": "🌀", "light": "💡", "ac": "❄️", "tv": "📺", "plug": "🔌", "sensor": "📡", "curtain": "🪟", "vacuum": "🧹"}.get(kind, "⚙️")

    def _make_slider(self, minv: int, maxv: int, value: int) -> QSlider:
        C, _S = _aurum()
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minv, maxv)
        slider.setValue(value)
        slider.setCursor(Qt.CursorShape.SplitHCursor)
        slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: {C.BAR_BG};
                height: 4px;
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {C.ACC};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {C.WHITE};
                width: 10px;
                height: 10px;
                margin-top: -3px;
                margin-bottom: -3px;
                border-radius: 5px;
            }}
        """)
        return slider

    def _build_controls(self):
        C, S = _aurum()
        kind = str(self.device.get("device_type", "device")).lower()
        is_on = bool(self.device.get("is_on"))
        traits = self.device.get("traits") if isinstance(self.device.get("traits"), dict) else {}
        
        # Every tile reads one caption style for its value line, so a fan's
        # speed and a light's brightness are the same face at the same weight.
        def caption(widget: QLabel) -> QLabel:
            widget.setFont(QFont(C.F_MONO, 9))
            widget.setStyleSheet(
                f"color: {C.TEXT_DIM}; background: transparent; border: none;"
            )
            return widget

        if kind == "fan":
            current_speed = traits.get("speed", 4)
            try:
                current_speed = int(current_speed)
            except Exception:
                current_speed = 4
                
            self.val_lbl = caption(QLabel(f"Speed {current_speed}" if is_on else "Off"))
            self._control_area.addWidget(self.val_lbl)
            
            # Segment Speed Indicators
            seg_row = QHBoxLayout()
            seg_row.setSpacing(4)
            seg_row.addStretch(0)
            for s in range(1, 6):
                seg = QFrame()
                seg.setFixedHeight(5)
                seg.setFixedWidth(24)
                seg.setStyleSheet(
                    f"background: {C.ACC if (is_on and s <= current_speed) else C.BAR_BG}; "
                    "border-radius: 2.5px;"
                )
                seg_row.addWidget(seg)
            seg_row.addStretch(1)
            self._control_area.addLayout(seg_row)
            
        elif kind == "light":
            try:
                current_brightness = int(traits.get("brightness", 70))
            except Exception:
                current_brightness = 70
            self.val_lbl = caption(QLabel(f"Brightness {current_brightness}%" if is_on else "Off"))
            self._control_area.addWidget(self.val_lbl)
            
        elif kind == "tv":
            status_row = QHBoxLayout()
            on_lbl = QLabel("On" if is_on else "Off")
            on_lbl.setFont(QFont(C.F_MONO, 9, QFont.Weight.Bold))
            on_lbl.setStyleSheet(
                f"color: {C.GREEN if is_on else C.TEXT_DIM}; "
                "background: transparent; border: none;"
            )
            status_row.addWidget(on_lbl)
            
            if is_on:
                try:
                    current_vol = int(traits.get("volume", 18))
                except Exception:
                    current_vol = 18
                status_row.addWidget(caption(QLabel(f"Volume {current_vol}")))
                status_row.addStretch(1)
                self._control_area.addLayout(status_row)
                
                # Volume track slider
                self.slider = self._make_slider(0, 100, current_vol)
                self.slider.valueChanged.connect(lambda value: self.action_requested.emit(str(self.device.get("id", "")), "volume", {"volume": value}))
                self._control_area.addWidget(self.slider)
            else:
                status_row.addStretch(1)
                self._control_area.addLayout(status_row)
                
        elif kind == "ac":
            try:
                current_temp = int(traits.get("temperature", 24))
            except Exception:
                current_temp = 24
            temp_lbl = QLabel(f"{current_temp}°C" if is_on else "Off")
            temp_lbl.setFont(QFont(C.F_DISPLAY, 13, QFont.Weight.Bold))
            temp_lbl.setStyleSheet(
                f"color: {C.ACC if is_on else C.TEXT_DIM}; "
                "background: transparent; border: none;"
            )
            self._control_area.addWidget(temp_lbl)
            
            if is_on:
                tags_row = QHBoxLayout()
                tags_row.setSpacing(6)
                for tag in ("❄ Cool", "◎ Fan Auto"):
                    tag_lbl = QLabel(tag)
                    tag_lbl.setFont(QFont(C.F_BODY, 8))
                    tag_lbl.setStyleSheet(
                        f"color: {C.TEXT}; background: {C.GLASS}; "
                        f"border: 1px solid {C.BORDER_B}; "
                        f"border-radius: {S.PILL_R}px; padding: 3px 10px;"
                    )
                    tags_row.addWidget(tag_lbl)
                tags_row.addStretch(1)
                self._control_area.addLayout(tags_row)
                
        elif kind == "plug":
            self.val_lbl = QLabel("On" if is_on else "Off")
            self.val_lbl.setFont(QFont(C.F_MONO, 9, QFont.Weight.Bold))
            self.val_lbl.setStyleSheet(
                f"color: {C.GREEN if is_on else C.TEXT_DIM}; "
                "background: transparent; border: none;"
            )
            self._control_area.addWidget(self.val_lbl)
            
        else:
            self._control_area.addWidget(caption(QLabel("No additional controls.")))

    def set_active(self, active: bool):
        """The tile's frame and its power control, from the shared specs.

        The frame keeps its object-name selector so the rule does not reach the
        segment bars and labels nested inside it.
        """
        C, _S = _aurum()
        self.setStyleSheet(_pick_card_style(active, "QFrame#DeviceTileFrame"))

        is_on = bool(self.device.get("is_on"))
        if is_on:
            self.power_btn.setStyleSheet(
                f"QPushButton {{ color: {C.ON_GOLD}; background: {C.GREEN}; "
                "border: none; border-radius: 13px; } "
                f"QPushButton:hover {{ background: {C.GREEN_D}; }}"
            )
        else:
            self.power_btn.setStyleSheet(
                f"QPushButton {{ color: {C.TEXT_DIM}; background: transparent; "
                f"border: 1px solid {C.BORDER_B}; border-radius: 13px; }} "
                f"QPushButton:hover {{ color: {C.ACC}; border: 1px solid {C.ACC}; }}"
            )

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            device_id = self.device.get("id")
            if device_id is None:
                return
            self.select_requested.emit(str(device_id))


class SaturnHomePage(QWidget):
    def __init__(self, parent=None):
        C, S = _aurum()
        super().__init__(parent)
        self._service = SmartHomeService()
        self._selected_device_id: str | None = None
        self._voice_state = "Listening"
        self._voice_phase = 0
        self._drawer_anim = None
        self._device_tiles: list[_DeviceTile] = []
        self._activity_items: list[dict[str, Any]] = []
        # "View All Activity" flips the panel between the ten most recent rows and
        # the whole log. False by default; see _toggle_activity_scope.
        self._activity_all = False
        self._device_columns_cached = 0

        self.setObjectName("SaturnHomePageModern")
        self.setStyleSheet(f"QWidget#SaturnHomePageModern {{ background: transparent; }} QScrollArea {{ background: transparent; border: none; }}")

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)

        self._main_frame = QFrame()
        self._main_frame.setStyleSheet(
            "QFrame {"
            "background: transparent;"
            "border: none;"
            "}"
        )
        main_lay = QVBoxLayout(self._main_frame)
        main_lay.setContentsMargins(20, 20, 20, 20)
        main_lay.setSpacing(14)
        root.addWidget(self._main_frame, 1)

        self._build_header(main_lay)

        # Split layout for body: Devices (Left) + Recent Activity Feed (Right)
        self._body_layout = QHBoxLayout()
        self._body_layout.setSpacing(16)
        
        # Left side: Devices container
        self._left_container = QWidget()
        self._left_lay = QVBoxLayout(self._left_container)
        self._left_lay.setContentsMargins(0, 0, 0, 0)
        self._left_lay.setSpacing(10)
        self._build_my_devices(self._left_lay)
        self._body_layout.addWidget(self._left_container, 3)
        
        # Recent Activity panel - a glass card. It used to light up on hover
        # like a button, which made a static readout look clickable.
        self._right_container = QFrame()
        self._right_container.setObjectName("ActivityPanel")
        self._right_container.setStyleSheet(S.glass_rule("QFrame#ActivityPanel"))
        self._right_lay = QVBoxLayout(self._right_container)
        self._right_lay.setContentsMargins(18, 18, 18, 18)
        self._right_lay.setSpacing(12)
        
        act_title = QLabel("RECENT ACTIVITY")
        act_title.setFont(QFont(C.F_MONO, 10, QFont.Weight.Bold))
        act_title.setStyleSheet(f"{S.mono(11)} letter-spacing: 2px;")
        self._right_lay.addWidget(act_title)
        
        act_scroll = QScrollArea()
        act_scroll.setWidgetResizable(True)
        act_scroll.setFrameShape(QFrame.Shape.NoFrame)
        act_scroll.setStyleSheet("background: transparent; border: none;")
        
        act_host = QWidget()
        act_host.setStyleSheet("background: transparent;")
        self._activity_list = QVBoxLayout(act_host)
        self._activity_list.setContentsMargins(0, 0, 0, 0)
        self._activity_list.setSpacing(8)
        self._activity_list.addStretch(1)
        
        act_scroll.setWidget(act_host)
        self._right_lay.addWidget(act_scroll, 1)
        
        # Link button: View All Activity
        self._view_all_act_btn = QPushButton("View All Activity  >")
        self._view_all_act_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_all_act_btn.setStyleSheet(_link_style())
        self._view_all_act_btn.clicked.connect(self._toggle_activity_scope)
        self._right_lay.addWidget(self._view_all_act_btn)
        
        self._body_layout.addWidget(self._right_container, 1)
        main_lay.addLayout(self._body_layout, 1)

        self._drawer = self._build_drawer()
        self._drawer.hide()

        self._refresh()

    def refresh(self):
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh()

    def _section_card(self, title: str) -> QFrame:
        C, _S = _aurum()
        frame = QFrame()
        frame.setStyleSheet(_panel_style())
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        head = QLabel(title)
        head.setFont(QFont(C.F_DISPLAY, 12, QFont.Weight.Bold))
        head.setStyleSheet(f"color: {C.TEXT}; letter-spacing: 1px; background: transparent;")
        lay.addWidget(head)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {C.BORDER}; border: none;")
        lay.addWidget(line)
        return frame

    def _build_header(self, lay: QVBoxLayout):
        C, S = _aurum()
        row = QHBoxLayout()
        row.setSpacing(12)
        text = QVBoxLayout()
        title = QLabel("SATURN HOME")
        title.setStyleSheet(S.display(24, 600, 1))
        subtitle = QLabel("Control your smart home with Saturn.")
        subtitle.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        text.addWidget(title)
        text.addWidget(subtitle)
        row.addLayout(text, 1)
        self._status_chip = QLabel()
        self._status_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_chip.setStyleSheet(
            f"QLabel {{ background: {C.GLASS}; border: 1px solid {C.BORDER_B}; "
            f"border-radius: {S.PILL_R}px; padding: 6px 14px; min-width: 110px; "
            f"font-family: '{C.F_MONO}'; }}"
        )
        row.addWidget(self._status_chip)
        self._voice_button = QPushButton("▮▮")
        self._voice_button.setFixedSize(S.ICON_D + 14, S.ICON_D + 14)
        self._voice_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._voice_button.setStyleSheet(S.icon_button(S.ICON_D + 14))
        self._voice_button.clicked.connect(self._toggle_voice_mode)
        row.addWidget(self._voice_button)
        lay.addLayout(row)

    def _build_voice_card(self, lay: QVBoxLayout):
        C, S = _aurum()
        self._voice_frame = QFrame()
        self._voice_frame.setStyleSheet(_panel_style())
        vf = QHBoxLayout(self._voice_frame)
        vf.setContentsMargins(18, 16, 18, 16)
        vf.setSpacing(16)
        self._mic_orb = QLabel("🎤")
        self._mic_orb.setFixedSize(86, 86)
        self._mic_orb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mic_orb.setFont(QFont(C.F_BODY, 26))
        self._mic_orb.setStyleSheet(
            f"QLabel {{ background: {C.PRI_GHO}; color: {C.TEXT}; "
            f"border: 2px solid {C.BORDER_B}; border-radius: 43px; }}"
        )
        vf.addWidget(self._mic_orb)
        middle = QVBoxLayout()
        self._voice_state_lbl = QLabel("Listening...")
        self._voice_state_lbl.setStyleSheet(S.display(18, 600, 0))
        self._voice_cmd_lbl = QLabel('"Turn bedroom fan to speed 4"')
        self._voice_cmd_lbl.setStyleSheet(
            f"color: {C.TEXT_MED}; background: transparent;"
        )
        middle.addWidget(self._voice_state_lbl)
        middle.addWidget(self._voice_cmd_lbl)
        vf.addLayout(middle, 1)
        self._wave = QHBoxLayout()
        self._wave.setSpacing(3)
        self._wave_bars: list[QFrame] = []
        for _ in range(36):
            bar = QFrame()
            bar.setFixedSize(4, 12)
            bar.setStyleSheet(f"background: {C.BORDER}; border-radius: 2px;")
            self._wave.addWidget(bar, alignment=Qt.AlignmentFlag.AlignVCenter)
            self._wave_bars.append(bar)
        vf.addLayout(self._wave)
        lay.addWidget(self._voice_frame)

    def _build_my_devices(self, lay: QVBoxLayout):
        C, S = _aurum()
        header = QHBoxLayout()
        title = QLabel("MY DEVICES")
        title.setStyleSheet(S.display(16, 500, 1))
        header.addWidget(title)
        header.addStretch(1)
        self._add_btn = QPushButton("+ Add Device")
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setStyleSheet(S.ghost())
        self._add_btn.clicked.connect(self._open_add_device)
        header.addWidget(self._add_btn)
        lay.addLayout(header)

        self._empty_state = QFrame()
        self._empty_state.setObjectName("EmptyStateCard")
        self._empty_state.setStyleSheet(S.glass_rule("QFrame#EmptyStateCard"))
        empty_lay = QVBoxLayout(self._empty_state)
        empty_lay.setContentsMargins(28, 32, 28, 32)
        empty_lay.setSpacing(10)
        empty_title = QLabel("No devices added.")
        empty_title.setStyleSheet(S.display(18, 500, 0))
        empty_sub = QLabel("Add a device to start controlling your home.")
        empty_sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        empty_lay.addWidget(empty_title, alignment=Qt.AlignmentFlag.AlignHCenter)
        empty_lay.addWidget(empty_sub, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._empty_add_btn = QPushButton("Add Device")
        self._empty_add_btn.setFixedSize(150, S.PILL_H + 8)
        self._empty_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._empty_add_btn.setStyleSheet(S.primary(radius=S.PILL_H // 2 + 4))
        self._empty_add_btn.clicked.connect(self._open_add_device)
        empty_lay.addWidget(self._empty_add_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self._empty_state)

        self._device_scroll = QScrollArea()
        self._device_scroll.setWidgetResizable(True)
        self._device_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._device_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._device_content = QWidget()
        self._device_content.setStyleSheet("background: transparent;")
        self._device_grid = QGridLayout(self._device_content)
        self._device_grid.setContentsMargins(0, 0, 0, 0)
        self._device_grid.setHorizontalSpacing(12)
        self._device_grid.setVerticalSpacing(12)
        self._device_scroll.setWidget(self._device_content)
        lay.addWidget(self._device_scroll, 1)

    def _build_voice_control_card(self) -> QFrame:
        C, S = _aurum()
        frame = QFrame()
        frame.setObjectName("VoiceControlCard")
        frame.setStyleSheet(S.glass_rule("QFrame#VoiceControlCard"))
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        
        left = QVBoxLayout()
        left.setSpacing(6)
        
        title = QLabel("VOICE CONTROL")
        title.setStyleSheet(S.mono(11))
        left.addWidget(title)
        
        wake_lay = QVBoxLayout()
        wake_lay.setSpacing(2)
        lbl = QLabel("Wake word")
        lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        
        word = QLabel("Saturn")
        word.setFont(QFont(C.F_DISPLAY, 10, QFont.Weight.Bold))
        word.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        
        wake_lay.addWidget(lbl)
        wake_lay.addWidget(word)
        left.addLayout(wake_lay)
        lay.addLayout(left, 1)
        
        # Micro wave button on right
        mic_btn = QPushButton("🎙")
        mic_btn.setFixedSize(S.ICON_D + 4, S.ICON_D + 4)
        mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        mic_btn.setStyleSheet(S.icon_button(S.ICON_D + 4))
        mic_btn.clicked.connect(self._toggle_voice_mode)
        lay.addWidget(mic_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        
        return frame

    def _build_drawer(self) -> QFrame:
        C, S = _aurum()
        drawer = QFrame(self)
        drawer.setObjectName("DetailsDrawerFrame")
        drawer.setStyleSheet(S.glass_rule("QFrame#DetailsDrawerFrame"))
        drawer.setGeometry(0, 0, 0, 0)
        lay = QVBoxLayout(drawer)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        
        top = QHBoxLayout()
        title = QLabel("DEVICE DETAILS")
        title.setStyleSheet(S.mono(11))
        top.addWidget(title)
        top.addStretch(1)
        
        close_btn = QPushButton("X")
        close_btn.setFixedSize(S.ICON_D, S.ICON_D)
        close_btn.clicked.connect(self._close_drawer)
        close_btn.setStyleSheet(S.icon_button(S.ICON_D))
        top.addWidget(close_btn)
        lay.addLayout(top)
        
        # The four readout cards are plain glass; their labels are transparent
        # so they do not punch a second fill through the card behind them.
        card_style = (
            S.glass_rule("QFrame", radius=16) + "\n"
            + "QLabel { background: transparent; border: none; }"
        )
        
        # 1. Name Card
        self._name_card = QFrame()
        self._name_card.setStyleSheet(card_style)
        nc_lay = QVBoxLayout(self._name_card)
        nc_lay.setContentsMargins(16, 14, 16, 14)
        self._drawer_name = QLabel("")
        self._drawer_name.setStyleSheet(S.display(18, 500, 0))
        nc_lay.addWidget(self._drawer_name)
        lay.addWidget(self._name_card)
        
        # 2. Meta Card
        self._meta_card = QFrame()
        self._meta_card.setStyleSheet(card_style)
        mc_lay = QVBoxLayout(self._meta_card)
        mc_lay.setContentsMargins(16, 14, 16, 14)
        self._drawer_meta = QLabel("")
        self._drawer_meta.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        mc_lay.addWidget(self._drawer_meta)
        lay.addWidget(self._meta_card)
        
        # 3. State Card
        self._state_card = QFrame()
        self._state_card.setStyleSheet(card_style)
        sc_lay = QVBoxLayout(self._state_card)
        sc_lay.setContentsMargins(16, 14, 16, 14)
        self._drawer_state = QLabel("")
        self._drawer_state.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        sc_lay.addWidget(self._drawer_state)
        lay.addWidget(self._state_card)
        
        # 4. Info Card
        self._info_card = QFrame()
        self._info_card.setStyleSheet(card_style)
        ic_lay = QVBoxLayout(self._info_card)
        ic_lay.setContentsMargins(16, 14, 16, 14)
        self._drawer_info = QLabel("")
        self._drawer_info.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ic_lay.addWidget(self._drawer_info)
        lay.addWidget(self._info_card)
        
        lay.addStretch(1)
        
        self._drawer_actions = QHBoxLayout()
        self._drawer_actions.setSpacing(8)
        lay.addLayout(self._drawer_actions)
        return drawer

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._drawer.isVisible():
            width = min(390, max(330, int(self.width() * 0.28)))
            self._drawer.setGeometry(self.width() - width - 12, 12, width, self.height() - 24)
        self._reflow_devices()
        
        pass

    def _device_columns(self) -> int:
        viewport = self._device_scroll.viewport().width() if hasattr(self, "_device_scroll") else 0
        available = max(1, viewport or self.width())
        if available >= 1240:
            return 4
        if available >= 980:
            return 3
        return 2

    def _reflow_devices(self):
        if not hasattr(self, "_device_grid"):
            return
        columns = self._device_columns()
        if columns == self._device_columns_cached:
            return
        self._device_columns_cached = columns
        self._refresh_devices(self._service.list_devices())

    def _animate_drawer(self, open_: bool):
        if self._drawer_anim and self._drawer_anim.state() == QPropertyAnimation.State.Running:
            self._drawer_anim.stop()
        current = self._drawer.geometry()
        width = min(360, max(300, int(self.width() * 0.28)))
        start = QRect(self.width() - 12, 12, 0, self.height() - 24) if not open_ else current
        end = QRect(self.width() - width - 12, 12, width, self.height() - 24) if open_ else QRect(self.width() - 12, 12, 0, self.height() - 24)
        if open_:
            self._drawer.show()
        self._drawer_anim = QPropertyAnimation(self._drawer, b"geometry", self)
        self._drawer_anim.setDuration(260)
        self._drawer_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._drawer_anim.setStartValue(start)
        self._drawer_anim.setEndValue(end)
        if not open_:
            self._drawer_anim.finished.connect(self._drawer.hide)
        self._drawer_anim.start()

    def _open_drawer(self, device: dict[str, Any]):
        C, S = _aurum()
        self._selected_device_id = str(device.get("id", ""))
        self._drawer_name.setText(str(device.get("name", "Device")))

        # The drawer's readouts are rich text inside a QLabel, which QSS cannot
        # reach, so their colours come from the tokens as literal hex here.
        def readout(pairs) -> str:
            body = "<br>".join(
                f"<span style='color: {C.TEXT_DIM}; font-size: 10pt;'>{label}:</span> "
                f"<span style='color: {colour}; font-weight: bold; font-size: 10pt;'>{value}</span>"
                for label, value, colour in pairs
            )
            return f"<div style='line-height: 140%;'>{body}</div>"

        self._drawer_meta.setText(readout([
            ("Manufacturer", device.get("manufacturer", ""), C.TEXT),
            ("Room", device.get("room", ""), C.TEXT),
            ("Connection", "Local", C.GREEN),
            ("Last Updated", "now", C.TEXT_MED),
        ]))

        traits = device.get("traits") if isinstance(device.get("traits"), dict) else {}
        is_on = bool(device.get("is_on"))
        self._drawer_state.setText(readout([
            ("Power", "On" if is_on else "Off", C.GREEN if is_on else C.TEXT_DIM),
        ]))

        self._drawer_info.setText(readout([
            ("Firmware", traits.get("firmware", "Current"), C.TEXT),
            ("MAC", traits.get("mac", "Local"), C.TEXT),
            ("IP", traits.get("ip", "Local"), C.TEXT),
            ("Battery", traits.get("battery", "N/A"), C.TEXT),
            ("Power Consumption",
             traits.get("energy_usage", traits.get("power_usage", "N/A")), C.TEXT),
        ]))
        
        while self._drawer_actions.count():
            item = self._drawer_actions.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        for text, slot in (("Rename", self._rename_selected), ("Restart", self._restart_selected), ("Forget", self._forget_selected)):
            btn = QPushButton(text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            btn.setStyleSheet(S.ghost(size=12))
            self._drawer_actions.addWidget(btn)
        self._animate_drawer(True)

    def _close_drawer(self):
        self._animate_drawer(False)

    def _rename_selected(self):
        if not self._selected_device_id:
            return
        device = self._service.get_device(self._selected_device_id)
        if not device:
            return
        from PyQt6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(self, "Rename Device", "New name:", text=str(device.get("name", "")))
        if ok and new_name.strip():
            self._service.rename_device(self._selected_device_id, new_name.strip())
            self._refresh()

    def _restart_selected(self):
        if self._selected_device_id:
            self._service.restart_device(self._selected_device_id)
            self._refresh()

    def _forget_selected(self):
        if self._selected_device_id:
            self._service.forget_device(self._selected_device_id)
            self._selected_device_id = None
            self._close_drawer()
            self._refresh()

    def _refresh(self):
        C, _S = _aurum()
        devices = self._service.list_devices()
        self._refresh_devices(devices)
        self._refresh_activity()
        
        # The chip's caption and value are rich text, so the tokens go in as
        # hex here rather than through a stylesheet QSS cannot reach.
        dot, word = ((C.GREEN, "Connected") if devices else (C.ACC, "Ready"))
        status_html = (
            "<div style='line-height: 1.3;'>"
            f"<span style='color: {C.TEXT_DIM}; font-size: 9px; font-weight: bold; "
            "letter-spacing: 1px;'>SYSTEM</span><br/>"
            f"<span style='color: {dot}; font-size: 12px;'>&#9679;</span> "
            f"<span style='color: {C.TEXT}; font-size: 11px; font-weight: bold;'>{word}</span>"
            "</div>"
        )
        self._status_chip.setText(status_html)
        self._empty_state.setVisible(not devices)
        self._device_scroll.setVisible(bool(devices))

    def _refresh_devices(self, devices: list[dict[str, Any]]):
        while self._device_grid.count():
            item = self._device_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._device_tiles.clear()
        
        # Reset row stretches
        for r in range(self._device_grid.rowCount() + 1):
            self._device_grid.setRowStretch(r, 0)
            
        if not devices:
            return
        columns = self._device_columns()
        self._device_columns_cached = columns
        for idx, device in enumerate(devices):
            tile = _DeviceTile(device)
            tile.select_requested.connect(self._on_tile_selected)
            tile.action_requested.connect(self._on_device_action)
            tile.set_active(str(device.get("id")) == self._selected_device_id)
            self._device_grid.addWidget(tile, idx // columns, idx % columns)
            self._device_tiles.append(tile)
            
        # Add stretch on the last row to absorb extra vertical space
        last_row = (len(devices) - 1) // columns + 1
        self._device_grid.setRowStretch(last_row, 1)

    def _toggle_activity_scope(self):
        """Expand or collapse the activity panel; it used to be an inert link.

        The button was created, styled and placed but never connected, so
        "View All Activity" did nothing on every click.
        """
        self._activity_all = not self._activity_all
        self._view_all_act_btn.setText("Show recent activity  <" if self._activity_all
                                      else "View All Activity  >")
        self._refresh_activity()

    def _refresh_activity(self):
        C, S = _aurum()
        while self._activity_list.count():
            item = self._activity_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._activity_items = self._service.recent_activity(1000 if self._activity_all else 10)
        if not self._activity_items:
            empty = QLabel("No recent activity yet.")
            empty.setStyleSheet(S.mono(11))
            self._activity_list.addWidget(empty)
            self._activity_list.addStretch(1)
            return
            
        for row in self._activity_items:
            item = QFrame()
            item.setStyleSheet("QFrame { background: transparent; border: none; }")
            box = QHBoxLayout(item)
            box.setContentsMargins(4, 4, 4, 4)
            box.setSpacing(12)
            
            # Timestamp (e.g. 12:08 PM)
            time_str = QDateTime.fromMSecsSinceEpoch(int(row["created_at"])).toString("h:mm AP")
            time_lbl = QLabel(time_str)
            time_lbl.setStyleSheet(f"{S.mono(10)} min-width: 60px;")
            box.addWidget(time_lbl)
            
            # Icon Badge - one gold-tinted tile per kind, as everywhere else
            kind = str(row.get("device_type", "device")).lower()
            badge = QLabel(self._activity_glyph(kind))
            badge.setFixedSize(32, 32)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFont(QFont(C.F_BODY, 11))
            badge.setStyleSheet(_badge_style(16))
            box.addWidget(badge)
            
            # Details Text
            text_box = QVBoxLayout()
            text_box.setSpacing(2)
            title = QLabel(str(row["title"]))
            title.setFont(QFont(C.F_BODY, 10, QFont.Weight.Bold))
            title.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
            
            detail = QLabel(str(row["detail"]))
            detail.setStyleSheet(S.mono(10))
            text_box.addWidget(title)
            text_box.addWidget(detail)
            
            box.addLayout(text_box, 1)
            self._activity_list.addWidget(item)
            
        self._activity_list.addStretch(1)

    def _activity_glyph(self, kind: str) -> str:
        return {"fan": "🌀", "light": "💡", "ac": "❄️", "tv": "📺", "plug": "🔌"}.get(kind, "⚙️")

    def _on_tile_selected(self, device_id: str):
        device = self._service.get_device(device_id)
        if not device:
            return
        self._selected_device_id = device_id
        for tile in self._device_tiles:
            tile.set_active(str(tile.device.get("id")) == device_id)
        self._open_drawer(device)

    def _on_device_action(self, device_id: str, action: str, payload: dict):
        try:
            self._service.execute_device_action(device_id, action, payload)
        except Exception:
            return
        self._refresh()
        device = self._service.get_device(device_id)
        if device:
            self._open_drawer(device)

    def _toggle_voice_mode(self):
        states = ["Idle", "Listening", "Thinking", "Executing", "Completed"]
        self._voice_phase = (self._voice_phase + 1) % len(states)
        self._voice_state = states[self._voice_phase]
        # Some UI paths may call this before voice widgets are built; guard access.
        if hasattr(self, "_voice_state_lbl") and isinstance(getattr(self, "_voice_state_lbl"), QLabel):
            try:
                self._voice_state_lbl.setText(f"{self._voice_state}...")
            except Exception:
                pass
        if hasattr(self, "_voice_state_small") and isinstance(getattr(self, "_voice_state_small"), QLabel):
            try:
                self._voice_state_small.setText(self._voice_state)
            except Exception:
                pass

    def _animate_voice(self):
        C, _S = _aurum()
        self._voice_phase += 1
        if hasattr(self, "_wave_bars") and self._wave_bars:
            for idx, bar in enumerate(self._wave_bars):
                height = 8 + int(18 * abs(((self._voice_phase + idx) % 14) - 7) / 7)
                bar.setFixedHeight(max(8, min(40, height)))
                bar.setStyleSheet(
                    f"background: {C.BORDER_B if self._voice_state in ('Listening', 'Executing') else C.BORDER}; "
                    "border-radius: 2px;"
                )
        if hasattr(self, "_voice_small_bars") and self._voice_small_bars:
            for idx, bar in enumerate(self._voice_small_bars):
                height = 6 + int(10 * abs(((self._voice_phase + idx) % 10) - 5) / 5)
                bar.setFixedHeight(max(6, min(24, height)))
                bar.setStyleSheet(
                    f"background: {C.BORDER_B if self._voice_state in ('Listening', 'Executing') else C.BORDER}; "
                    "border-radius: 2px;"
                )
        if hasattr(self, "_voice_cmd_lbl") and self._voice_cmd_lbl:
            self._voice_cmd_lbl.setText({"Idle": '"Saturn"', "Listening": '"Turn bedroom fan to speed 4"', "Thinking": '"Understanding..."', "Executing": '"Applying command..."', "Completed": '"Done"'}.get(self._voice_state, '"Saturn"'))
        if hasattr(self, "_mic_orb") and self._mic_orb:
            self._mic_orb.setText("🎙️" if self._voice_state in ("Listening", "Executing") else "🎙️")

    def _open_add_device(self):
        dialog = AddDeviceDialog(self._service, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.result_payload()
        provider_key = str(payload.get("provider_key", ""))
        selected = list(payload.get("selected_external_ids", []))
        if not provider_key or not selected:
            return
        creds = dict(payload.get("credentials", {}))
        label = str(payload.get("account_label", "Home"))
        try:
            self._service.connect_devices(provider_key, label, creds, selected)
        except Exception:
            return
        self._refresh()
