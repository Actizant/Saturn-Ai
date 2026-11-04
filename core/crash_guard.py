"""
core/crash_guard.py
===================
Process-wide crash diagnostics and Qt-slot hardening for Saturn.

Why this exists
---------------
The desktop app was dying silently on Windows with two native fault codes:

* ``0xC0000409`` (fast-fail inside ``Qt6Core.dll``) - PyQt calls ``qFatal``
  when a connected slot raises an uncaught exception, which aborts the whole
  process.
* ``0xC0000374`` (heap corruption inside ``ntdll.dll``) - a widget is being
  touched from a worker thread.

Both bypass Python's normal ``try/except`` and left no ``FATAL_CRASH.log``,
so there was nothing to diagnose. This module makes those failures
observable and survivable:

1. ``faulthandler`` dumps every thread's Python stack when the process dies
   from a *native* fault. Without it a hard crash leaves zero trace.
2. ``sys.excepthook`` / ``threading.excepthook`` record uncaught Python
   errors and hand them to the existing auto-heal monitor.
3. ``guarded()`` wraps a Qt slot so a bad payload is logged instead of
   calling ``qFatal`` and taking the app down.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

_installed = False
_install_lock = threading.Lock()
_faulthandler_stream = None  # keep the file object open for process lifetime


def _base_dir() -> Path:
    try:
        from core.user_paths import get_user_data_dir

        return Path(get_user_data_dir())
    except Exception:
        return Path(__file__).resolve().parent.parent


def native_log_path() -> Path:
    """Human-readable log for native faults and guarded-slot errors."""
    return _base_dir() / "FATAL_NATIVE.log"


def _crash_signal_path() -> Path:
    """Lives where ``_terminal_crash_monitor`` in main.py already looks."""
    return _base_dir() / "config" / "crash_signal.json"


def _append(path: Path, header: str, body: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write("\n===== %s | %s =====\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), header))
            f.write((body or "").rstrip() + "\n")
    except Exception:
        pass


def _record(header: str, body: str) -> None:
    """Persist an error and nudge the auto-heal monitor if it is running."""
    _append(native_log_path(), header, body)
    try:
        path = _crash_signal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"traceback": body or "", "kind": header}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _record("UNCAUGHT[%s]" % getattr(exc_type, "__name__", "Exception"), text)
    try:
        sys.__stderr__.write(text)
        sys.__stderr__.flush()
    except Exception:
        pass


def _thread_excepthook(args) -> None:
    if getattr(args, "exc_type", None) is SystemExit:
        return
    text = "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
    )
    name = getattr(getattr(args, "thread", None), "name", "?")
    _record("THREAD[%s]" % name, text)
    try:
        sys.__stderr__.write(text)
        sys.__stderr__.flush()
    except Exception:
        pass


def install_crash_logging() -> Optional[Path]:
    """Install native-fault capture and Python exception hooks (idempotent).

    Safe to call before heavy imports: it only touches the standard library
    and a lazily-imported path helper.
    """
    global _installed, _faulthandler_stream
    with _install_lock:
        if _installed:
            return native_log_path()
        _installed = True

        try:
            path = native_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _faulthandler_stream = open(path, "a", encoding="utf-8", errors="replace")
            import faulthandler

            faulthandler.enable(file=_faulthandler_stream, all_threads=True)
        except Exception:
            _faulthandler_stream = None

        sys.excepthook = _excepthook
        try:
            threading.excepthook = _thread_excepthook
        except Exception:
            pass

        _append(
            native_log_path(),
            "RUNTIME",
            "python=%s\nexe=%s\ncwd=%s\npid=%s"
            % (sys.version.split()[0], sys.executable, os.getcwd(), os.getpid()),
        )
        return native_log_path()


def guarded(fn: Callable[..., Any], name: str = "") -> Callable[..., Any]:
    """Return a copy of ``fn`` that can never abort a Qt slot.

    PyQt turns an uncaught exception in a connected slot into ``qFatal``
    (``0xC0000409``). Wrapping the slot keeps the error visible in the log
    while the application stays alive.
    """
    label = name or getattr(fn, "__name__", "slot")

    def _wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            _record("SLOT[%s]" % label, traceback.format_exc())
            return None

    try:
        _wrapper.__name__ = getattr(fn, "__name__", "guarded_slot")
        _wrapper.__doc__ = getattr(fn, "__doc__", None)
    except Exception:
        pass
    return _wrapper
