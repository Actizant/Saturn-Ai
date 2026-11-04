"""One tool: where has the user's attention actually been?

The journal it reads keeps window titles in memory only, so this answers from a
record rather than from a guess. When there is no record it says so instead of
describing a day it did not see.
"""

from __future__ import annotations

from core.focus_journal import get_focus_journal

# Long enough to see a work session, short enough to stay one sentence.
_DEFAULT_MINUTES = 60
_MAX_MINUTES = 720


def _minutes(parameters: dict) -> float:
    raw = (parameters or {}).get("minutes")
    if raw is None:
        raw = (parameters or {}).get("since_minutes")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(_DEFAULT_MINUTES)
    return max(1.0, min(float(_MAX_MINUTES), value))


def what_have_i_been_doing(parameters: dict, player=None) -> str:
    minutes = _minutes(parameters)
    journal = get_focus_journal()
    if not journal.running:
        # Reading a journal that nobody filled would describe an empty hour as
        # if it were a real one.
        if not journal.segments(minutes):
            return ("Focus tracking isn't running, so I can't tell you where the "
                    "time went. It starts with Saturn.")
    return journal.summary(minutes)


def focus_breakdown(parameters: dict, player=None) -> list:
    """Raw segments, newest first, for a caller that wants to plot them."""
    return get_focus_journal().recent(_minutes(parameters), limit=20)
