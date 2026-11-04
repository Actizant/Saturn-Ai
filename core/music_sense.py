"""core/music_sense.py - Saturn's third ear: what is playing on your machine.

Saturn was built with two audio directions and never a third - the microphone
(to hear you) and the voice (to answer you). This module adds the missing sense:
*listening to what already comes out of your own speakers*, so Saturn can enjoy
a track alongside you. It is a Qt-free daemon, exactly like the sensorium, and
it reads two things:

  Sense A - KNOW the music. No new dependency and no audio decoding. Nearly
  every player puts the current record in its window title (Spotify:
  "Title - Artist"; a YouTube tab: "Artist - Title - YouTube"; VLC/mpv/iTunes
  similar). We scan the desktop's top-level windows with the same primitives
  core/window_context uses and parse that title. This works right now.

  Sense B - FEEL the music. The orb only truly dances to a real signal, so we
  open a WASAPI loopback of the default output and read its energy. That needs
  the small `pyaudiowpatch` wheel; when it is absent the loopback simply does
  not open and the sense degrades to A alone - Saturn still knows and reacts,
  it just stops pulsing. Loopback is opened only while something is playing and
  closed the moment it stops, so an idle machine pays nothing.

Nothing here touches Qt or the network. The audio is analysed locally and never
leaves the machine; a loopback is your own playback, not your room, so this is
quieter on privacy than the microphone. The UI drives all of it through three
callbacks handed to MusicSense: on_level(level, beat, band) at ~15 Hz while
playing, on_track(session) when the record changes, and on_stop() when playback
ends.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

# Players whose window title is "the current record", and the label we show.
# Matched case-insensitively against the process name.
_MEDIA_PROCS = {
    "spotify.exe": "Spotify",
    "itunes.exe": "iTunes",
    "music.exe": "Groove",
    "vlc.exe": "VLC",
    "mpv.exe": "mpv",
    "wmplayer.exe": "WMP",
    "foobar2000.exe": "foobar2000",
    "musicbee.exe": "MusicBee",
    "aimp.exe": "AIMP",
    "tidahome.exe": "Tidal",
    "pandorahome.exe": "Pandora",
}
# Browsers only count when the tab is unmistakably a media page.
_BROWSER_PROCS = ("chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "vivaldi.exe")
_MEDIA_TAB_HINTS = ("youtube", "music.youtube", "open.spotify", "soundcloud",
                    "music.apple", "tidal.com", "bandcamp")

# Trailing application noise to strip before splitting a title.
_APP_SUFFIXES = (" - VLC media player", " - VLC", " - mpv", " - iTunes",
                 " - Spotify", " - YouTube", " - Google Chrome", " - Microsoft Edge",
                 " - Brave", " - Groove Music", " - Music", " - Opera", " - Vivaldi",
                 " | Spotify", " - SoundCloud")

# A loopback chunk: ~50 ms at 44.1/48 kHz is responsive without costing CPU.
_BLOCKSIZE = 2048


def _clean(raw: str) -> str:
    t = (raw or "").strip()
    for suf in _APP_SUFFIXES:
        if t.lower().endswith(suf.lower()):
            t = t[: -len(suf)].strip()
    return t


def parse_title(source: str, raw: str) -> Dict[str, str]:
    """Split a window title into {title, artist}. Convention differs per app:
    Spotify/Groove read "Title - Artist"; browsers and most players read
    "Artist - Title"; a bare filename has no artist. We never invent one."""
    t = _clean(raw)
    if not t:
        return {"title": "", "artist": ""}
    # Normalise the en/em dash used by Spotify and Groove to a plain separator.
    parts = [p.strip() for p in re_split(t)]
    if source in ("Spotify", "Groove"):
        if len(parts) >= 2:
            return {"title": parts[0], "artist": parts[1]}
        return {"title": parts[0], "artist": ""}
    if len(parts) >= 2:
        # "Artist - Title" is the browser/player default; keep both honest.
        return {"artist": parts[0], "title": parts[-1] if len(parts) > 2 else parts[1]}
    return {"title": parts[0], "artist": ""}


def re_split(t: str) -> list:
    import re
    # Split on a hyphen or dash flanked by spaces, so "AC/DC - Song" stays one.
    return re.split(r"\s+[-–—]\s+", t)


def discover_session() -> Optional[Dict[str, Any]]:
    """Find the record a media app is currently showing, or None.

    Scans top-level windows (including minimised ones, because music plays in
    the background) for a known player, then for a browser tab that is clearly
    a music page. Returns {playing, title, artist, source, raw}.
    """
    try:
        from core import window_context as wc
    except Exception:
        return None
    if not getattr(wc, "user32", None):
        return None

    best = None
    for hwnd in wc._iter_top_level_windows(limit=240):
        try:
            if not wc.user32.IsWindow(hwnd):
                continue
        except Exception:
            continue
        info = wc._describe_window(hwnd)
        title = info.get("title") or ""
        if not title:
            continue
        proc = (wc.process_name_for(info.get("pid")) or "").lower()
        source = _MEDIA_PROCS.get(proc)
        if source:
            parsed = parse_title(source, title)
            if parsed["title"]:
                # A dedicated player is authoritative; take it immediately.
                return {"playing": True, "source": source,
                        "title": parsed["title"], "artist": parsed["artist"],
                        "raw": title}
        elif proc in _BROWSER_PROCS:
            low = title.lower()
            if any(h in low for h in _MEDIA_TAB_HINTS):
                parsed = parse_title("browser", title)
                if parsed["title"]:
                    best = {"playing": True, "source": "Browser",
                            "title": parsed["title"], "artist": parsed["artist"],
                            "raw": title}
    return best


class _Loopback:
    """WASAPI loopback of the default output, via pyaudiowpatch. Opens only
    while music plays; every failure is a silent 'not available', never a crash."""

    def __init__(self):
        self._pa = None
        self._stream = None
        self.rate = 0
        self.available = False
        self._probed = False

    def _probe(self):
        self._probed = True
        try:
            import pyaudiowpatch as pyaudio
            self._pa = pyaudio.PyAudio()
            # The loopback partner of the current default output device.
            for dev in self._pa.get_loopback_device_info_generator():
                self._device_index = int(dev["index"])
                self.rate = int(dev["defaultSampleRate"])
                self._channels = int(dev["maxInputChannels"])
                self.available = True
                return
        except Exception:
            self.available = False

    def open(self) -> bool:
        if not self._probed:
            self._probe()
        if not self.available or self._stream is not None:
            return self._stream is not None
        try:
            self._stream = self._pa.open(
                format=self._pa.get_format_from_width(2),
                channels=self._channels, rate=self.rate,
                input=True, input_device_index=self._device_index,
                frames_per_buffer=_BLOCKSIZE)
            return True
        except Exception:
            self._stream = None
            return False

    def read(self) -> Optional[bytes]:
        if self._stream is None:
            return None
        try:
            return self._stream.read(_BLOCKSIZE, exception_on_overflow=False)
        except Exception:
            return None

    def close(self):
        if self._stream is not None:
            try:
                self._stream.stop_stream(); self._stream.close()
            except Exception:
                pass
            self._stream = None

    def dispose(self):
        self.close()
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


class MusicSense:
    """The daemon. Construct with the three callbacks and call start()."""

    META_INTERVAL = 0.7      # how often to re-read the window title
    LEVEL_INTERVAL = 0.066   # ~15 Hz orb feed while playing
    SILENCE_SECONDS = 4.0    # energy floor this long == paused/stopped

    def __init__(self,
                 on_level: Optional[Callable[[float, bool, str], None]] = None,
                 on_track: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_stop: Optional[Callable[[], None]] = None,
                 enabled: bool = True):
        self.on_level = on_level
        self.on_track = on_track
        self.on_stop = on_stop
        self.enabled = enabled
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop = _Loopback()
        self._session: Optional[Dict[str, Any]] = None
        self._last_key = ""
        self._env = 0.0        # smoothed loudness envelope
        self._avg = 0.0        # long-run mean, for beat detection
        self._last_sound = 0.0
        self.level = 0.0
        self.band = "silent"

    def start(self):
        if self._running or not self.enabled:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop_run, daemon=True,
                                        name="SaturnMusicSense")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        self._loop.dispose()

    def set_enabled(self, enabled: bool):
        """Flip the sense on or off from a live setting. Turning it off stops
        the thread, closes the loopback and drops the orb feed to silence so no
        stale level is left pulsing; turning it back on restarts the daemon."""
        self.enabled = bool(enabled)
        if self.enabled:
            self.start()
        else:
            self.stop()
            self._session = None
            self._last_key = ""
            self.level = 0.0
            self.band = "silent"
            if self.on_level:
                try:
                    self.on_level(0.0, False, "silent")
                except Exception:
                    pass

    def current(self) -> Optional[Dict[str, Any]]:
        return self._session

    def _loop_run(self):
        next_meta = 0.0
        while self._running:
            now = time.monotonic()
            try:
                if now >= next_meta:
                    next_meta = now + self.META_INTERVAL
                    self._refresh_meta()
                self._sample_audio(now)
            except Exception:
                pass
            time.sleep(self.LEVEL_INTERVAL)

    def _refresh_meta(self):
        sess = discover_session()
        if not sess:
            if self._session is not None:
                self._session = None
                self._fire_stop()
            return
        key = f"{sess['source']}|{sess['title']}|{sess['artist']}"
        self._session = sess
        if key != self._last_key:
            self._last_key = key
            if self.on_track:
                try:
                    self.on_track(dict(sess))
                except Exception:
                    pass

    def _sample_audio(self, now: float):
        playing = bool(self._session)
        if not playing:
            if self._loop._stream is not None:
                self._loop.close()
            if self.level != 0.0:
                self.level = 0.0
                self.band = "silent"
                if self.on_level:
                    try:
                        self.on_level(0.0, False, "silent")
                    except Exception:
                        pass
            return
        # Open the loopback lazily the first time music is actually up.
        if self._loop._stream is None:
            if not self._loop.open():
                return  # Sense B unavailable - Sense A still drives reactions.
        data = self._loop.read()
        if data is None:
            return
        try:
            import numpy as np
        except Exception:
            return
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if arr.size == 0:
            return
        rms = float(np.sqrt(np.mean(arr * arr)))
        # Fast envelope for the orb, slow mean for a beat reference.
        self._env += (rms - self._env) * 0.5
        self._avg += (rms - self._avg) * 0.05
        level = min(1.0, self._env * 3.2)
        beat = self._env > max(0.02, self._avg * 1.35)
        if self._env > 0.012:
            self._last_sound = now
        band = ("silent" if level < 0.05 else "calm" if level < 0.28
                else "warm" if level < 0.55 else "high")
        self.level = level
        self.band = band
        if self.on_level:
            try:
                self.on_level(level, beat, band)
            except Exception:
                pass
        # A paused track keeps its title but drops to silence: treat a long
        # quiet as stopped so the orb settles and we do not claim it is playing.
        if now - self._last_sound > self.SILENCE_SECONDS and self._loop.available:
            self._fire_stop()
            self._session = None
            self._loop.close()

    def _fire_stop(self):
        self._last_key = ""
        if self.on_stop:
            try:
                self.on_stop()
            except Exception:
                pass


# Singleton + accessor, mirroring the sensorium's shape so main can start it
# once and any consumer can read the current record.
music_sense = MusicSense()


def get_music_sense() -> MusicSense:
    return music_sense
