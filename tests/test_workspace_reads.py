"""The inbox and the calendar: read them from anywhere, write them from nowhere.

Gmail and Calendar checks were an inline ladder in ``main.py``, after the
router - so like the document factories before them, they worked at the
keyboard and nowhere else. They are a Tier-1 intent with a handler now.

The asymmetry is deliberate and is the heart of this file: a *read* may be
claimed by a 2ms regex, a *write* may not. Sending the wrong email cannot be
undone by running the command again.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.action_router import dispatch_actions, has_handler
from core.intent_dispatcher import IntentDispatcher

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"


@pytest.fixture
def dispatcher(tmp_path: Path):
    return IntentDispatcher(cache_file=tmp_path / "intent_cache.json")


@pytest.fixture
def stub_workspace(monkeypatch):
    """Records what the handler asked Google for, and answers like the real one."""
    import actions.google_workspace_mcp as module

    calls = []

    def _fake(parameters, response=None, player=None, speak=None, session_memory=None):
        calls.append((dict(parameters), speak, player))
        return "3 unread messages."

    monkeypatch.setattr(module, "google_workspace", _fake)
    return calls


# ── Reading is claimed, at no cost ─────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "check my email",
        "check inbox",
        "any unread emails",
        "open my inbox",
    ],
)
def test_an_inbox_check_is_claimed(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result.intent == "google_workspace"
    assert result.action == "check_email"
    assert result.tier == 1


@pytest.mark.parametrize(
    "text",
    [
        "what's on my calendar",
        "check calendar",
        "any meetings today",
        "show me today's events",
    ],
)
def test_a_schedule_check_is_claimed(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result.intent == "google_workspace"
    assert result.action == "check_calendar"
    assert result.tier == 1


# ── Writing is not ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "send an email to mom",
        "reply to that email",
        "forward my inbox to Sam",
        "draft an email about the roof",
        "create a meeting with the team",
        "add to my calendar tomorrow",
        "cancel my meetings",
        "reschedule today's events",
        "invite Sam to my calendar",
    ],
)
def test_a_write_is_left_to_the_model(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result is None or result.intent != "google_workspace", text


# ── The handler ────────────────────────────────────────────────────────────

class UI:
    def __init__(self):
        self.began = None
        self.updated = []
        self.log = []

    def write_log(self, message=""):
        self.log.append(str(message))

    def begin_task_workspace(self, title, steps, source=None):
        self.began = (title, list(steps), source)

    def update_task_workspace(self, **kwargs):
        self.updated.append(kwargs)


class App:
    def __init__(self):
        self.ui = UI()
        self.said = []

    def speak(self, message):
        self.said.append(message)


class Intent:
    def __init__(self, action):
        self.intent = "google_workspace"
        self.action = action
        self.parameters = {}
        self.voice_ack = ""


def _run(action: str):
    """Dispatch one turn and wait for the worker to finish."""
    import time

    app = App()
    handled = dispatch_actions("check it", Intent(action), app, source="dashboard")

    for _ in range(60):
        if app.ui.updated:
            break
        time.sleep(0.02)

    return app, handled


def test_the_inbox_is_read_through_the_action_module(stub_workspace):
    app, handled = _run("check_email")

    assert handled is True
    assert app.said[0] == "Checking your Gmail inbox, sir..."
    assert app.ui.began[1] == [
        "Connecting to Gmail",
        "Fetching unread messages",
        "Parsing headers & body",
        "Summarizing inbox",
    ]
    assert stub_workspace == [({"service": "gmail", "action": "unread"}, None, None)]
    assert app.ui.updated[-1]["status"] == "Email Check Completed"
    assert app.ui.updated[-1]["percent"] == 100
    assert app.said[-1] == "Here are your latest emails, sir."


def test_the_schedule_is_read_through_the_action_module(stub_workspace):
    app, handled = _run("check_calendar")

    assert handled is True
    assert app.said[0] == "Checking your calendar schedule..."
    assert stub_workspace == [({"service": "calendar", "action": "list"}, None, None)]
    assert app.ui.updated[-1]["status"] == "Schedule Retrieved"
    assert app.said[-1] == "Here is your schedule, sir."


def test_it_says_its_one_acknowledgement_only_once(stub_workspace):
    app, _ = _run("check_email")

    # The action module speaks too ("Checking your unread emails..."); passing
    # speak=None is what keeps the user from hearing both.
    assert stub_workspace[0][1] is None
    assert app.said.count("Checking your Gmail inbox, sir...") == 1


def test_a_refusal_is_reported_rather_than_swallowed(monkeypatch):
    import actions.google_workspace_mcp as module

    def _boom(*args, **kwargs):
        raise RuntimeError("credentials missing")

    monkeypatch.setattr(module, "google_workspace", _boom)
    app, handled = _run("check_calendar")

    assert handled is True
    assert app.ui.updated[-1]["percent"] == 0
    assert app.said[-1] == "There was an issue checking your schedule, sir."
    assert any("credentials missing" in line for line in app.ui.log)


def test_an_action_it_cannot_serve_is_declined():
    app, handled = _run("send_email")

    assert handled is False
    assert app.said == []


def test_the_intent_is_dispatchable():
    assert has_handler("google_workspace")


# ── The regression: a door with no model behind it ─────────────────────────

def test_the_phone_can_check_the_inbox(monkeypatch, dispatcher):
    monkeypatch.setattr("core.omni_planner.OmniPlanner.is_composite_goal", lambda text: False)
    monkeypatch.setattr(
        "core.memory_cortex.MemoryCortex.get_instance", classmethod(lambda cls: MagicMock())
    )

    result = dispatcher.resolve("check my email", enable_ai_tier=False)

    assert result.tier == 1
    assert result.intent == "google_workspace"
    assert result.action == "check_email"


# ── And main.py keeps no copy ──────────────────────────────────────────────

def test_main_has_no_gmail_ladder_left():
    source = MAIN.read_text(encoding="utf-8")

    for forgotten in (
        "is_email_check",
        "is_calendar_check",
        "_run_gw_email",
        "_run_gw_calendar",
        "Email Check Completed",
        "Schedule Retrieved",
    ):
        assert forgotten not in source, forgotten
