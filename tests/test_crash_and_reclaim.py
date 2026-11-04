"""Phase 0 stabilization: crash-guard hardening + tiered memory reclaim.

Covers the two native crash classes found in the Windows event log:

* ``0xC0000409`` - PyQt aborts when a connected slot raises, so ``guarded``
  must swallow slot exceptions instead of letting them reach ``qFatal``.
* RAM creep - the watcher must trim softly, purge hard, and reclaim idle
  resources so an overnight session cannot climb until it crashes.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import time

import core.anticipatory_watcher as aw
import core.crash_guard as cg
from core.anticipatory_watcher import AnticipatoryWatcher
from core.crash_guard import guarded


# ---------------------------------------------------------------------------
# crash guard
# ---------------------------------------------------------------------------

def test_guarded_swallows_slot_exceptions(monkeypatch):
    """An exception inside a Qt slot must be logged, never abort the process."""
    # Redirect the recorder so a deliberate failure never lands in the real
    # forensic log the running app depends on.
    monkeypatch.setattr(cg, "_record", lambda header, body: None)

    def boom(*_args, **_kwargs):
        raise RuntimeError("slot blew up")

    wrapped = guarded(boom, "boom")
    assert wrapped("payload") is None


def test_guarded_passes_through_success():
    assert guarded(lambda a, b: a + b)(2, 3) == 5


def test_install_crash_logging_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "native_log_path", lambda: tmp_path / "FATAL_NATIVE.log")
    monkeypatch.setattr(cg, "_crash_signal_path", lambda: tmp_path / "crash_signal.json")
    monkeypatch.setattr(cg, "_installed", False)

    first = cg.install_crash_logging()
    second = cg.install_crash_logging()

    assert first == second
    assert (tmp_path / "FATAL_NATIVE.log").exists()


# ---------------------------------------------------------------------------
# tiered memory reclaim
# ---------------------------------------------------------------------------

def _make_watcher(monkeypatch):
    watcher = AnticipatoryWatcher(check_interval_sec=0.1, cooldown_sec=0.0)
    # Skip the calendar (60s) and network (30s) probes so these tests only
    # exercise the memory tiers.
    watcher._last_calendar_check = time.time()
    watcher._last_network_check = time.time()
    try:
        from core.game_sentinel import GameSentinelManager

        stub = SimpleNamespace(is_game_mode_active=True, scan_for_game=lambda: None)
        monkeypatch.setattr(GameSentinelManager, "get_instance", staticmethod(lambda: stub))
    except Exception:
        pass
    return watcher


def _run_cycle(watcher, monkeypatch, percent, idle_seconds=None):
    monkeypatch.setattr(aw.psutil, "virtual_memory", lambda: SimpleNamespace(percent=percent))
    monkeypatch.setattr(
        watcher,
        "_user_idle_seconds",
        lambda: idle_seconds,
    )
    with patch("core.anticipatory_watcher.datetime", wraps=datetime) as mock_dt:
        mock_dt.now.return_value = datetime(2026, 10, 30, 14, 0, 0)
        return watcher.run_inspection_cycle()


def test_soft_ram_tier_trims_quietly(monkeypatch):
    watcher = _make_watcher(monkeypatch)
    watcher.ram_soft_threshold_percent = 72.0
    watcher.ram_threshold_percent = 88.0
    watcher.soft_trim_cooldown_sec = 0.0

    nudges = []
    watcher.set_nudge_callback(lambda msg, cat: nudges.append((msg, cat)))
    releases = []
    monkeypatch.setattr(watcher, "_release_idle_resources", lambda force=False: releases.append(force))

    clean = MagicMock(return_value={"freed_mb": 0.0})
    monkeypatch.setattr(aw.MemReductEngine, "clean_ram", clean)

    result = _run_cycle(watcher, monkeypatch, percent=78.0)

    clean.assert_called_once_with(purge_standby=False, empty_working_sets=True)
    assert releases == []
    assert nudges == []
    assert any(c.get("action") == "soft_trim" for c in result["checks"])


def test_hard_ram_tier_purges_and_nudges(monkeypatch):
    watcher = _make_watcher(monkeypatch)
    watcher.ram_soft_threshold_percent = 72.0
    watcher.ram_threshold_percent = 88.0
    watcher._last_ram_nudge = 0.0

    nudges = []
    watcher.set_nudge_callback(lambda msg, cat: nudges.append((msg, cat)))
    releases = []
    monkeypatch.setattr(watcher, "_release_idle_resources", lambda force=False: releases.append(force))

    clean = MagicMock(return_value={"freed_mb": 640.0})
    monkeypatch.setattr(aw.MemReductEngine, "clean_ram", clean)

    _run_cycle(watcher, monkeypatch, percent=91.0)

    clean.assert_called_once_with()  # full native purge, no kwargs
    assert releases == [False]
    assert len(nudges) == 1
    assert nudges[0][1] == "memory"


def test_idle_reclaim_closes_resources(monkeypatch):
    watcher = _make_watcher(monkeypatch)
    watcher.idle_evict_seconds = 600.0
    watcher.idle_evict_cooldown_sec = 0.0
    watcher._last_idle_evict = 0.0

    releases = []
    monkeypatch.setattr(watcher, "_release_idle_resources", lambda force=False: releases.append(force))

    clean = MagicMock(return_value={"freed_mb": 0.0})
    monkeypatch.setattr(aw.MemReductEngine, "clean_ram", clean)

    result = _run_cycle(watcher, monkeypatch, percent=40.0, idle_seconds=1200.0)

    assert releases == [True]
    clean.assert_called_once_with(purge_standby=False, empty_working_sets=True)
    assert any(c.get("type") == "idle" for c in result["checks"])
