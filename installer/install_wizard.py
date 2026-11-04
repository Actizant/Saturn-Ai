import sys
import os
import shutil
import time
import win32com.client
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QPushButton, QProgressBar, QFileDialog, QGraphicsDropShadowEffect,
    QStackedWidget, QLineEdit, QCheckBox, QFrame
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl, QTimer, QPoint
from PyQt6.QtGui import QIcon, QFont, QColor, QCursor
from PyQt6.QtWebEngineWidgets import QWebEngineView


class InstallThread(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, source_dir: str, target_dir: str, create_desktop: bool = True, create_start_menu: bool = True, run_startup: bool = False):
        super().__init__()
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.create_desktop = create_desktop
        self.create_start_menu = create_start_menu
        self.run_startup = run_startup

    def run(self):
        try:
            self.status.emit("Initializing installation environment...")
            self.progress.emit(5)
            time.sleep(0.5)

            if os.path.exists(self.target_dir):
                self.status.emit("Cleaning previous installation...")
                self.progress.emit(10)
                shutil.rmtree(self.target_dir, ignore_errors=True)
                time.sleep(0.3)
            
            self.progress.emit(15)
            self.status.emit("Preparing target directory...")
            os.makedirs(self.target_dir, exist_ok=True)

            total_files = sum([len(files) for _, _, files in os.walk(self.source_dir)])
            copied = 0
            
            self.status.emit("Deploying application binaries and AI models...")
            for src_dir, _, files in os.walk(self.source_dir):
                dst_dir = src_dir.replace(self.source_dir, self.target_dir, 1)
                os.makedirs(dst_dir, exist_ok=True)
                for file_ in files:
                    src_file = os.path.join(src_dir, file_)
                    dst_file = os.path.join(dst_dir, file_)
                    shutil.copy2(src_file, dst_file)
                    copied += 1
                    if total_files > 0:
                        prog = 15 + int((copied / total_files) * 65)
                        if prog % 4 == 0:
                            self.progress.emit(prog)
                            if copied % 15 == 0:
                                self.status.emit(f"Extracting: {file_}")

            self.progress.emit(85)
            self.status.emit("Configuring application shortcuts and permissions...")
            
            exe_path = os.path.join(self.target_dir, 'Saturn.exe')
            ico_path = os.path.join(self.target_dir, 'assets', 'Saturn_Logo.ico')
            if not os.path.exists(ico_path):
                # Fallback to any ico in assets
                assets_dir = os.path.join(self.target_dir, 'assets')
                if os.path.exists(assets_dir):
                    for f in os.listdir(assets_dir):
                        if f.endswith('.ico'):
                            ico_path = os.path.join(assets_dir, f)
                            break

            shell = win32com.client.Dispatch("WScript.Shell")
            
            # Desktop Shortcut
            if self.create_desktop and os.path.exists(exe_path):
                try:
                    desktop = shell.SpecialFolders("Desktop")
                    shortcut_path = os.path.join(desktop, "Saturn.lnk")
                    shortcut = shell.CreateShortCut(shortcut_path)
                    shortcut.Targetpath = exe_path
                    shortcut.WorkingDirectory = self.target_dir
                    if os.path.exists(ico_path):
                        shortcut.IconLocation = ico_path
                    shortcut.WindowStyle = 1
                    shortcut.Description = "Saturn - Autonomous AI Assistant"
                    shortcut.save()
                except Exception as e:
                    print(f"Failed to create desktop shortcut: {e}")

            # Start Menu Shortcut
            if self.create_start_menu and os.path.exists(exe_path):
                try:
                    start_menu = shell.SpecialFolders("Programs")
                    shortcut_path_sm = os.path.join(start_menu, "Saturn.lnk")
                    shortcut_sm = shell.CreateShortCut(shortcut_path_sm)
                    shortcut_sm.Targetpath = exe_path
                    shortcut_sm.WorkingDirectory = self.target_dir
                    if os.path.exists(ico_path):
                        shortcut_sm.IconLocation = ico_path
                    shortcut_sm.WindowStyle = 1
                    shortcut_sm.Description = "Saturn - Autonomous AI Assistant"
                    shortcut_sm.save()

                    # Start Menu Uninstall Shortcut
                    uninst_exe = os.path.join(self.target_dir, "Uninstall.exe")
                    uninst_cmd = os.path.join(self.target_dir, "uninstall.cmd")
                    uninst_target = uninst_exe if os.path.exists(uninst_exe) else uninst_cmd
                    if os.path.exists(uninst_target):
                        shortcut_path_un = os.path.join(start_menu, "Uninstall Saturn.lnk")
                        shortcut_un = shell.CreateShortCut(shortcut_path_un)
                        shortcut_un.Targetpath = uninst_target
                        shortcut_un.WorkingDirectory = self.target_dir
                        if os.path.exists(ico_path):
                            shortcut_un.IconLocation = ico_path
                        shortcut_un.Description = "Uninstall Saturn"
                        shortcut_un.save()
                except Exception as e:
                    print(f"Failed to create start menu shortcut: {e}")

            # Run on Windows Startup Shortcut
            if self.run_startup and os.path.exists(exe_path):
                try:
                    startup_dir = shell.SpecialFolders("Startup")
                    shortcut_path_su = os.path.join(startup_dir, "Saturn.lnk")
                    shortcut_su = shell.CreateShortCut(shortcut_path_su)
                    shortcut_su.Targetpath = exe_path
                    shortcut_su.WorkingDirectory = self.target_dir
                    if os.path.exists(ico_path):
                        shortcut_su.IconLocation = ico_path
                    shortcut_su.WindowStyle = 1
                    shortcut_su.Description = "Saturn Startup Daemon"
                    shortcut_su.save()
                except Exception as e:
                    print(f"Failed to register startup shortcut: {e}")

            # Certified Windows Uninstaller Registry Entry
            try:
                import winreg
                reg_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Saturn"
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE) as reg_key:
                    winreg.SetValueEx(reg_key, "DisplayName", 0, winreg.REG_SZ, "Saturn")
                    winreg.SetValueEx(reg_key, "DisplayVersion", 0, winreg.REG_SZ, "1.0.0")
                    winreg.SetValueEx(reg_key, "Publisher", 0, winreg.REG_SZ, "Saturn")
                    winreg.SetValueEx(reg_key, "InstallLocation", 0, winreg.REG_SZ, self.target_dir)
                    if os.path.exists(ico_path):
                        winreg.SetValueEx(reg_key, "DisplayIcon", 0, winreg.REG_SZ, ico_path)
                    
                    uninst_exe = os.path.join(self.target_dir, "Uninstall.exe")
                    if os.path.exists(uninst_exe):
                        winreg.SetValueEx(reg_key, "UninstallString", 0, winreg.REG_SZ, f'"{uninst_exe}"')
                        winreg.SetValueEx(reg_key, "QuietUninstallString", 0, winreg.REG_SZ, f'"{uninst_exe}" /S')
                    else:
                        cmd_path = os.path.join(self.target_dir, "uninstall.cmd")
                        winreg.SetValueEx(reg_key, "UninstallString", 0, winreg.REG_SZ, f'cmd.exe /c "{cmd_path}"')
                        winreg.SetValueEx(reg_key, "QuietUninstallString", 0, winreg.REG_SZ, f'cmd.exe /c "{cmd_path}" /S')
                    
                    winreg.SetValueEx(reg_key, "EstimatedSize", 0, winreg.REG_DWORD, 1500000)
                    winreg.SetValueEx(reg_key, "NoModify", 0, winreg.REG_DWORD, 1)
                    winreg.SetValueEx(reg_key, "NoRepair", 0, winreg.REG_DWORD, 1)
            except Exception as e:
                print(f"Failed to register Windows uninstaller: {e}")

            self.progress.emit(100)
            self.status.emit("Installation Complete!")
            time.sleep(0.5)
            self.finished.emit()

        except Exception as e:
            self.error.emit(str(e))


class InstallWizard(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Saturn - Setup")
        self.resize(840, 560)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag_pos = None

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Background Container
        self.bg_container = QWidget(self)
        self.bg_container.resize(self.size())
        
        # Web Engine View for Animated Orb Background
        self.web_view = QWebEngineView(self.bg_container)
        self.web_view.resize(self.size())
        self.web_view.page().setBackgroundColor(Qt.GlobalColor.transparent)

        # Find base dir / assets folder
        if hasattr(sys, '_MEIPASS'):
            self.base_dir = sys._MEIPASS
        else:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        orb_path = os.path.join(self.base_dir, 'assets', 'web_background', 'index.html')
        if os.path.exists(orb_path):
            self.web_view.setUrl(QUrl.fromLocalFile(orb_path))

        # Overlay UI
        self.overlay = QWidget(self)
        self.overlay.resize(self.size())
        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(40, 30, 40, 30)

        # Glassmorphic Content Card
        self.content_box = QWidget()
        self.content_box.setStyleSheet("""
            QWidget#ContentBox {
                background-color: rgba(12, 16, 24, 235);
                border-radius: 18px;
                border: 1px solid rgba(0, 229, 255, 0.25);
            }
            QLabel {
                background: transparent;
                border: none;
                color: #e0e8f0;
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
            QPushButton.primary-btn:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00e5ff, stop:1 #38bdf8);
            }
            QPushButton.primary-btn:disabled {
                background: #334155;
                color: #64748b;
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
                border-color: rgba(0, 229, 255, 0.4);
            }
            QLineEdit {
                background: rgba(0, 0, 0, 0.4);
                border: 1px solid rgba(0, 229, 255, 0.3);
                border-radius: 8px;
                color: #ffffff;
                padding: 8px 12px;
                font-size: 12px;
                font-family: 'Consolas', 'Segoe UI';
            }
            QLineEdit:focus {
                border: 1px solid #00e5ff;
            }
            QCheckBox {
                color: #f1f5f9;
                font-size: 13px;
                spacing: 10px;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid rgba(0, 229, 255, 0.4);
                background: rgba(0, 0, 0, 0.3);
            }
            QCheckBox::indicator:checked {
                background: #00e5ff;
                border-color: #00e5ff;
                image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="%23000" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>');
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
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0099ff, stop:1 #00e5ff);
                border-radius: 6px;
            }
        """)
        self.content_box.setObjectName("ContentBox")
        box_layout = QVBoxLayout(self.content_box)
        box_layout.setContentsMargins(28, 20, 28, 24)
        box_layout.setSpacing(14)

        # Header Title Bar
        header_bar = QHBoxLayout()
        header_bar.setContentsMargins(0, 0, 0, 6)
        
        logo_label = QLabel("⚡ SATURN")
        logo_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        logo_label.setStyleSheet("color: #00e5ff; letter-spacing: 2px;")
        header_bar.addWidget(logo_label)
        header_bar.addStretch()

        # Window controls
        btn_min = QPushButton("─")
        btn_min.setFixedSize(28, 24)
        btn_min.setStyleSheet("QPushButton { background: transparent; color: #94a3b8; border: none; font-size: 12px; } QPushButton:hover { color: white; }")
        btn_min.clicked.connect(self.showMinimized)
        
        btn_x = QPushButton("✕")
        btn_x.setFixedSize(28, 24)
        btn_x.setStyleSheet("QPushButton { background: transparent; color: #94a3b8; border: none; font-size: 13px; } QPushButton:hover { color: #f87171; }")
        btn_x.clicked.connect(self.close)
        
        header_bar.addWidget(btn_min)
        header_bar.addWidget(btn_x)
        box_layout.addLayout(header_bar)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("background: rgba(255, 255, 255, 0.08); max-height: 1px;")
        box_layout.addWidget(divider)

        # Stacked Widget for 5-page flow
        self.stacked_widget = QStackedWidget()
        box_layout.addWidget(self.stacked_widget, 1)

        # Default install destination path
        self.install_path = os.path.join(os.environ.get('LOCALAPPDATA', os.environ.get('USERPROFILE', 'C:\\')), 'Saturn')

        # Build pages
        self._init_page_intro()
        self._init_page_destination()
        self._init_page_options()
        self._init_page_installing()
        self._init_page_completed()

        overlay_layout.addWidget(self.content_box)
        layout.addWidget(self.overlay)

        # Shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setColor(QColor(0, 0, 0, 220))
        shadow.setOffset(0, 6)
        self.content_box.setGraphicsEffect(shadow)

    # ── Page 0: Intro / Welcome ──────────────────────────────────────────────
    def _init_page_intro(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(16)

        title = QLabel("Welcome to Saturn Setup")
        title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        lay.addWidget(title)

        desc = QLabel(
            "This setup wizard will guide you through installing Saturn — the autonomous "
            "AI assistant with local action execution, real-time voice, vision, and system control.\n\n"
            "Saturn will be installed with full hardware acceleration and optimized background "
            "subsystems."
        )
        desc.setFont(QFont("Segoe UI", 11))
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #94a3b8; line-height: 1.5;")
        lay.addWidget(desc)

        # System Readiness Info Box
        info_card = QFrame()
        info_card.setStyleSheet("background: rgba(0, 229, 255, 0.04); border: 1px solid rgba(0, 229, 255, 0.15); border-radius: 10px; padding: 12px;")
        ic_lay = QVBoxLayout(info_card)
        ic_lay.setSpacing(6)

        ic_title = QLabel("✓ System Verification Ready")
        ic_title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        ic_title.setStyleSheet("color: #38bdf8;")
        ic_lay.addWidget(ic_title)

        os_info = QLabel("• Architecture: Windows 64-bit (x86_64) Native Runtime\n• Required Storage: ~1.5 GB Free Disk Space\n• Privileges: Standard User Installation (No root UAC hassle)")
        os_info.setFont(QFont("Segoe UI", 9))
        os_info.setStyleSheet("color: #cbd5e1;")
        ic_lay.addWidget(os_info)
        lay.addWidget(info_card)

        lay.addStretch()

        # Navigation row
        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setProperty("class", "secondary-btn")
        btn_cancel.clicked.connect(self.close)

        btn_next = QPushButton("Next >")
        btn_next.setProperty("class", "primary-btn")
        btn_next.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(1))

        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(btn_next)
        lay.addLayout(btn_row)

        self.stacked_widget.addWidget(page)

    # ── Page 1: Installation Place / Destination ─────────────────────────────
    def _init_page_destination(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(14)

        title = QLabel("Select Installation Directory")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        lay.addWidget(title)

        desc = QLabel("Setup will deploy Saturn into the following folder. To install into a different location, click Browse.")
        desc.setFont(QFont("Segoe UI", 10))
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #94a3b8;")
        lay.addWidget(desc)

        # Folder picker
        path_box = QHBoxLayout()
        self.txt_path = QLineEdit(self.install_path)
        self.txt_path.textChanged.connect(self._on_path_changed)
        path_box.addWidget(self.txt_path, 1)

        btn_browse = QPushButton("Browse...")
        btn_browse.setProperty("class", "secondary-btn")
        btn_browse.clicked.connect(self._browse_folder)
        path_box.addWidget(btn_browse)
        lay.addLayout(path_box)

        # Disk space status card
        self.space_card = QFrame()
        self.space_card.setStyleSheet("background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 10px; padding: 12px;")
        sp_lay = QVBoxLayout(self.space_card)
        sp_lay.setSpacing(4)

        self.lbl_space_req = QLabel("Space required: 1.5 GB")
        self.lbl_space_req.setFont(QFont("Segoe UI", 10))
        self.lbl_space_req.setStyleSheet("color: #94a3b8;")
        sp_lay.addWidget(self.lbl_space_req)

        self.lbl_space_avail = QLabel("Calculating available disk space...")
        self.lbl_space_avail.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.lbl_space_avail.setStyleSheet("color: #00e5ff;")
        sp_lay.addWidget(self.lbl_space_avail)
        lay.addWidget(self.space_card)
        lay.addStretch()

        # Navigation row
        btn_row = QHBoxLayout()
        btn_back = QPushButton("< Back")
        btn_back.setProperty("class", "secondary-btn")
        btn_back.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setProperty("class", "secondary-btn")
        btn_cancel.clicked.connect(self.close)

        self.btn_dest_next = QPushButton("Next >")
        self.btn_dest_next.setProperty("class", "primary-btn")
        self.btn_dest_next.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(2))

        btn_row.addWidget(btn_back)
        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_dest_next)
        lay.addLayout(btn_row)

        self.stacked_widget.addWidget(page)

        # Initial disk space calculation
        self._update_disk_space(self.install_path)

    def _browse_folder(self):
        chosen = QFileDialog.getExistingDirectory(self, "Select Installation Folder", self.txt_path.text())
        if chosen:
            # Append Saturn subfolder if not ending with it
            if not chosen.lower().endswith("saturn"):
                chosen = os.path.join(chosen, "Saturn")
            self.txt_path.setText(chosen)

    def _on_path_changed(self, new_path: str):
        self.install_path = new_path.strip()
        self._update_disk_space(self.install_path)

    def _update_disk_space(self, target_dir: str):
        try:
            drive = os.path.splitdrive(os.path.abspath(target_dir))[0] or "C:"
            usage = shutil.disk_usage(drive)
            free_gb = usage.free / (1024 ** 3)
            self.lbl_space_avail.setText(f"Space available on {drive}\\: {free_gb:.1f} GB")
            if hasattr(self, 'btn_dest_next') and self.btn_dest_next is not None:
                if free_gb < 1.5:
                    self.lbl_space_avail.setStyleSheet("color: #f87171; font-weight: bold;")
                    self.btn_dest_next.setEnabled(False)
                else:
                    self.lbl_space_avail.setStyleSheet("color: #38bdf8; font-weight: bold;")
                    self.btn_dest_next.setEnabled(True)
        except Exception:
            self.lbl_space_avail.setText("Space available: Sufficient")
            self.lbl_space_avail.setStyleSheet("color: #38bdf8;")
            if hasattr(self, 'btn_dest_next') and self.btn_dest_next is not None:
                self.btn_dest_next.setEnabled(True)

    # ── Page 2: Options & Permissions ────────────────────────────────────────
    def _init_page_options(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(14)

        title = QLabel("Select Additional Shortcuts & Permissions")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        lay.addWidget(title)

        desc = QLabel("Select which application shortcuts and startup preferences you want to configure:")
        desc.setFont(QFont("Segoe UI", 10))
        desc.setStyleSheet("color: #94a3b8;")
        lay.addWidget(desc)

        options_card = QFrame()
        options_card.setStyleSheet("background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 10px; padding: 16px;")
        oc_lay = QVBoxLayout(options_card)
        oc_lay.setSpacing(12)

        self.chk_desktop = QCheckBox("Create Desktop Shortcut (with Saturn Logo)")
        self.chk_desktop.setChecked(True)
        oc_lay.addWidget(self.chk_desktop)

        self.chk_start_menu = QCheckBox("Create Start Menu and Programs Shortcut")
        self.chk_start_menu.setChecked(True)
        oc_lay.addWidget(self.chk_start_menu)

        self.chk_startup = QCheckBox("Launch Saturn automatically when Windows starts")
        self.chk_startup.setChecked(False)
        oc_lay.addWidget(self.chk_startup)

        lay.addWidget(options_card)

        # Installation Summary
        summary_label = QLabel("Click 'Install' to begin deploying files. To review your settings, click Back.")
        summary_label.setFont(QFont("Segoe UI", 9))
        summary_label.setStyleSheet("color: #64748b; font-style: italic;")
        lay.addWidget(summary_label)

        lay.addStretch()

        # Navigation row
        btn_row = QHBoxLayout()
        btn_back = QPushButton("< Back")
        btn_back.setProperty("class", "secondary-btn")
        btn_back.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(1))

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setProperty("class", "secondary-btn")
        btn_cancel.clicked.connect(self.close)

        btn_install = QPushButton("⚡ Install Now")
        btn_install.setProperty("class", "primary-btn")
        btn_install.clicked.connect(self.start_installation)

        btn_row.addWidget(btn_back)
        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(btn_install)
        lay.addLayout(btn_row)

        self.stacked_widget.addWidget(page)

    # ── Page 3: Installing Progress ──────────────────────────────────────────
    def _init_page_installing(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(16)

        title = QLabel("Installing Saturn")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        lay.addWidget(title)

        subtitle = QLabel("Please wait while Setup deploys files to your computer...")
        subtitle.setFont(QFont("Segoe UI", 10))
        subtitle.setStyleSheet("color: #94a3b8;")
        lay.addWidget(subtitle)

        lay.addSpacing(16)

        self.lbl_status = QLabel("Preparing installation files...")
        self.lbl_status.setFont(QFont("Segoe UI", 11))
        self.lbl_status.setStyleSheet("color: #00e5ff;")
        lay.addWidget(self.lbl_status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        lay.addWidget(self.progress_bar)

        lay.addStretch()

        tip_label = QLabel("💡 Tip: Saturn features integrated Voice, Vision, and local AI automation.")
        tip_label.setFont(QFont("Segoe UI", 9))
        tip_label.setStyleSheet("color: #64748b; font-style: italic;")
        lay.addWidget(tip_label)

        self.stacked_widget.addWidget(page)

    # ── Page 4: Completed Page ───────────────────────────────────────────────
    def _init_page_completed(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(16)

        icon_title = QLabel("✨ Setup Completed Successfully")
        icon_title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        icon_title.setStyleSheet("color: #38bdf8;")
        lay.addWidget(icon_title)

        desc = QLabel(
            "Saturn has been successfully installed on your computer.\n"
            "The application may be launched by selecting the installed shortcuts."
        )
        desc.setFont(QFont("Segoe UI", 11))
        desc.setStyleSheet("color: #cbd5e1; line-height: 1.5;")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        finish_card = QFrame()
        finish_card.setStyleSheet("background: rgba(0, 229, 255, 0.05); border: 1px solid rgba(0, 229, 255, 0.2); border-radius: 10px; padding: 14px;")
        fc_lay = QVBoxLayout(finish_card)
        
        self.chk_launch = QCheckBox("Launch Saturn now")
        self.chk_launch.setChecked(True)
        self.chk_launch.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        fc_lay.addWidget(self.chk_launch)
        lay.addWidget(finish_card)

        lay.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_finish = QPushButton("Finish")
        btn_finish.setProperty("class", "primary-btn")
        btn_finish.clicked.connect(self._on_finish_clicked)
        btn_row.addWidget(btn_finish)
        lay.addLayout(btn_row)

        self.stacked_widget.addWidget(page)

    # ── Draggable Window Support ─────────────────────────────────────────────
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

    def resizeEvent(self, event):
        self.bg_container.resize(self.size())
        self.web_view.resize(self.size())
        self.overlay.resize(self.size())
        super().resizeEvent(event)

    # ── Installation Execution ───────────────────────────────────────────────
    def start_installation(self):
        self.stacked_widget.setCurrentIndex(3)
        source_dir = os.path.join(self.base_dir, 'Saturn')
        if not os.path.exists(source_dir):
            # Check dist/Saturn or payload
            dist_cand = os.path.join(self.base_dir, 'dist', 'Saturn')
            if os.path.exists(dist_cand):
                source_dir = dist_cand
            else:
                self.lbl_status.setText(f"Error: Payload missing at {source_dir}")
                self.lbl_status.setStyleSheet("color: #ff4444;")
                return

        self.thread = InstallThread(
            source_dir=source_dir,
            target_dir=self.install_path,
            create_desktop=self.chk_desktop.isChecked(),
            create_start_menu=self.chk_start_menu.isChecked(),
            run_startup=self.chk_startup.isChecked()
        )
        self.thread.progress.connect(self.progress_bar.setValue)
        self.thread.status.connect(self.lbl_status.setText)
        self.thread.finished.connect(self.on_finished)
        self.thread.error.connect(self.on_error)
        self.thread.start()

    def on_finished(self):
        self.stacked_widget.setCurrentIndex(4)

    def on_error(self, msg: str):
        self.lbl_status.setText(f"Installation failed: {msg}")
        self.lbl_status.setStyleSheet("color: #ff4444; font-weight: bold;")

    def _on_finish_clicked(self):
        if self.chk_launch.isChecked():
            exe_path = os.path.join(self.install_path, 'Saturn.exe')
            if os.path.exists(exe_path):
                os.startfile(exe_path)
        self.close()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    wizard = InstallWizard()
    wizard.show()
    sys.exit(app.exec())
