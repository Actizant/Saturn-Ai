"""A command from the phone takes the desktop's road, and no other.

The dashboard used to carry its own two branches; the point of these tests is
that the seam now runs real router handlers, that the answer comes back through
the phone's own voice, and that a handler which needs the desktop window
declines instead of half-working.
"""

from pathlib import Path

import pytest

from agent import action_router
from agent.action_router import dispatch_actions
from core import trust_gate
from dashboard.turn import PhoneTurn, PhoneUI

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "dashboard" / "server.py"


class Intent:
    def __init__(self, name, action="", parameters=None, voice_ack=""):
        self.intent = name
        self.action = action
        self.parameters = parameters or {}
        self.voice_ack = voice_ack


def _probe(ctx):
    ctx.speak("spoken from the phone")
    ctx.log("probe logged")
    ctx.ui_call("update_task_workspace", title="not on a phone")
    return True


@pytest.fixture
def probe():
    """Borrow the router for one test, then leave the registry as it was."""
    name = "__phone_turn_probe"
    action_router._HANDLERS[name] = _probe
    try:
        yield name
    finally:
        action_router._HANDLERS.pop(name, None)


def _turn():
    spoken, logged = [], []
    return PhoneTurn(spoken.append, logged.append), spoken, logged


# ── The stand-in an app handler receives ────────────────────────────────────

def test_a_handler_speaks_through_the_phone(probe):
    app, spoken, logged = _turn()

    handled = dispatch_actions("probe this", Intent(probe), app, source="dashboard")

    assert handled is True
    assert spoken == ["spoken from the phone"]
    assert "probe logged" in logged


def test_the_answer_is_logged_before_it_is_spoken():
    app, spoken, logged = _turn()

    app.speak("Opening Spotify, sir.")

    assert logged[0] == "Saturn: Opening Spotify, sir."
    assert spoken == ["Opening Spotify, sir."]


def test_without_a_voice_the_answer_is_still_logged():
    logged = []
    PhoneTurn(None, logged.append).speak("Done.")

    assert logged == ["Saturn: Done."]


def test_a_broken_voice_does_not_kill_the_turn():
    logged = []

    def _dead(_text):
        raise RuntimeError("engine offline")

    PhoneTurn(_dead, logged.append).speak("Done.")

    assert any("speak failed" in line for line in logged)


def test_the_ui_stand_in_stays_honest_about_what_a_phone_can_do():
    ui = PhoneUI()

    assert callable(ui.write_log)
    # A phone has no task workspace and no desktop screen to capture.
    assert not hasattr(ui, "capture_screen_bytes")


def test_narration_that_needs_a_workspace_is_a_no_op_not_a_crash():
    app, spoken, logged = _turn()

    app.ui.update_task_workspace(title="Screen Analysis", percent=40)
    app.ui.finish_task_workspace("done")
    app.ui.write_log("a line")

    assert logged == ["a line"]
    assert spoken == []


def test_a_handler_that_needs_the_desktop_window_declines():
    app, spoken, _ = _turn()

    handled = dispatch_actions("pair my phone", Intent("mobile_connect"), app,
                               source="dashboard")

    assert handled is False
    assert spoken == []


# ── The gate applies to a phone turn too ────────────────────────────────────

def test_a_stranger_at_the_phone_is_refused_out_loud(monkeypatch, probe):
    def _deny(capability, **kwargs):
        return trust_gate.Decision(allowed=False, capability=capability,
                                   trust=trust_gate.Trust.UNKNOWN, verified=True,
                                   policy_allowed=False,
                                   spoken="That isn't yours to approve.")

    monkeypatch.setattr("core.trust_gate.evaluate", _deny)
    app, spoken, _ = _turn()

    handled = dispatch_actions("probe this", Intent(probe), app, source="dashboard")

    # The refusal is the answer: falling through would let the session do
    # exactly what the gate just refused.
    assert handled is True
    assert spoken == ["That isn't yours to approve."]


# ── The dashboard's own copy of the ladder is gone ──────────────────────────

def test_the_dashboard_calls_the_router_instead_of_its_own_branches():
    source = SERVER.read_text(encoding="utf-8")

    assert "dispatch_actions(" in source
    assert 'source="dashboard"' in source
    assert 'intent_res.intent == "media_control"' not in source
    assert 'intent_res.intent == "open_app"' not in source
    assert "from actions import media_keys" not in source
