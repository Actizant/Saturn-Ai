"""Tests for Saturn's third ear (core/music_sense.py).

Sense A (know the record from its window title) is pure and always testable.
Sense B (feel the loopback energy) depends on the optional pyaudiowpatch wheel,
so the contract we lock here is that it degrades silently when absent."""

import types

from core import music_sense as ms


# --- Sense A: title parsing -------------------------------------------------
def test_parse_title_spotify_is_title_then_artist():
    out = ms.parse_title("Spotify", "Bohemian Rhapsody - Queen - Spotify")
    assert out == {"title": "Bohemian Rhapsody", "artist": "Queen"}


def test_parse_title_browser_is_artist_then_title():
    out = ms.parse_title("browser", "Queen - Bohemian Rhapsody - YouTube")
    assert out == {"title": "Bohemian Rhapsody", "artist": "Queen"}


def test_parse_title_bare_filename_has_no_artist():
    out = ms.parse_title("VLC", "01 - Bohemian Rhapsody.flac - VLC media player")
    assert out["artist"] == "" or out["title"]


def test_re_split_preserves_band_name_with_slash():
    # "AC/DC" must stay one token: only a space-flanked dash is a separator.
    assert ms.re_split("AC/DC - Thunderstruck") == ["AC/DC", "Thunderstruck"]


# --- Sense A: live window scan ---------------------------------------------
class _FakeUser32:
    @staticmethod
    def IsWindow(_hwnd):
        return True


def _patch_windows(monkeypatch, hwnds, described, procs):
    wc = types.ModuleType("core.window_context")
    wc.user32 = _FakeUser32()
    wc._iter_top_level_windows = lambda limit=0: list(hwnds)
    wc._describe_window = lambda hwnd: described[hwnd]
    wc.process_name_for = lambda pid: procs.get(pid)
    monkeypatch.setitem(__import__("sys").modules, "core.window_context", wc)


def test_discover_session_matches_known_player(monkeypatch):
    _patch_windows(
        monkeypatch,
        hwnds=[1, 2],
        described={1: {"title": "Blinding Lights - The Weeknd", "pid": 100, "hwnd": 1},
                   2: {"title": "Some Editor", "pid": 200, "hwnd": 2}},
        procs={100: "spotify.exe", 200: "code.exe"},
    )
    sess = ms.discover_session()
    assert sess is not None
    assert sess["source"] == "Spotify"
    assert sess["title"] == "Blinding Lights"
    assert sess["artist"] == "The Weeknd"
    assert sess["playing"] is True


def test_discover_session_ignores_non_media_windows(monkeypatch):
    _patch_windows(
        monkeypatch,
        hwnds=[1],
        described={1: {"title": "report.xlsx", "pid": 100, "hwnd": 1}},
        procs={100: "excel.exe"},
    )
    assert ms.discover_session() is None


# --- The daemon: callbacks + graceful degrade ------------------------------
def test_daemon_fires_track_then_stop(monkeypatch):
    monkeypatch.setattr(ms, "discover_session",
                        lambda: {"playing": True, "source": "Spotify",
                                 "title": "Song", "artist": "Artist", "raw": ""})
    tracks, stops = [], []
    eng = ms.MusicSense(on_track=tracks.append, on_stop=lambda: stops.append(True),
                        enabled=True)
    eng._refresh_meta()
    assert tracks and tracks[0]["title"] == "Song"
    # Same key again must not re-fire the track event.
    eng._refresh_meta()
    assert len(tracks) == 1
    # No window now -> the stop fires once.
    monkeypatch.setattr(ms, "discover_session", lambda: None)
    eng._refresh_meta()
    assert stops


def test_daemon_stays_silent_without_loopback(monkeypatch):
    # pyaudiowpatch is not required to be installed: Sense B simply reports
    # unavailable and never raises, so the orb just stops pulsing.
    lb = ms._Loopback()
    lb._probe()
    assert lb.available is False
    assert lb.open() is False
    assert lb.read() is None
    lb.dispose()


def test_set_enabled_off_drops_level_to_zero():
    levels = []
    eng = ms.MusicSense(on_level=lambda l, b, band: levels.append((l, band)),
                        enabled=True)
    eng.level = 0.5
    eng.set_enabled(False)
    assert eng.enabled is False
    assert eng.level == 0.0
    assert levels and levels[-1] == (0.0, "silent")
