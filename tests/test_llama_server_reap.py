"""core/llama_server.reap_orphans: leftover servers must die, strangers must not.

Saturn starts llama-server as a child and stops it on a clean exit, so a server
already running from our own binary at boot belongs to a previous run that died
before reaching ``stop()``. Those leftovers are why a 4 GB card silently ran out
of memory: three were found holding ~1.2 GB of RAM each, and only one of them even
held the API port - so a port check alone would never have found the other two.

Nothing here touches a real process. A fake ``psutil`` is injected into
``sys.modules``, which the deferred ``import psutil`` inside the function picks up.
"""

import builtins
import os
import sys
from pathlib import Path

from core import llama_server as ls

OUR_BIN = str(ls.SERVER_BIN)


class FakePsutil:
    """Minimal stand-in exposing only the surface ``reap_orphans`` uses.

    Behaviour is driven by per-process spec flags so each test declares intent
    instead of repatching methods:

    ``exe``            path reported by exe(); None with ``exe_raises``
    ``exe_raises``      exe() fails, so ownership cannot be established
    ``term_raises``     terminate() itself blows up
    ``ignores_term``    terminate() is acknowledged but the process will not exit
    ``vanishes``        the process disappears while being waited on
    """

    class NoSuchProcess(Exception):
        pass

    class TimeoutExpired(Exception):
        def __init__(self, pid):
            super().__init__(f"pid {pid} did not exit")
            self.pid = pid

    def __init__(self, procs):
        self.procs = procs

    def process_iter(self, attrs):
        return [self._Process(spec) for spec in self.procs]

    class _Placeholder:
        """Stands in for psutil.Process(os.getpid()), used only to compare pids."""

        def __init__(self, pid):
            self.pid = pid

    class _Process:
        def __init__(self, spec):
            self._spec = spec
            self.pid = spec["pid"]
            self.info = {"pid": spec["pid"], "name": spec.get("name", "llama-server.exe")}
            self.actions = spec.setdefault("actions", [])

        def exe(self):
            if self._spec.get("exe_raises"):
                raise PermissionError("access denied")
            return self._spec["exe"]

        def terminate(self):
            self.actions.append("terminate")
            if self._spec.get("term_raises"):
                raise OSError("device not ready")

        def wait(self, timeout=None):
            if self._spec.get("vanishes"):
                raise FakePsutil.NoSuchProcess()
            self.actions.append("wait")
            if self._spec.get("ignores_term") and "kill" not in self.actions:
                raise FakePsutil.TimeoutExpired(self.pid)

        def kill(self):
            self.actions.append("kill")

    def Process(self, pid):
        return self._Placeholder(pid)


def _install(monkeypatch, procs):
    """Route the function's deferred ``import psutil`` to a fake."""
    monkeypatch.setitem(sys.modules, "psutil", FakePsutil(procs))
    # Zero the post-reap settle so the suite stays fast.
    monkeypatch.setattr(ls, "REAP_SETTLE_SECONDS", 0.0)


def _spec(pid, exe, **flags):
    return {"pid": pid, "exe": exe, **flags}


# ---------------------------------------------------------------------------
# the safety boundary: which process counts as ours
# ---------------------------------------------------------------------------

def test_our_own_binary_is_reaped(monkeypatch):
    proc = _spec(5001, OUR_BIN)
    _install(monkeypatch, [proc])
    assert ls.reap_orphans() == [5001]
    assert proc["actions"] == ["terminate", "wait"]


def test_a_stranger_llama_server_is_left_alone(monkeypatch):
    """Same executable *name* from a different build is not ours. Killing a llama
    server the user runs for another project would be a serious overreach."""
    other = str(Path(ls.BASE_DIR, "other-checkout", "llama-server.exe"))
    proc = _spec(5002, other)
    _install(monkeypatch, [proc])
    assert ls.reap_orphans() == []
    assert proc["actions"] == []


def test_unreadable_executable_is_never_killed(monkeypatch):
    """If the path cannot be read, ownership is unknown - and unknown must mean
    leave it alone rather than guess and kill."""
    proc = _spec(5003, None, exe_raises=True)
    _install(monkeypatch, [proc])
    assert ls.reap_orphans() == []
    assert proc["actions"] == []


def test_own_pid_is_skipped(monkeypatch):
    """Defensive: if this process ever *is* a llama-server from our binary (a
    frozen single-file build), the reap must not be suicide."""
    mine = _spec(os.getpid(), OUR_BIN)
    _install(monkeypatch, [mine])
    assert ls.reap_orphans() == []
    assert mine["actions"] == []


def test_path_compare_ignores_windows_casing_separators_and_relative_forms(monkeypatch):
    """Measured: comparing ``exe()`` with a plain ``==`` against ``str(SERVER_BIN)``
    reaped nothing at all once the reported path differed in casing, in separator
    style, or by being expressed relative to the cwd - the reap silently became a
    no-op, which is exactly the failure mode that let three orphans pile up.
    Resolving both sides fixes it."""
    twisted = _spec(5004, OUR_BIN.replace("\\", "/").upper())
    # Derived from the real cwd rather than hardcoded, so the assertion does not
    # only hold when the suite happens to run from the repo root.
    relative = _spec(5005, Path(os.path.relpath(OUR_BIN)).as_posix())
    _install(monkeypatch, [twisted, relative])
    assert sorted(ls.reap_orphans()) == [5004, 5005]


def test_mixed_population_reaps_only_ours(monkeypatch):
    ours = _spec(5010, OUR_BIN)
    stranger = _spec(5011, str(Path(ls.BASE_DIR, "elsewhere", "llama-server.exe")))
    unknown = _spec(5012, None, exe_raises=True)
    _install(monkeypatch, [stranger, ours, unknown])
    assert ls.reap_orphans() == [5010]
    assert stranger["actions"] == [] and unknown["actions"] == []


# ---------------------------------------------------------------------------
# termination mechanics
# ---------------------------------------------------------------------------

def test_kill_escalates_when_terminate_is_ignored(monkeypatch):
    """A stuck server acknowledges SIGTERM and then does not exit. Abandoning it
    would leave the VRAM held, which is the whole problem being fixed."""
    proc = _spec(5005, OUR_BIN, ignores_term=True)
    _install(monkeypatch, [proc])
    assert ls.reap_orphans() == [5005]
    assert proc["actions"] == ["terminate", "wait", "kill", "wait"]


def test_a_process_that_vanishes_mid_sweep_is_not_an_error(monkeypatch):
    """Orphans exit on their own constantly; that is a success path, not a
    reason to abort the remaining sweep."""
    gone = _spec(5006, OUR_BIN, vanishes=True)
    still = _spec(5007, OUR_BIN)
    _install(monkeypatch, [gone, still])
    assert ls.reap_orphans() == [5007]
    assert gone["actions"] == ["terminate"]


def test_one_failure_does_not_abort_the_sweep(monkeypatch):
    """A permission error on one pid must not leave the next orphan holding the
    GPU."""
    bad = _spec(5008, OUR_BIN, term_raises=True)
    good = _spec(5009, OUR_BIN)
    _install(monkeypatch, [bad, good])
    assert ls.reap_orphans() == [5009]


def test_missing_psutil_degrades_to_no_reaping(monkeypatch):
    """The reap is an optimisation, not a precondition for booting - and never a
    reason to crash Saturn."""
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(ls, "REAP_SETTLE_SECONDS", 0.0)
    real_import = builtins.__import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    assert ls.reap_orphans() == []


# ---------------------------------------------------------------------------
# the recorded Windows pitfall
# ---------------------------------------------------------------------------

def test_os_kill_is_never_used_as_a_probe(monkeypatch):
    """``os.kill(pid, 0)`` is the usual cheap liveness check, but on Windows
    signal 0 is not a no-op - it can terminate the target. This function exists to
    kill processes, so guard against regressing into that pattern for probing."""
    _install(monkeypatch, [_spec(5013, OUR_BIN)])

    def _forbidden(*args, **kwargs):
        raise AssertionError("os.kill must not be used to probe liveness on Windows")

    monkeypatch.setattr(os, "kill", _forbidden)
    assert ls.reap_orphans() == [5013]
