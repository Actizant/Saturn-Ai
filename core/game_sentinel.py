"""
core/game_sentinel.py
Saturn - Game Sentinel & Hardware Turbo Boost Supervisor.
Monitors system for running games (Steam, Epic, Riot, EA, Battle.net, standalone games):
1. Detects game launch in real time.
2. Elevates process priority to HIGH_PRIORITY_CLASS.
3. Automatically triggers Mem Reduct engine to purge standby memory and process working sets.
4. Switches Windows Power Plan to Ultimate Performance / High Performance.
5. Silences non-critical Saturn interventions (such as StudyGuard absence alerts) during gameplay.
6. Automatically restores balanced power plan and normal desktop profile upon game exit.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import psutil

from core.mem_reduct import MemReductEngine

logger = logging.getLogger("GameSentinel")

# Curated list of known game executables (lowercased)
KNOWN_GAME_EXES: Set[str] = {
    # Valve & Counter-Strike
    "cs2.exe", "csgo.exe", "dota2.exe", "hl2.exe", "left4dead2.exe", "tf_win64.exe", "tf2.exe",
    # Riot Games
    "valorant-win64-shipping.exe", "valorant.exe", "league of legends.exe", "leagueclientux.exe",
    # Epic & Unreal Games
    "fortniteclient-win64-shipping.exe", "fortnite.exe", "rocketleague.exe", "fallguys_client.exe",
    # EA & Respawn
    "r5apex.exe", "apex.exe", "fifa.exe", "fc24.exe", "fc25.exe", "starwarsjedisurvivor.exe",
    "battlefield2042.exe", "bfv.exe", "bf1.exe", "f1_24.exe", "f1_23.exe",
    # Activision & Blizzard
    "cod.exe", "callofduty.exe", "modernwarfare.exe", "blackops.exe", "overwatch.exe",
    "diablo iv.exe", "diabloiv.exe", "wow.exe", "world of warcraft.exe",
    # Rockstar & Bethesda
    "gta5.exe", "gta_sa.exe", "rdr2.exe", "starfield.exe", "fallout4.exe", "skyrimse.exe", "doom.exe", "doometernal.exe",
    # FromSoftware & CD Projekt Red
    "eldenring.exe", "armoredcore6.exe", "sekiro.exe", "dark souls iii.exe", "darksoulsiii.exe",
    "cyberpunk2077.exe", "witcher3.exe",
    # Larian & RPGs
    "bg3.exe", "bg3_dx11.exe", "hogwartslegacy.exe", "persona3reload.exe", "persona5royal.exe",
    # Survival & Shooters
    "palworld-win64-shipping.exe", "palworld.exe", "helldivers2.exe", "rust.exe", "tslgame.exe", "pubg.exe",
    "rainbowsix.exe", "rainbowsix_vulkan.exe", "warframe.x64.exe", "destiny2.exe",
    # HoYoverse
    "genshinimpact.exe", "starrail.exe", "zenlesszonezero.exe",
    # Microsoft & Racing
    "forzahorizon5.exe", "forzahorizon4.exe", "msfs.exe", "haloinfinite.exe", "seaofthieves.exe",
    "minecraft.exe", "minecraftlauncher.exe", "javaw.exe",
    # Capcom
    "mhw.exe", "monsterhunterworld.exe", "mhwilds.exe", "re4.exe", "streetfighter6.exe",
}

# Directories whose sub-executables are virtually always games (lowercased)
KNOWN_GAME_DIRS: Tuple[str, ...] = (
    "steamapps\\common",
    "steamapps/common",
    "epic games\\",
    "epic games/",
    "riot games\\",
    "riot games/",
    "ubisoft game launcher\\games",
    "ubisoft game launcher/games",
    "battle.net\\",
    "battle.net/",
    "xboxgames\\",
    "xboxgames/",
    "gog galaxy\\games",
    "gog galaxy/games",
    "ea games\\",
    "ea games/",
    "application support/steam/steamapps/common",
    ".steam/steam/steamapps/common",
)

# Launchers to ignore so we don't treat the store client itself as a game
LAUNCHERS_TO_IGNORE: Set[str] = {
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "riotclientservices.exe",
    "battlenet.exe", "eadesktop.exe", "eaconnect_mounter.exe", "gog galaxy.exe",
    "ubisoftconnect.exe", "origin.exe", "discord.exe",
    "steam", "steamwebhelper", "epicgameslauncher", "riotclientservices",
    "battlenet", "eadesktop", "gog galaxy", "ubisoftconnect", "origin", "discord"
}

KNOWN_GAME_BASE_NAMES: Set[str] = {exe.replace(".exe", "") for exe in KNOWN_GAME_EXES}


class GameSentinelManager:
    """Singleton manager monitoring game activity, hardware turbo boost, and memory purging."""

    _instance: Optional[GameSentinelManager] = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.is_game_mode_active = False
        self.active_game_name = ""
        self.active_game_pid: Optional[int] = None
        self.active_game_exe = ""
        self.game_start_time = 0.0

        self.previous_power_scheme_guid: Optional[str] = None
        self.last_ram_clean_result: Dict[str, Any] = {}

        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.is_watchdog_running = False

        self._speak_cb: Optional[Callable[[str], None]] = None
        self._hud_cb: Optional[Callable[[str, int, str], None]] = None

    @classmethod
    def get_instance(cls) -> GameSentinelManager:
        if cls._instance is None:
            cls._instance = GameSentinelManager()
        return cls._instance

    # =========================================================================
    # HARDWARE & POWER MANAGEMENT
    # =========================================================================

    @staticmethod
    def get_active_power_scheme() -> Optional[str]:
        """Queries Windows active power scheme GUID."""
        if os.name != "nt":
            return None
        try:
            out = subprocess.check_output(["powercfg", "/getactivescheme"], text=True, stderr=subprocess.DEVNULL)
            m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", out)
            if m:
                return m.group(1).lower()
        except Exception as e:
            logger.debug(f"Error querying active power scheme: {e}")
        return None

    @staticmethod
    def list_available_power_schemes() -> Dict[str, str]:
        """Returns mapping of scheme name -> GUID."""
        schemes = {}
        if os.name != "nt":
            return schemes
        try:
            out = subprocess.check_output(["powercfg", "/list"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s+\((.*?)\)", line)
                if m:
                    guid = m.group(1).lower()
                    name = m.group(2).strip()
                    schemes[name] = guid
        except Exception as e:
            logger.debug(f"Error listing power schemes: {e}")
        return schemes

    @classmethod
    def set_power_scheme(cls, target_guid: str) -> bool:
        """Sets active power scheme by GUID."""
        if os.name != "nt" or not target_guid:
            return False
        try:
            res = subprocess.run(["powercfg", "/setactive", target_guid], check=True, capture_output=True)
            return res.returncode == 0
        except Exception as e:
            logger.warning(f"Failed to set power scheme to {target_guid}: {e}")
            return False

    @classmethod
    def activate_turbo_power_plan(cls) -> Optional[str]:
        """Finds and activates Ultimate Performance or High Performance scheme."""
        schemes = cls.list_available_power_schemes()
        target_guid = None

        # Prefer Ultimate Performance, then High performance
        for name, guid in schemes.items():
            if "ultimate" in name.lower():
                target_guid = guid
                break
        if not target_guid:
            for name, guid in schemes.items():
                if "high performance" in name.lower() or "high" in name.lower():
                    target_guid = guid
                    break

        if target_guid:
            if cls.set_power_scheme(target_guid):
                return target_guid
        return None

    # =========================================================================
    # PROCESS DETECTION & PRIORITY
    # =========================================================================

    @staticmethod
    def elevate_process_priority(pid: int) -> bool:
        """Elevates process priority across Windows (HIGH_PRIORITY_CLASS) and Linux/macOS (nice -10)."""
        try:
            proc = psutil.Process(pid)
            if hasattr(psutil, "HIGH_PRIORITY_CLASS"):
                proc.nice(psutil.HIGH_PRIORITY_CLASS)
                logger.info(f"Elevated process PID {pid} to HIGH_PRIORITY_CLASS")
                return True
            else:
                # POSIX (Linux, macOS): negative nice elevates scheduling priority
                try:
                    proc.nice(-10)
                    logger.info(f"Elevated process PID {pid} nice value to -10")
                    return True
                except (psutil.AccessDenied, PermissionError):
                    logger.debug(f"User privileges insufficient to elevate nice value for PID {pid}")
        except Exception as e:
            logger.debug(f"Could not elevate priority for PID {pid}: {e}")
        return False

    def scan_for_game(self) -> Optional[Tuple[str, int, str]]:
        """
        Scans running processes for a game across Windows, Linux, and macOS.
        Returns (display_name, pid, exe_name) or None.
        """
        for proc in psutil.process_iter(['pid', 'name', 'exe']):
            try:
                name = (proc.info.get('name') or "").lower()
                pid = proc.info.get('pid') or 0
                exe = (proc.info.get('exe') or "").lower()
                norm_exe = exe.replace("\\", "/")

                if not name or pid <= 4 or name in LAUNCHERS_TO_IGNORE:
                    continue

                base_name = name[:-4] if name.endswith(".exe") else name

                # 1. Exact match in curated games list
                if name in KNOWN_GAME_EXES or base_name in KNOWN_GAME_BASE_NAMES:
                    display = base_name.replace("-win64-shipping", "").replace("_win64", "").replace("_linux", "").replace("_", " ").title()
                    return display, pid, name

                # 2. Check if process resides inside known game directories
                if any(gdir in norm_exe or gdir in exe for gdir in KNOWN_GAME_DIRS):
                    # Avoid helper utilities like crashpad, unitycrashhandler, etc.
                    if any(skip in name for skip in ("crashhandler", "crashreport", "cefsharp", "helper", "patcher", "unrealcef")):
                        continue
                    display = base_name.replace("-win64-shipping", "").replace("_win64", "").replace("_linux", "").replace("_", " ").title()
                    return display, pid, name

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue

        return None

    # =========================================================================
    # GAME MODE STATE MACHINE
    # =========================================================================

    def activate_game_mode(
        self,
        game_name: str = "Active Game",
        pid: Optional[int] = None,
        manual: bool = False,
        speak: bool = True
    ) -> Dict[str, Any]:
        """Engages Game Mode & Hardware Turbo Boost."""
        with self._lock:
            if self.is_game_mode_active and self.active_game_name == game_name:
                return {
                    "success": True,
                    "already_active": True,
                    "game_name": self.active_game_name,
                    "message": f"Game Mode is already active for {self.active_game_name}."
                }

            self.is_game_mode_active = True
            self.active_game_name = game_name
            self.active_game_pid = pid
            self.game_start_time = time.time()

            # 1. Save previous power scheme
            curr_scheme = self.get_active_power_scheme()
            if curr_scheme and not self.previous_power_scheme_guid:
                self.previous_power_scheme_guid = curr_scheme

            # 2. Engage Turbo Performance Power Scheme
            turbo_guid = self.activate_turbo_power_plan()

            # 3. Elevate game process priority
            elevated = False
            if pid:
                elevated = self.elevate_process_priority(pid)

            # 4. Clean RAM with Mem Reduct
            ram_res = MemReductEngine.clean_ram(purge_standby=True, empty_working_sets=True)
            self.last_ram_clean_result = ram_res

        # 5. Voice Announcement
        voice_msg = (
            f"Game detected: {game_name}. Activating Game Mode: Turbo boost engaged, "
            f"RAM purged by {ram_res.get('freed_mb', 0)} megabytes, and performance power plan active."
        ) if not manual else (
            f"Activating Game Mode: Turbo boost engaged, "
            f"RAM purged by {ram_res.get('freed_mb', 0)} megabytes, and performance profile active."
        )

        if speak and self._speak_cb:
            try:
                self._speak_cb(voice_msg)
            except Exception as e:
                logger.debug(f"Voice callback error: {e}")

        # 6. HUD Workspace signal
        if self._hud_cb:
            try:
                self._hud_cb(f"Game Mode: {game_name} (Active)", 100, voice_msg)
            except Exception:
                pass

        logger.info(f"Game Mode ENGAGED for '{game_name}' (PID: {pid}). RAM freed: {ram_res.get('freed_mb')}MB")

        return {
            "success": True,
            "game_name": game_name,
            "pid": pid,
            "priority_elevated": elevated,
            "power_plan": "Ultimate/High Performance" if turbo_guid else "Default",
            "ram_cleaned": ram_res,
            "message": voice_msg
        }

    def deactivate_game_mode(self, manual: bool = False, speak: bool = True) -> Dict[str, Any]:
        """Disengages Game Mode and restores normal desktop settings."""
        with self._lock:
            if not self.is_game_mode_active:
                return {
                    "success": False,
                    "message": "Game Mode is not currently active."
                }

            game_name = self.active_game_name or "Game"
            duration_sec = max(0, time.time() - self.game_start_time)
            mins = int(duration_sec // 60)
            secs = int(duration_sec % 60)

            # Restore previous power scheme
            restored = False
            if self.previous_power_scheme_guid:
                restored = self.set_power_scheme(self.previous_power_scheme_guid)
                self.previous_power_scheme_guid = None

            self.is_game_mode_active = False
            self.active_game_name = ""
            self.active_game_pid = None
            self.game_start_time = 0.0

        dur_text = f"{mins} minutes" if mins > 0 else f"{secs} seconds"
        voice_msg = f"{game_name} session concluded ({dur_text}). Game Mode disengaged. Hardware restored to normal profile, sir."

        if speak and self._speak_cb:
            try:
                self._speak_cb(voice_msg)
            except Exception as e:
                logger.debug(f"Voice callback error: {e}")

        if self._hud_cb:
            try:
                self._hud_cb("Game Mode Disengaged", 0, voice_msg)
            except Exception:
                pass

        logger.info(f"Game Mode DISENGAGED for '{game_name}'. Duration: {dur_text}")

        return {
            "success": True,
            "game_name": game_name,
            "duration_minutes": mins,
            "power_scheme_restored": restored,
            "message": voice_msg
        }

    def clean_ram_now(self, speak: bool = True) -> Dict[str, Any]:
        """Manual Mem Reduct purge on demand."""
        res = MemReductEngine.clean_ram(purge_standby=True, empty_working_sets=True)
        freed = res.get("freed_mb", 0)
        curr_pct = res.get("after_percent", 0)

        voice_msg = (
            f"RAM purged successfully, sir. Freed {freed} megabytes of memory. "
            f"Current RAM usage dropped to {curr_pct} percent."
        )

        if speak and self._speak_cb:
            try:
                self._speak_cb(voice_msg)
            except Exception:
                pass

        return {
            "success": True,
            "stats": res,
            "message": voice_msg
        }

    def get_status(self) -> Dict[str, Any]:
        """Returns live game mode and RAM telemetry."""
        mem = psutil.virtual_memory()
        with self._lock:
            return {
                "is_game_mode_active": self.is_game_mode_active,
                "active_game_name": self.active_game_name,
                "active_game_pid": self.active_game_pid,
                "elapsed_seconds": round(time.time() - self.game_start_time, 1) if self.is_game_mode_active else 0,
                "ram_percent": mem.percent,
                "ram_used_mb": round(mem.used / (1024 * 1024), 1),
                "ram_available_mb": round(mem.available / (1024 * 1024), 1),
                "last_ram_clean": self.last_ram_clean_result,
            }

    # =========================================================================
    # BACKGROUND AUTONOMOUS WATCHDOG DAEMON
    # =========================================================================

    def start_background_watchdog(
        self,
        poll_interval: float = 3.5,
        speak_cb: Optional[Callable[[str], None]] = None,
        hud_cb: Optional[Callable[[str, int, str], None]] = None
    ) -> None:
        """Starts background thread polling for game launches/exits."""
        with self._lock:
            if self.is_watchdog_running:
                return
            self.is_watchdog_running = True
            self._stop_event.clear()
            if speak_cb:
                self._speak_cb = speak_cb
            if hud_cb:
                self._hud_cb = hud_cb

            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                args=(poll_interval,),
                daemon=True,
                name="GameSentinelWatchdog"
            )
            self._watchdog_thread.start()
            logger.info("GameSentinel autonomous background watchdog started.")

    def stop_background_watchdog(self) -> None:
        """Stops watchdog thread."""
        with self._lock:
            self._stop_event.set()
            self.is_watchdog_running = False

    def _watchdog_loop(self, poll_interval: float) -> None:
        """Background poll loop checking process list."""
        while not self._stop_event.is_set():
            try:
                time.sleep(poll_interval)
                if self._stop_event.is_set():
                    break

                # 1. If Game Mode is currently ACTIVE: check if active game process is still alive
                if self.is_game_mode_active:
                    pid = self.active_game_pid
                    still_running = False
                    if pid:
                        try:
                            p = psutil.Process(pid)
                            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                                still_running = True
                        except Exception:
                            still_running = False
                    else:
                        # Fallback: scan if game is still in process list
                        found = self.scan_for_game()
                        still_running = found is not None

                    if not still_running:
                        logger.info(f"Game process (PID {pid}) exited. Automatically disengaging Game Mode...")
                        self.deactivate_game_mode(manual=False, speak=True)

                # 2. If Game Mode is NOT ACTIVE: scan if a game was launched
                else:
                    found = self.scan_for_game()
                    if found:
                        display_name, pid, exe_name = found
                        logger.info(f"Game launch detected: '{display_name}' (PID {pid}). Auto-activating Game Mode...")
                        self.activate_game_mode(game_name=display_name, pid=pid, manual=False, speak=True)

            except Exception as exc:
                logger.debug(f"GameSentinel watchdog iteration error: {exc}")


def get_game_sentinel_manager() -> GameSentinelManager:
    return GameSentinelManager.get_instance()
