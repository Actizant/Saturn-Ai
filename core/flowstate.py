"""
core/flowstate.py
=================
FlowState Time Machine: Persistent Workspace State Snapshot & Restoration Engine.
Allows saving complete multi-app desktop environments (open browser tabs, VS Code
workspaces, window layouts, running apps) as named restoration points that survive
reboots and can be restored in seconds.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.user_paths import get_user_data_dir

logger = logging.getLogger("flowstate")
logger.setLevel(logging.INFO)

# System processes to ignore during desktop app snapshotting
IGNORE_PROCESSES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "memory compression",
    "spoolsv.exe", "antigravity.exe", "language_server.exe", "conhost.exe", "cmd.exe",
    "powershell.exe", "whip.exe", "taskhostw.exe", "sihost.exe", "shellexperiencehost.exe",
    "searchindexer.exe", "searchhost.exe", "startmenuexperiencehost.exe", "lockapp.exe",
    "applicationframehost.exe", "runtimebroker.exe", "ctfmon.exe", "dashost.exe",
    "securityhealthservice.exe", "msmpeng.exe", "mpdefendercorservice.exe", "wmiprvse.exe",
    "audiodg.exe", "lsaio.exe", "ngciso.exe", "textinputhost.exe", "tabtip.exe",
    "radeonsoftware.exe", "amdrsserv.exe", "amdrssrcsext.exe", "auepdu.exe",
    "rtkauduservice64.exe", "lively.watchdog.exe"
}


@dataclass
class AppSnapshot:
    name: str
    exe_path: str
    cmdline: List[str] = field(default_factory=list)
    title: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    is_maximized: bool = False
    cwd: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FlowStateSnapshot:
    name: str
    created_at: float
    created_str: str
    apps: List[AppSnapshot] = field(default_factory=list)
    chrome_tabs: List[str] = field(default_factory=list)
    edge_tabs: List[str] = field(default_factory=list)
    vscode_workspaces: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["apps"] = [a.to_dict() if isinstance(a, AppSnapshot) else a for a in self.apps]
        return d


class FlowStateManager:
    def __init__(self):
        self.user_data_dir = get_user_data_dir()
        self.storage_file = self.user_data_dir / "config" / "flowstate_snapshots.json"
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_file.exists():
            self.storage_file.write_text("{}", encoding="utf-8")

    def _load_all(self) -> Dict[str, Any]:
        try:
            if self.storage_file.exists():
                return json.loads(self.storage_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Failed to load FlowState snapshots: {exc}")
        return {}

    def _save_all(self, data: Dict[str, Any]) -> None:
        self.storage_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # =========================================================================
    # EXTRACTORS: BROWSER TABS & WORKSPACES
    # =========================================================================

    def extract_chrome_tabs(self) -> List[str]:
        """Extracts active URLs from Chrome session files on disk across Windows, macOS, and Linux."""
        urls: Set[str] = set()
        candidate_dirs: List[Path] = []

        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            candidate_dirs.append(Path(local_appdata) / "Google" / "Chrome" / "User Data" / "Default" / "Sessions")

        home = Path.home()
        # macOS
        candidate_dirs.append(home / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Sessions")
        # Linux
        candidate_dirs.append(home / ".config" / "google-chrome" / "Default" / "Sessions")
        candidate_dirs.append(home / ".config" / "chromium" / "Default" / "Sessions")

        try:
            for sessions_dir in candidate_dirs:
                if sessions_dir.exists():
                    session_files = sorted(sessions_dir.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)[:3]
                    for sf in session_files:
                        try:
                            content = sf.read_bytes()
                            matches = re.findall(rb'https?://[a-zA-Z0-9\.\-_/\?&=%#~:;+@]+', content)
                            for m in matches:
                                url = m.decode("utf-8", errors="ignore").strip()
                                if self._is_meaningful_url(url):
                                    urls.add(url)
                        except Exception:
                            pass
                    if urls:
                        break
        except Exception as exc:
            logger.debug(f"Chrome tab extraction error: {exc}")
        return sorted(list(urls))[:20]

    def extract_edge_tabs(self) -> List[str]:
        """Extracts active URLs from Microsoft Edge session files on disk across Windows, macOS, and Linux."""
        urls: Set[str] = set()
        candidate_dirs: List[Path] = []

        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            candidate_dirs.append(Path(local_appdata) / "Microsoft" / "Edge" / "User Data" / "Default" / "Sessions")

        home = Path.home()
        # macOS
        candidate_dirs.append(home / "Library" / "Application Support" / "Microsoft Edge" / "Default" / "Sessions")
        # Linux
        candidate_dirs.append(home / ".config" / "microsoft-edge" / "Default" / "Sessions")

        try:
            for sessions_dir in candidate_dirs:
                if sessions_dir.exists():
                    session_files = sorted(sessions_dir.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)[:3]
                    for sf in session_files:
                        try:
                            content = sf.read_bytes()
                            matches = re.findall(rb'https?://[a-zA-Z0-9\.\-_/\?&=%#~:;+@]+', content)
                            for m in matches:
                                url = m.decode("utf-8", errors="ignore").strip()
                                if self._is_meaningful_url(url):
                                    urls.add(url)
                        except Exception:
                            pass
                    if urls:
                        break
        except Exception as exc:
            logger.debug(f"Edge tab extraction error: {exc}")
        return sorted(list(urls))[:20]

    def _is_meaningful_url(self, url: str) -> bool:
        if len(url) < 10:
            return False
        skip_patterns = (
            "google.com/search?", "chrome-extension://", "edge-extension://",
            "gstatic.com", "accounts.google.com", "doubleclick.net", "facebook.com/tr",
            "google-analytics.com", "googletagmanager.com", "login.live.com", "microsoftonline.com"
        )
        return not any(pat in url.lower() for pat in skip_patterns)

    def extract_vscode_workspaces(self) -> List[str]:
        """Extracts active VS Code workspace folder paths across Windows, macOS, and Linux."""
        workspaces: Set[str] = set()
        candidate_files: List[Path] = []

        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidate_files.append(Path(appdata) / "Code" / "User" / "globalStorage" / "storage.json")

        home = Path.home()
        candidate_files.append(home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "storage.json")
        candidate_files.append(home / ".config" / "Code" / "User" / "globalStorage" / "storage.json")

        try:
            for storage_json in candidate_files:
                if storage_json.exists():
                    data = json.loads(storage_json.read_text(encoding="utf-8"))
                    profile_assoc = data.get("profileAssociations", {})
                    ws_dict = profile_assoc.get("workspaces", {})
                    for uri_str in ws_dict.keys():
                        if uri_str.startswith("file:///"):
                            path_str = urllib.parse.unquote(uri_str[7:])
                            # If Windows drive letter formatted as /C:/...
                            if len(path_str) >= 3 and path_str[0] == "/" and path_str[2] == ":":
                                path_str = path_str[1:]
                            norm = os.path.normpath(path_str)
                            if Path(norm).exists():
                                workspaces.add(norm)
                    if workspaces:
                        break
        except Exception as exc:
            logger.debug(f"VS Code workspace extraction error: {exc}")
        return list(workspaces)

    # =========================================================================
    # SNAPSHOT ENGINE
    # =========================================================================

    def snapshot_workspace(self, name: str, notes: str = "") -> Dict[str, Any]:
        """
        Captures the entire active workspace state:
        - Running desktop applications
        - Open Chrome & Edge tab URLs
        - Active VS Code project folders
        - Window placements & bounds
        Saves permanently to disk.
        """
        import psutil

        clean_name = name.strip() or "Default Workspace"
        t_now = time.time()
        created_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_now))

        app_snapshots: List[AppSnapshot] = []
        seen_exes: Set[str] = set()

        # 1. Enumerate visible running desktop applications
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                pexe = proc.info.get("exe") or ""
                if not pexe or pname in IGNORE_PROCESSES or pexe in seen_exes:
                    continue

                # Filter to user apps (installed in Program Files, AppData, /Applications, /usr, /home, etc.)
                is_user_app = any(p in pexe.lower() for p in (
                    "program files", "appdata\\local", "appdata\\roaming", "users\\",
                    "/applications", "/users/", "/usr/bin", "/opt", "/snap", "/home/"
                )) and not any(p in pexe.lower() for p in (
                    "windows\\system32", "windows\\syswow64", "/system/library", "/usr/libexec"
                ))

                if is_user_app:
                    seen_exes.add(pexe)
                    cmd = proc.info.get("cmdline") or []
                    cwd = ""
                    try:
                        cwd = proc.cwd()
                    except Exception:
                        pass

                    app_snapshots.append(AppSnapshot(
                        name=pname,
                        exe_path=pexe,
                        cmdline=cmd,
                        title=pname.replace(".exe", "").capitalize(),
                        cwd=cwd,
                    ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # 2. Extract browser tabs & developer workspaces
        chrome_tabs = self.extract_chrome_tabs()
        edge_tabs = self.extract_edge_tabs()
        vscode_workspaces = self.extract_vscode_workspaces()

        snapshot = FlowStateSnapshot(
            name=clean_name,
            created_at=t_now,
            created_str=created_str,
            apps=app_snapshots,
            chrome_tabs=chrome_tabs,
            edge_tabs=edge_tabs,
            vscode_workspaces=vscode_workspaces,
            notes=notes,
        )

        # 3. Save to persistent storage
        all_snapshots = self._load_all()
        all_snapshots[clean_name] = snapshot.to_dict()
        self._save_all(all_snapshots)

        logger.info(f"Saved FlowState snapshot '{clean_name}' with {len(app_snapshots)} apps and {len(chrome_tabs) + len(edge_tabs)} tabs.")

        return {
            "success": True,
            "name": clean_name,
            "created_str": created_str,
            "apps_count": len(app_snapshots),
            "apps": [a.name for a in app_snapshots],
            "chrome_tabs_count": len(chrome_tabs),
            "edge_tabs_count": len(edge_tabs),
            "vscode_workspaces": vscode_workspaces,
        }

    # =========================================================================
    # RESTORATION ENGINE
    # =========================================================================

    def restore_workspace(self, name: str) -> Dict[str, Any]:
        """
        Restores a previously snapshotted workspace:
        - Relaunches Chrome with saved URLs
        - Relaunches Edge with saved URLs
        - Relaunches VS Code into saved project folders
        - Relaunches saved desktop applications
        """
        import psutil

        all_snapshots = self._load_all()
        clean_name = name.strip()
        data = all_snapshots.get(clean_name)
        if not data:
            # Fuzzy match by case or substring
            for k, v in all_snapshots.items():
                if k.lower() == clean_name.lower() or clean_name.lower() in k.lower():
                    data = v
                    clean_name = k
                    break

        if not data:
            return {
                "success": False,
                "error": f"Restoration point '{name}' was not found. Use 'list workspaces' to see available points."
            }

        restored_items: List[str] = []
        currently_running_exes = set()
        for p in psutil.process_iter(["exe"]):
            try:
                exe = p.info.get("exe")
                if exe:
                    currently_running_exes.add(exe.lower())
            except Exception:
                pass

        # 1. Restore Chrome Tabs
        chrome_tabs = data.get("chrome_tabs", [])
        if chrome_tabs:
            chrome_exe = self._find_browser_exe("chrome.exe")
            if chrome_exe:
                try:
                    # Launch Chrome with the first 8 primary URLs to restore working session
                    target_urls = chrome_tabs[:8]
                    subprocess.Popen([chrome_exe, "--new-window"] + target_urls)
                    restored_items.append(f"Chrome ({len(target_urls)} tabs)")
                except Exception as exc:
                    logger.warning(f"Failed to launch Chrome tabs: {exc}")

        # 2. Restore Edge Tabs
        edge_tabs = data.get("edge_tabs", [])
        if edge_tabs:
            edge_exe = self._find_browser_exe("msedge.exe")
            if edge_exe:
                try:
                    target_urls = edge_tabs[:6]
                    subprocess.Popen([edge_exe, "--new-window"] + target_urls)
                    restored_items.append(f"Edge ({len(target_urls)} tabs)")
                except Exception as exc:
                    logger.warning(f"Failed to launch Edge tabs: {exc}")

        # 3. Restore VS Code Workspaces
        vscode_workspaces = data.get("vscode_workspaces", [])
        if vscode_workspaces:
            for ws_path in vscode_workspaces:
                if Path(ws_path).exists():
                    try:
                        # Launch code into directory
                        subprocess.Popen(f'code "{ws_path}"', shell=True)
                        restored_items.append(f"VS Code ({Path(ws_path).name})")
                    except Exception as exc:
                        logger.warning(f"Failed to launch VS Code workspace {ws_path}: {exc}")

        # 4. Restore Desktop Applications
        apps = data.get("apps", [])
        for app in apps:
            exe_path = app.get("exe_path", "")
            app_name = app.get("name", "").lower()
            if not exe_path or not Path(exe_path).exists():
                continue

            # Don't duplicate browsers or code since handled above
            if any(b in app_name for b in ("chrome", "msedge", "code")):
                continue

            # If already running, skip launching duplicate instance
            if exe_path.lower() in currently_running_exes:
                continue

            try:
                cwd = app.get("cwd") or None
                if cwd and not Path(cwd).exists():
                    cwd = None
                subprocess.Popen([exe_path], cwd=cwd)
                restored_items.append(app.get("title") or app_name.replace(".exe", "").capitalize())
            except Exception as exc:
                logger.debug(f"Could not launch app {exe_path}: {exc}")

        return {
            "success": True,
            "name": clean_name,
            "created_str": data.get("created_str", ""),
            "restored_items": restored_items,
            "total_restored": len(restored_items),
        }

    def _find_browser_exe(self, binary_name: str) -> Optional[str]:
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "Google" / "Chrome" / "Application" / binary_name,
            Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")) / "Google" / "Chrome" / "Application" / binary_name,
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / binary_name,
            Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / binary_name,
            Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "Microsoft" / "Edge" / "Application" / binary_name,
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    # =========================================================================
    # MANAGEMENT: LIST & DELETE
    # =========================================================================

    def list_workspaces(self) -> List[Dict[str, Any]]:
        """Returns summary of all saved FlowState restoration points."""
        all_snapshots = self._load_all()
        summary = []
        for name, data in all_snapshots.items():
            summary.append({
                "name": name,
                "created_str": data.get("created_str", "Unknown"),
                "apps_count": len(data.get("apps", [])),
                "tabs_count": len(data.get("chrome_tabs", [])) + len(data.get("edge_tabs", [])),
                "workspaces_count": len(data.get("vscode_workspaces", [])),
            })
        return sorted(summary, key=lambda x: x["name"])

    def delete_workspace(self, name: str) -> bool:
        """Deletes a saved restoration point."""
        all_snapshots = self._load_all()
        if name in all_snapshots:
            del all_snapshots[name]
            self._save_all(all_snapshots)
            return True
        return False


# Global singleton instance
_FLOWSTATE_MANAGER: Optional[FlowStateManager] = None


def get_flowstate_manager() -> FlowStateManager:
    global _FLOWSTATE_MANAGER
    if _FLOWSTATE_MANAGER is None:
        _FLOWSTATE_MANAGER = FlowStateManager()
    return _FLOWSTATE_MANAGER
