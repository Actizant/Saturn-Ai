"""Saturn Studio -- the canvas host.

Saturn's clean-room native engine (:mod:`saturn_studio.native`) is now the only
engine the studio has: it draws the design canvas through Qt's own painter, with
no browser stack, no loopback HTTP server and no JavaScript bridge. This module
is the single door the rest of Saturn calls -- ``saturn_studio/tool.py``, the
voice layer and the agents keep using ``StudioWindow(parent=...)`` and
``get_window(parent=...)`` exactly as before and never learn that the old web
engine was ever here.

The native host lives in :mod:`saturn_studio.native.window`; this module holds
no drawing of its own -- only the re-export that keeps existing imports working.
Run standalone:
    .venv312\\Scripts\\python.exe -m saturn_studio.window
"""

from __future__ import annotations

from typing import Any, Optional

from saturn_studio.native.window import NativeStudioWindow


def StudioWindow(parent: Any = None) -> NativeStudioWindow:
    """Build the native canvas host -- the same call the old web class took.

    ``ui.py`` calls ``StudioWindow(parent=self)`` and drops the result straight
    into the centre stack; it is a ``QWidget``, so the reveal is unaffected.
    """
    return NativeStudioWindow(parent=parent)


def get_window(parent: Any = None, *, timeout: float = 30.0) -> Optional[Any]:
    """The live native canvas from whichever thread asks, or None.

    Never build a widget on a thread that owns none just to answer a tool --
    the native host keeps its own instance registry for exactly this reason.
    """
    from saturn_studio.native import window as native_window

    return native_window.get_window(parent=parent, timeout=timeout)


def main() -> int:
    """Standalone smoke entry: open the native studio in its own window."""
    from saturn_studio.native.window import main as native_main

    return native_main()


if __name__ == "__main__":
    raise SystemExit(main())
