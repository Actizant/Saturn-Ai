import sys
import os
import shutil
import time
import subprocess
import winreg
import win32com.client
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QPushButton, QProgressBar, QCheckBox, QFrame, QStackedWidget,
    QGraphicsDropShadowEffect
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint
from PyQt6.QtGui import QFont, QColor


def get_default_install_dir() -> str:
    """Detect directory where Saturn is installed."""
    if hasattr(sys, '_MEIPASS'):
        # If running as compiled exe inside install folder
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.exists(os.path.join(exe_dir, "Saturn.exe")):
            return exe_dir
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Check registry first
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturn") as k:
            loc, _ = winreg.QueryValueEx(k, "InstallLocation")
            if loc and os.path.exists(loc):
                return loc
    except Exception:
        pass
    
    # Check default localappdata location
    candidate = os.path.join(os.environ.get('LOCALAPPDATA', os.environ.get('USERPROFILE', 'C:\\')), 'Saturn')
    if os.path.exists(candidate):
        return candidate
    return script_dir


class UninstallThread(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, target_dir: str, remove_user_data: bool = True):
        super().__init__()
        self.target_dir = os.path.abspath(target_dir)
        self.remove_user_data = remove_user_data

    def run(self):
        try:
            self.status.emit("Closing active Saturn instances...")
            self.progress.emit(10)
            
            # Kill running processes
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "Saturn.exe", "/T"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            except Exception:
                pass
            time.sleep(0.5)

            # Shortcuts cleanup
            self.status.emit("Removing shortcuts from Desktop and Start Menu...")
            self.progress.emit(30)
            try:
                shell = win32com.client.Dispatch("WScript.Shell")
                desktop = shell.SpecialFolders("Desktop")
                start_menu = shell.SpecialFolders("Programs")
                startup = shell.SpecialFolders("Startup")

                for sc in [
                    os.path.join(desktop, "Saturn.lnk"),
                    os.path.join(start_menu, "Saturn.lnk"),
                    os.path.join(start_menu, "Uninstall Saturn.lnk"),
                    os.path.join(startup, "Saturn.lnk")
                ]:
                    if os.path.exists(sc):
                        try:
                            os.remove(sc)
                        except Exception:
                            pass
            except Exception as e:
                print(f"Error removing shortcuts: {e}")

            # Windows Registry Cleanup
            self.status.emit("Unregistering from Windows Installed Apps...")
            self.progress.emit(55)
            try:
                key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturn"
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except Exception as e:
                print(f"Registry unregister error: {e}")

            # User data cleanup (optional)
            if self.remove_user_data:
                self.status.emit("Cleaning user cache and model configurations...")
                self.progress.emit(75)
                localappdata = os.environ.get('LOCALAPPDATA', '')
                if localappdata:
                    config_dir = os.path.join(localappdata, "SaturnAI")
                    if os.path.exists(config_dir):
                        try:
                            shutil.rmtree(config_dir, ignore_errors=True)
                        except Exception:
                            pass

            self.status.emit("Removing installed files...")
            self.progress.emit(90)
            time.sleep(0.5)

            # Schedule delayed deletion of the installation directory
            # (Because this process is currently running inside or alongside the target folder)
            self_exe = sys.executable
            detach_cmd = f'timeout /t 2 /nobreak >nul & rd /s /q "{self.target_dir}"'
            try:
                subprocess.Popen(
                    f'cmd.exe /c "{detach_cmd}"',
                    shell=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            except Exception as e:
                print(f"Failed to spawn detachment cleanup: {e}")

            self.progress.emit(100)
            self.status.emit("Saturn was successfully removed!")
            time.sleep(0.4)
            self.finished.emit()

        except Exception as exc:
            self.error.emit(str(exc))


class UninstallWizard(QWidget):
    def __init__(self, target_dir: str = None):
        super().__init__()
        self.setWindowTitle("Saturn - Uninstall")
        self.resize(680, 440)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag_pos = None

        self.target_dir = target_dir or get_default_install_dir()

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Content Card
        self.content_box = QWidget()
        self.content_box.setStyleSheet("""
            QWidget#ContentBox {
                background-color: rgba(12, 16, 24, 250);
                border-radius: 18px;
                border: 1px solid rgba(248, 113, 113, 0.3);
            }
            QLabel {
                background: transparent;
                border: none;
                color: #e0e8f0;
            }
            QPushButton.danger-btn {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ef4444, stop:1 #dc2626);
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 10px 22px;
                font-weight: 700;
                font-size: 13px;
                min-width: 90px;
            }
            QPushButton.danger-btn:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #f87171, stop:1 #ef4444);
            }
            QPushButton.primary-btn {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0099ff, stop:1 #00e5ff);
                color: #030712;
                border: none;
                border-radius: 8px;
                padding: 10px 24px;
                font-weight: 700;
                font-size: 13px;
                min-width: 90px;
            }
            QPushButton.secondary-btn {
                background: rgba(255, 255, 255, 0.05);
                color: #cbd5e1;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 8px;
                padding: 9px 20px;
                font-weight: 600;
                font-size: 13px;
                min-width: 80px;
            }
            QPushButton.secondary-btn:hover {
                background: rgba(255, 255, 255, 0.1);
                color: #ffffff;
            }
            QCheckBox {
                color: #cbd5e1;
                font-size: 12px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid rgba(248, 113, 113, 0.4);
                background: rgba(0, 0, 0, 0.4);
            }
            QCheckBox::indicator:checked {
                background: #ef4444;
                border-color: #ef4444;
            }
            QProgressBar {
                border: none;
                background-color: rgba(255, 255, 255, 0.08);
                border-radius: 6px;
                text-align: center;
                color: #ffffff;
                font-weight: bold;
                height: 18px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #f87171, stop:1 #ef4444);
                border-radius: 6px;
            }
        """)
        self.content_box.setObjectName("ContentBox")
        box_layout = QVBoxLayout(self.content_box)
        box_layout.setContentsMargins(28, 20, 28, 24)
        box_layout.setSpacing(14)

        # Title bar
        header_bar = QHBoxLayout()
        logo_label = QLabel("⚡ SATURN - UNINSTALLER")
        logo_label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        logo_label.setStyleSheet("color: #f87171; letter-spacing: 2px;")
        header_bar.addWidget(logo_label)
        header_bar.addStretch()

        btn_x = QPushButton("✕")
        btn_x.setFixedSize(28, 24)
        btn_x.setStyleSheet("QPushButton { background: transparent; color: #94a3b8; border: none; font-size: 13px; } QPushButton:hover { color: #f87171; }")
        btn_x.clicked.connect(self.close)
        header_bar.addWidget(btn_x)
        box_layout.addLayout(header_bar)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("background: rgba(255, 255, 255, 0.08); max-height: 1px;")
        box_layout.addWidget(divider)

        # Stacked Pages
        self.stacked_widget = QStackedWidget()
        box_layout.addWidget(self.stacked_widget, 1)

        self._init_page_confirm()
        self._init_page_progress()
        self._init_page_done()

        layout.addWidget(self.content_box)

        # Shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setColor(QColor(0, 0, 0, 240))
        shadow.setOffset(0, 8)
        self.content_box.setGraphicsEffect(shadow)

    # ── Page 0: Confirm ──────────────────────────────────────────────────────
    def _init_page_confirm(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(14)

        title = QLabel("Uninstall Saturn")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        lay.addWidget(title)

        desc = QLabel(
            "Are you sure you want to completely remove Saturn and all of its components "
            "from your computer?"
        )
        desc.setFont(QFont("Segoe UI", 11))
        desc.setStyleSheet("color: #94a3b8; line-height: 1.5;")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        # Info card
        info_card = QFrame()
        info_card.setStyleSheet("background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 10px; padding: 12px;")
        ic_lay = QVBoxLayout(info_card)
        ic_lay.setSpacing(6)

        lbl_loc = QLabel(f"Installation Location:\n{self.target_dir}")
        lbl_loc.setFont(QFont("Consolas", 10))
        lbl_loc.setStyleSheet("color: #38bdf8;")
        lbl_loc.setWordWrap(True)
        ic_lay.addWidget(lbl_loc)

        self.chk_remove_data = QCheckBox("Also remove user data, API keys, and cached AI models (%LOCALAPPDATA%\\SaturnAI)")
        self.chk_remove_data.setChecked(True)
        ic_lay.addWidget(self.chk_remove_data)
        lay.addWidget(info_card)

        lay.addStretch()

        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setProperty("class", "secondary-btn")
        btn_cancel.clicked.connect(self.close)

        btn_uninstall = QPushButton("Uninstall")
        btn_uninstall.setProperty("class", "danger-btn")
        btn_uninstall.clicked.connect(self.start_uninstall)

        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(btn_uninstall)
        lay.addLayout(btn_row)

        self.stacked_widget.addWidget(page)

    # ── Page 1: Progress ─────────────────────────────────────────────────────
    def _init_page_progress(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(14)

        title = QLabel("Uninstalling Saturn...")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        lay.addWidget(title)

        subtitle = QLabel("Please wait while Saturn is removed from your system.")
        subtitle.setFont(QFont("Segoe UI", 10))
        subtitle.setStyleSheet("color: #94a3b8;")
        lay.addWidget(subtitle)

        lay.addSpacing(12)

        self.lbl_status = QLabel("Initializing uninstaller...")
        self.lbl_status.setFont(QFont("Segoe UI", 11))
        self.lbl_status.setStyleSheet("color: #f87171;")
        lay.addWidget(self.lbl_status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        lay.addWidget(self.progress_bar)

        lay.addStretch()
        self.stacked_widget.addWidget(page)

    # ── Page 2: Done ─────────────────────────────────────────────────────────
    def _init_page_done(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(14)

        title = QLabel("✓ Uninstallation Complete")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #38bdf8;")
        lay.addWidget(title)

        desc = QLabel(
            "Saturn has been successfully removed from your computer.\n"
            "All shortcuts, registry entries, and core files have been cleaned."
        )
        desc.setFont(QFont("Segoe UI", 11))
        desc.setStyleSheet("color: #cbd5e1; line-height: 1.5;")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        lay.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton("Finish")
        btn_close.setProperty("class", "primary-btn")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

        self.stacked_widget.addWidget(page)

    # ── Window Dragging ──────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    # ── Execution ────────────────────────────────────────────────────────────
    def start_uninstall(self):
        self.stacked_widget.setCurrentIndex(1)
        self.thread = UninstallThread(
            target_dir=self.target_dir,
            remove_user_data=self.chk_remove_data.isChecked()
        )
        self.thread.progress.connect(self.progress_bar.setValue)
        self.thread.status.connect(self.lbl_status.setText)
        self.thread.finished.connect(lambda: self.stacked_widget.setCurrentIndex(2))
        self.thread.error.connect(self.on_error)
        self.thread.start()

    def on_error(self, msg: str):
        self.lbl_status.setText(f"Uninstall error: {msg}")
        self.lbl_status.setStyleSheet("color: #ff4444; font-weight: bold;")


def run_silent_uninstall(target_dir: str = None):
    """Silent uninstall mode for /S flag."""
    target = target_dir or get_default_install_dir()
    thread = UninstallThread(target, remove_user_data=True)
    thread.run()


if __name__ == '__main__':
    # Check for silent /S flag from Windows Settings or command line
    if "/S" in sys.argv or "-s" in sys.argv or "--silent" in sys.argv:
        run_silent_uninstall()
        sys.exit(0)

    app = QApplication(sys.argv)
    wizard = UninstallWizard()
    wizard.show()
    sys.exit(app.exec())
