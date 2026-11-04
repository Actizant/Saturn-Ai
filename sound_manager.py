from core.user_paths import get_user_data_dir
"""
Saturn - Cinematic Holographic Sound Effects Subsystem
Provides low-latency, non-blocking sci-fi acoustics for holographic UI interactions:
- Holographic wing deploy / aperture whoosh
- High-tech telemetry chirp
- Mission complete deliverable chime
- Listening start / stop pings
"""

import sys
import os
import time
import math
import wave
import struct
import json
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QUrl
from PyQt6.QtMultimedia import QSoundEffect

BASE_DIR = Path(__file__).resolve().parent
SOUNDS_DIR = BASE_DIR / "assets" / "sounds"
SETTINGS_FILE = get_user_data_dir() / "config" / "app_settings.json"


def _generate_default_sounds(target_dir: Path):
    """Procedurally synthesizes 16-bit 44.1kHz PCM WAV sci-fi sound assets if missing."""
    target_dir.mkdir(parents=True, exist_ok=True)
    sample_rate = 44100

    def write_wav(path: Path, samples: list[float]):
        with wave.open(str(path), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            raw = bytearray()
            for s in samples:
                val = int(max(min(s * 32767.0, 32767.0), -32767.0))
                raw.extend(struct.pack("<h", val))
            wf.writeframes(raw)

    # 1. Deploy Whoosh (~380ms): Filtered frequency sweep + airy resonance
    whoosh_path = target_dir / "deploy_whoosh.wav"
    if not whoosh_path.exists():
        n_samples = int(sample_rate * 0.38)
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            norm = t / 0.38
            env = math.sin(norm * math.pi) ** 1.8
            freq = 220.0 + 750.0 * math.sin(norm * math.pi)
            phase = 2.0 * math.pi * freq * t
            tone = (
                0.55 * math.sin(phase)
                + 0.25 * math.sin(phase * 1.5)
                + 0.15 * math.sin(phase * 2.3)
                + 0.08 * math.sin(phase * 3.7)
            )
            flutter = math.sin(i * 0.77) * math.cos(i * 0.23) * 0.12
            s = (tone + flutter) * env * 0.75
            samples.append(s)
        write_wav(whoosh_path, samples)

    # 2. Telemetry Chirp (~110ms): High-tech dual-pulse frequency modulation
    chirp_path = target_dir / "telemetry_chirp.wav"
    if not chirp_path.exists():
        n_samples = int(sample_rate * 0.11)
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            if t < 0.045:
                p_t = t / 0.045
                env = (1.0 - p_t) ** 1.5
                f = 2100.0 + 400.0 * p_t
                s = (0.65 * math.sin(2 * math.pi * f * t) + 0.25 * math.sin(4 * math.pi * f * t)) * env
            elif t < 0.055:
                s = 0.0
            else:
                p_t = (t - 0.055) / 0.055
                env = (1.0 - p_t) ** 2.0
                f = 2800.0 + 600.0 * p_t
                s = (0.75 * math.sin(2 * math.pi * f * t) + 0.25 * math.sin(4 * math.pi * f * t)) * env
            samples.append(s * 0.65)
        write_wav(chirp_path, samples)

    # 3. Mission Complete Chime (~950ms): Warm harmonic chord arpeggio with shimmer
    chime_path = target_dir / "mission_complete.wav"
    if not chime_path.exists():
        n_samples = int(sample_rate * 0.95)
        samples = []
        notes = [
            (0.00, 587.33, 0.40),
            (0.08, 739.99, 0.35),
            (0.16, 880.00, 0.30),
            (0.24, 1174.66, 0.45),
            (0.32, 1479.98, 0.25),
        ]
        for i in range(n_samples):
            t = i / sample_rate
            acc = 0.0
            for start_t, f, amp in notes:
                if t >= start_t:
                    dt = t - start_t
                    env = math.exp(-3.8 * dt)
                    tone = math.sin(2 * math.pi * f * dt) + 0.22 * math.sin(4 * math.pi * f * dt)
                    acc += amp * tone * env
            samples.append(acc * 0.55)
        write_wav(chime_path, samples)

    # 4. Listening Start (~150ms): Rising sci-fi two-tone ping
    start_path = target_dir / "listening_start.wav"
    if not start_path.exists():
        n_samples = int(sample_rate * 0.15)
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            p = t / 0.15
            env = math.sin(p * math.pi) ** 1.3
            f = 640.0 + 420.0 * p
            s = (0.7 * math.sin(2 * math.pi * f * t) + 0.2 * math.sin(4 * math.pi * f * t)) * env
            samples.append(s * 0.7)
        write_wav(start_path, samples)

    # 5. Listening Stop (~130ms): Falling sci-fi two-tone ping
    stop_path = target_dir / "listening_stop.wav"
    if not stop_path.exists():
        n_samples = int(sample_rate * 0.13)
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            p = t / 0.13
            env = math.sin(p * math.pi) ** 1.4
            f = 960.0 - 380.0 * p
            s = (0.7 * math.sin(2 * math.pi * f * t) + 0.2 * math.sin(4 * math.pi * f * t)) * env
            samples.append(s * 0.65)
        write_wav(stop_path, samples)

    # 6. Startup ready chime (~1.6s): warm glassy bell + soft sub thump. This is
    #    only a fallback - if assets/sounds/startup_ready.wav (the designed cue)
    #    is present it wins, so a fresh checkout is never silent but the real
    #    sound is the one users hear.
    ready_path = target_dir / "startup_ready.wav"
    if not ready_path.exists():
        n_samples = int(sample_rate * 1.6)
        samples = []
        notes = [
            (0.00, 440.00, 0.34),   # A - open, calm root
            (0.00, 660.00, 0.22),   # perfect fifth, no third = unhurried
            (0.10, 880.00, 0.20),
            (0.20, 1174.66, 0.16),
        ]
        for i in range(n_samples):
            t = i / sample_rate
            acc = 0.0
            for start_t, f, amp in notes:
                if t >= start_t:
                    dt = t - start_t
                    env = math.exp(-2.4 * dt)
                    tone = math.sin(2 * math.pi * f * dt) + 0.18 * math.sin(4 * math.pi * f * dt)
                    acc += amp * tone * env
            # soft low thump under the bell for the first ~0.4s
            if t < 0.4:
                acc += 0.28 * math.sin(2 * math.pi * 110.0 * t) * math.exp(-6.0 * t)
            samples.append(max(-1.0, min(1.0, acc * 0.6)))
        write_wav(ready_path, samples)

    # 7. Ready chime (~1.5s): warm glassy bell for the moment the orb opens its
    #    ears / becomes ready. Fallback only - assets/sounds/ready_chime.wav (the
    #    designed cue) wins when present, so a fresh checkout still chimes.
    rdy_path = target_dir / "ready_chime.wav"
    if not rdy_path.exists():
        n_samples = int(sample_rate * 1.5)
        samples = []
        notes = [
            (0.00, 523.25, 0.30),   # C5
            (0.00, 783.99, 0.20),   # G5 - open fifth, calm
            (0.10, 1046.50, 0.16),  # C6 shimmer on top
        ]
        for i in range(n_samples):
            t = i / sample_rate
            acc = 0.0
            for start_t, f, amp in notes:
                if t >= start_t:
                    dt = t - start_t
                    env = math.exp(-2.8 * dt)
                    tone = math.sin(2 * math.pi * f * dt) + 0.16 * math.sin(4 * math.pi * f * dt)
                    acc += amp * tone * env
            samples.append(max(-1.0, min(1.0, acc * 0.6)))
        write_wav(rdy_path, samples)

    # 8. Error sting (~0.45s): low falling buzz - the audible 'that didn't work'.
    #    Fallback for assets/sounds/error.wav (the designed cue).
    err_path = target_dir / "error.wav"
    if not err_path.exists():
        n_samples = int(sample_rate * 0.45)
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            p = t / 0.45
            env = (math.sin(p * math.pi) ** 0.7) * (1.0 - 0.35 * p)
            f = 300.0 - 150.0 * p                       # pitch sinks
            tone = (math.sin(2 * math.pi * f * t)
                    + 0.5 * math.sin(2 * math.pi * f * 1.48 * t)   # dissonant rub
                    + 0.25 * math.sin(2 * math.pi * f * 2.02 * t))
            s = (tone + 0.14 * math.sin(2 * math.pi * 90.0 * t)) * env * 0.55
            samples.append(max(-1.0, min(1.0, s)))
        write_wav(err_path, samples)

    # 9. Notification tick (~0.2s): a single bright, crisp blip for an arriving
    #    toast - the most-repeated cue, so short and light. Fallback for the
    #    designed assets/sounds/notification.wav.
    not_path = target_dir / "notification.wav"
    if not not_path.exists():
        n_samples = int(sample_rate * 0.2)
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            p = t / 0.2
            env = math.exp(-9.0 * t) * math.sin(math.pi * min(1.0, p * 8.0))
            f = 1580.0
            tone = math.sin(2 * math.pi * f * t) + 0.3 * math.sin(2 * math.pi * f * 2.0 * t)
            samples.append(max(-1.0, min(1.0, tone * env * 0.6)))
        write_wav(not_path, samples)


class SoundManager(QObject):
    """
    Centralized sound effects controller.
    Manages low-latency playback of sci-fi acoustics using Qt's QSoundEffect.
    """

    _instance: Optional["SoundManager"] = None

    @classmethod
    def instance(cls) -> "SoundManager":
        if cls._instance is None:
            cls._instance = SoundManager()
        return cls._instance

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._enabled = True
        self._volume = 0.75  # 0.0 to 1.0
        self._startup_enabled = True
        self._last_telemetry_time = 0.0
        self._telemetry_cooldown = 0.22  # Minimum 220ms between telemetry chirps
        self._effects: dict[str, QSoundEffect] = {}

        self._load_settings()
        self._init_audio()

    def _load_settings(self):
        try:
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._enabled = bool(data.get("sound_effects_enabled", True))
                    self._startup_enabled = bool(data.get("startup_sound_enabled", True))
                    raw_vol = int(data.get("sound_effects_volume", 75))
                    self._volume = max(0.0, min(1.0, raw_vol / 100.0))
        except Exception:
            self._enabled = True
            self._startup_enabled = True
            self._volume = 0.75

    def _save_settings(self):
        try:
            settings = {}
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    settings = json.load(f)
            settings["sound_effects_enabled"] = self._enabled
            settings["startup_sound_enabled"] = self._startup_enabled
            settings["sound_effects_volume"] = int(round(self._volume * 100))
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=4)
        except Exception:
            pass

    def _init_audio(self):
        try:
            _generate_default_sounds(SOUNDS_DIR)

            sound_files = {
                "deploy_whoosh": SOUNDS_DIR / "deploy_whoosh.wav",
                "telemetry_chirp": SOUNDS_DIR / "telemetry_chirp.wav",
                "mission_complete": SOUNDS_DIR / "mission_complete.wav",
                "listening_start": SOUNDS_DIR / "listening_start.wav",
                "listening_stop": SOUNDS_DIR / "listening_stop.wav",
                "startup_ready": SOUNDS_DIR / "startup_ready.wav",
                "dock_whoosh": SOUNDS_DIR / "dock_whoosh.wav",
                "ready_chime": SOUNDS_DIR / "ready_chime.wav",
                "error": SOUNDS_DIR / "error.wav",
                "notification": SOUNDS_DIR / "notification.wav",
                "mute": SOUNDS_DIR / "mute.wav",
                "unmute": SOUNDS_DIR / "unmute.wav",
            }

            for name, path in sound_files.items():
                if path.exists():
                    eff = QSoundEffect(self)
                    eff.setSource(QUrl.fromLocalFile(str(path.resolve())))
                    eff.setVolume(self._volume)
                    self._effects[name] = eff
        except Exception as e:
            print(f"[SoundManager] Initialization notice: {e}")

    # --- Property Accessors ---

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, val: bool):
        self._enabled = bool(val)
        self._save_settings()

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, val: float):
        self._volume = max(0.0, min(1.0, float(val)))
        for eff in self._effects.values():
            try:
                eff.setVolume(self._volume)
            except Exception:
                pass
        self._save_settings()

    def set_volume_percent(self, pct: int):
        self.volume = pct / 100.0

    def get_volume_percent(self) -> int:
        return int(round(self._volume * 100))

    # --- Playback Methods ---

    def _play(self, name: str):
        if not self._enabled:
            return
        eff = self._effects.get(name)
        if eff:
            try:
                if eff.isPlaying():
                    eff.stop()
                eff.setVolume(self._volume)
                eff.play()
            except Exception:
                pass

    def play_deploy_whoosh(self):
        """Holographic card slide-out / aperture expansion whoosh."""
        self._play("deploy_whoosh")

    def play_telemetry_chirp(self):
        """High-tech data flutter / telemetry step chirp (rate-limited)."""
        now = time.time()
        if now - self._last_telemetry_time >= self._telemetry_cooldown:
            self._last_telemetry_time = now
            self._play("telemetry_chirp")

    def play_mission_complete(self):
        """Warm harmonic chord arpeggio when reports, files, or tasks complete."""
        self._play("mission_complete")

    def play_listening_start(self):
        """Ascending prompt ping when microphone/voice capture begins."""
        self._play("listening_start")

    def play_listening_stop(self):
        """Descending prompt ping when microphone stops or processing begins."""
        self._play("listening_stop")

    # --- Startup cue + quiet-context handling ---

    @property
    def startup_enabled(self) -> bool:
        return self._startup_enabled

    @startup_enabled.setter
    def startup_enabled(self, val: bool):
        self._startup_enabled = bool(val)
        self._save_settings()

    # Quiet-hours bounds read by _is_quiet_context(). They live in the shared
    # app-settings store (memory.config_manager) because that is what the
    # helper consults on every boot; these accessors let the Settings page
    # write the very same keys instead of a parallel copy.
    @property
    def startup_quiet_start_hour(self) -> int:
        try:
            from memory.config_manager import get_setting
            return int(get_setting("startup_quiet_start_hour", 22))
        except Exception:
            return 22

    @startup_quiet_start_hour.setter
    def startup_quiet_start_hour(self, val: int):
        try:
            from memory.config_manager import set_setting
            set_setting("startup_quiet_start_hour", max(0, min(23, int(val))))
        except Exception:
            pass

    @property
    def startup_quiet_end_hour(self) -> int:
        try:
            from memory.config_manager import get_setting
            return int(get_setting("startup_quiet_end_hour", 7))
        except Exception:
            return 7

    @startup_quiet_end_hour.setter
    def startup_quiet_end_hour(self, val: int):
        try:
            from memory.config_manager import set_setting
            set_setting("startup_quiet_end_hour", max(0, min(23, int(val))))
        except Exception:
            pass

    def _is_quiet_context(self) -> bool:
        """True late at night or while Do Not Disturb is on - the startup cue
        then plays at a whisper instead of startling anyone. A meeting is never
        active at boot, so it is not checked here."""
        try:
            import datetime
            hour = datetime.datetime.now().hour
            # configurable window, default 22:00-07:00
            try:
                from memory.config_manager import get_setting
                start_h = int(get_setting("startup_quiet_start_hour", 22))
                end_h = int(get_setting("startup_quiet_end_hour", 7))
            except Exception:
                start_h, end_h = 22, 7
            if start_h <= end_h:
                late = start_h <= hour < end_h
            else:
                late = (hour >= start_h) or (hour < end_h)
            if late:
                return True
        except Exception:
            pass
        try:
            from memory.config_manager import get_setting
            if bool(get_setting("do_not_disturb", False)):
                return True
        except Exception:
            pass
        return False

    def play_dock_whoosh(self):
        """The gentle whoosh as the orb travels to its dock. Uses the designed
        dock_whoosh.wav when present, otherwise falls back to the deploy
        whoosh so the handoff is never silent."""
        if not self._enabled:
            return
        name = "dock_whoosh" if "dock_whoosh" in self._effects else "deploy_whoosh"
        self._play(name)

    def play_startup_ready(self):
        """The signature READY chime, fired once when boot actually hands off.
        Respects the global mute, the dedicated startup toggle, and ducks hard
        in a quiet context rather than going fully silent."""
        if not self._enabled or not self._startup_enabled:
            return
        eff = self._effects.get("startup_ready")
        if not eff:
            return
        vol = self._volume
        if self._is_quiet_context():
            vol *= 0.25
        try:
            if eff.isPlaying():
                eff.stop()
            eff.setVolume(max(0.0, min(1.0, vol)))
            eff.play()
        except Exception:
            pass

    def play_ready_chime(self):
        """The warm READY/listening bell, fired when the orb opens its ears. Uses
        the designed ready_chime.wav, falling back to the listening ping so the
        handoff is never silent on a fresh checkout."""
        if not self._enabled:
            return
        name = "ready_chime" if "ready_chime" in self._effects else "listening_start"
        self._play(name)

    def play_error(self):
        """The low falling sting when the orb drops into its error state - the
        audible 'that didn't work'. Respects the global mute."""
        if not self._enabled:
            return
        name = "error" if "error" in self._effects else "listening_stop"
        self._play(name)

    def play_notification(self):
        """The short bright tick that plays as a notification toast appears.
        Respects the global mute; the toast itself is already suppressed under
        DND, so gating playback to the visible toast keeps DND quiet."""
        if not self._enabled:
            return
        name = "notification" if "notification" in self._effects else "telemetry_chirp"
        self._play(name)

    def play_mute(self, muted: bool):
        """The crisp toggle click confirming a deliberate mute / unmute. Going
        quiet uses the softer mute click, resuming the brighter unmute click; on
        a fresh checkout without the designed cues they fall back to the matching
        listening ping so the toggle is never silent."""
        if not self._enabled:
            return
        if muted:
            name = "mute" if "mute" in self._effects else "listening_stop"
        else:
            name = "unmute" if "unmute" in self._effects else "listening_start"
        self._play(name)


sound_mgr = SoundManager.instance()