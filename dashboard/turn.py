"""A command from the phone, taken down the same road as one from the keyboard.

WHY THIS EXISTS
    ``dashboard/server.py`` had grown its own copy of the action ladder: an
    ``open_app`` branch, a ``media_control`` branch with its own if-chain, and
    nothing else. So "open Spotify" worked at the keyboard and, from the phone,
    took a different road with different rules - and every handler added since
    (office, commerce, system) was simply unreachable from the phone.

WHAT A HANDLER NEEDS, AND WHAT A PHONE HAS
    A handler receives ``ctx.app`` and may speak through it or touch its UI.
    There is no desktop window inside a phone turn, and reaching for one from
    the dashboard's thread would put Qt on the wrong thread. So this module
    supplies the smallest honest stand-in: ``speak`` goes out through the
    phone's own voice, and the UI methods that only *narrate* work become
    no-ops.

    Anything else is deliberately missing rather than faked. A handler that
    needs the desktop window raises ``AttributeError``, the router catches it
    and lets the turn fall through to the live session - exactly what happens
    on the desktop when a handler declines. The alternative, a shim that
    answers every attribute with a no-op callable, would make ``has_ui()``
    lie and let a handler believe it had captured a screen it never saw.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class PhoneUI:
    """The two slices of the desktop UI that narration touches, and nothing else.

    ``write_log`` is what a launcher or a handler uses to leave a trace, and it
    exists so ``ctx.log()`` works. The task-workspace calls exist because the
    handlers wrap their narration in them; on a phone there is no workspace, so
    the call does nothing and the command still runs.

    ``has_ui()`` stays honest as a result: a method that is not defined here is
    a capability the phone does not have, so a handler that needs the desktop
    screen skips it instead of pretending.
    """

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        self._log = log

    def write_log(self, message: Any = "", *args: Any, **kwargs: Any) -> None:
        if self._log and message:
            self._log(str(message))

    def update_task_workspace(self, *args: Any, **kwargs: Any) -> None:
        """There is no task workspace on a phone. The command still runs."""

    def finish_task_workspace(self, *args: Any, **kwargs: Any) -> None:
        """There is nothing to finish on a phone."""


class PhoneTurn:
    """``ctx.app`` for a turn that arrived over the dashboard.

    ``say`` delivers a sentence to the person who asked, and is normally the
    dashboard's live voice engine - which is the whole point of not handing the
    handler the desktop window: the answer belongs to the phone it came from.

    The window is not available, on purpose. See the module docstring.
    """

    def __init__(self,
                 say: Optional[Callable[[str], None]] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self._say = say
        self._log = log
        self.ui = PhoneUI(log)

    def speak(self, message: Any) -> None:
        text = str(message or "").strip()
        if not text:
            return
        if self._log:
            self._log(f"Saturn: {text}")
        if self._say is None:
            return
        try:
            self._say(text)
        except Exception as exc:
            # A dead voice engine must not kill the handler that just succeeded.
            if self._log:
                self._log(f"speak failed: {exc}")
