"""The machine's own vitals: read anywhere, change by a guest, end by the owner.

RAM, battery, brightness and process termination were an inline ladder in
``main.py``, after the router and behind a bare ``return`` - so they answered at
the keyboard and nowhere else. They are three Tier-1 intents now, split by how
much they can cost: reading is harmless, dimming the screen is one keypress from
where you started, and ``kill chrome`` takes whatever was unsaved with it.
"""

import time
from pathlib import Path

import pytest

import core.trust_gate as gate
from agent.action_router import dispatch_actions, has_handler
from core.intent_dispatcher import IntentDispatcher
from core.trust_gate import Evidence, Trust, evaluate

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"


@pytest.fixture
def dispatcher(tmp_path: Path):
    return IntentDispatcher(cache_file=tmp_path / "intent_cache.json")


@pytest.fixture
def stub_diagnostics(monkeypatch):
    """Records what the handler asked the system for, without touching the OS."""
    import actions.system_diagnostics_mcp as module

    calls = []

    def _fake(parameters, player=None, speak=None):
        calls.append((dict(parameters), speak, player))
        return "RAM is at 40 percent."

    monkeypatch.setattr(module, "system_diagnostics", _fake)
    return calls


# ── Reading the vitals is claimed ───────────────────────────────────────────

@pytest.mark.parametrize(
    "text, action",
    [
        ("how much ram is chrome using", "ram_hogs"),
        ("what is eating my ram", "ram_hogs"),
        ("check memory usage", "ram_hogs"),
        ("what is eating my cpu", "cpu_hogs"),
        ("check cpu usage", "cpu_hogs"),
        ("how much battery do i have", "battery"),
        ("is laptop charging", "battery"),
        ("system status", "status"),
        ("run a system diagnostics report", "status"),
    ],
)
def test_a_vitals_question_is_claimed(dispatcher, text, action):
    result = dispatcher.match_instant_reflex(text)

    assert result is not None, text
    assert result.intent == "system_status"
    assert result.action == action
    assert result.tier == 1


# ── Brightness: a number is absolute, a verb is relative ────────────────────

@pytest.mark.parametrize(
    "text, expected",
    [
        ("set brightness to 40", {"level": 40, "relative": False}),
        ("brightness 65", {"level": 65, "relative": False}),
        ("dim the screen", {"level": -20, "relative": True}),
        ("lower the brightness", {"level": -20, "relative": True}),
        ("increase brightness", {"level": 20, "relative": True}),
        ("make the screen brighter", {"level": 20, "relative": True}),
        ("what is my brightness", {}),
    ],
)
def test_a_brightness_request_carries_the_right_hint(dispatcher, text, expected):
    result = dispatcher.match_instant_reflex(text)

    assert result is not None, text
    assert result.intent == "brightness_control"
    assert result.action == "brightness"
    assert result.parameters == expected


def test_a_number_beats_a_verb(dispatcher):
    # The old ladder sent level=90 *and* relative=True, i.e. ninety points more
    # than where the screen already was.
    result = dispatcher.match_instant_reflex("increase brightness to 90")

    assert result.parameters == {"level": 90, "relative": False}


# ── Ending a program ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text, target, force",
    [
        ("kill chrome", "chrome", False),
        ("terminate notepad", "notepad", False),
        ("close app spotify", "spotify", False),
        ("end process discord", "discord", False),
        ("force kill chrome", "chrome", True),
        ("kill the chrome browser", "chrome browser", False),
    ],
)
def test_a_kill_request_names_its_target(dispatcher, text, target, force):
    result = dispatcher.match_instant_reflex(text)

    assert result is not None, text
    assert result.intent == "process_kill"
    assert result.parameters == {"target": target, "force": force}


def test_a_bare_kill_asks_rather_than_guesses(dispatcher):
    result = dispatcher.match_instant_reflex("kill")

    assert result.intent == "process_kill"
    assert result.parameters["target"] == ""


@pytest.mark.parametrize(
    "text",
    [
        "kill all the chrome windows i left open yesterday please",
        "open chrome",
        "play some music",
        "what is the weather",
        "clean ram",
    ],
)
def test_unrelated_sentences_stay_unclaimed(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result is None or result.intent not in (
        "process_kill",
        "brightness_control",
        "system_status",
    ), text


# ── The handlers ───────────────────────────────────────────────────────────

class UI:
    def __init__(self):
        self.finished = []
        self.log = []

    def write_log(self, message=""):
        self.log.append(str(message))

    def finish_task_workspace(self, result, status, percent):
        self.finished.append((result, status, percent))


class App:
    def __init__(self):
        self.ui = UI()
        self.said = []

    def speak(self, message):
        self.said.append(message)


class Intent:
    def __init__(self, name, action, parameters=None):
        self.intent = name
        self.action = action
        self.parameters = parameters or {}
        self.voice_ack = ""


def _run(intent: Intent, text: str = "do it"):
    """Dispatch one turn and wait for the worker to finish."""
    app = App()
    handled = dispatch_actions(text, intent, app, source="dashboard")

    for _ in range(60):
        if app.ui.finished:
            break
        time.sleep(0.02)

    return app, handled


def test_ram_is_read_through_the_action_module(stub_diagnostics):
    app, handled = _run(Intent("system_status", "ram_hogs"))

    assert handled is True
    assert app.said == ["Scanning memory consumers..."]
    assert stub_diagnostics[0][0] == {"action": "ram_hogs"}
    assert app.ui.finished[-1][1] == "Memory analysis complete."
    assert app.ui.finished[-1][2] == 100


def test_the_report_says_its_acknowledgement_once(stub_diagnostics):
    app, handled = _run(Intent("system_status", "status"), "system status")

    assert handled is True
    assert app.said == ["Retrieving system diagnostics report..."]
    assert stub_diagnostics[0][0] == {"action": "status"}


def test_a_battery_check_is_silent_until_the_answer(stub_diagnostics):
    app, _ = _run(Intent("system_status", "battery"))

    # Diagnostics speaks its own summary; a second sentence before it would be
    # the same double-acknowledgement bug fixed in the other handler modules.
    assert app.said == []
    assert stub_diagnostics[0][0] == {"action": "battery"}


def test_brightness_changes_before_it_speaks(stub_diagnostics):
    app, handled = _run(
        Intent("brightness_control", "brightness", {"level": -20, "relative": True}),
        "dim the screen",
    )

    assert handled is True
    assert app.said == []
    assert stub_diagnostics[0][0] == {"action": "brightness", "level": -20, "relative": True}
    assert stub_diagnostics[0][0]["relative"] is True


def test_a_brightness_question_asks_for_no_level(stub_diagnostics):
    app, _ = _run(Intent("brightness_control", "brightness", {}), "what is my brightness")

    assert stub_diagnostics[0][0] == {"action": "brightness"}
    assert app.ui.finished[-1][1] == "Brightness adjusted."


def test_a_named_process_is_terminated(stub_diagnostics):
    app, handled = _run(
        Intent("process_kill", "kill", {"target": "chrome", "force": True}), "force kill chrome"
    )

    assert handled is True
    assert app.said == ["Closing chrome, sir..."]
    assert stub_diagnostics[0][0] == {"action": "kill", "target": "chrome", "force": True}


def test_a_nameless_kill_asks_for_a_name(stub_diagnostics):
    app, handled = _run(Intent("process_kill", "kill", {"target": ""}), "kill")

    assert handled is True
    assert app.said == ["Which program should I close, sir?"]
    assert stub_diagnostics == []          # nothing was terminated
    assert app.ui.finished == []


def test_a_failing_reader_is_reported_rather_than_swallowed(monkeypatch):
    import actions.system_diagnostics_mcp as module

    def _boom(*args, **kwargs):
        raise RuntimeError("psutil unavailable")

    monkeypatch.setattr(module, "system_diagnostics", _boom)
    app, handled = _run(Intent("system_status", "ram_hogs"))

    assert handled is True
    assert app.ui.finished[-1][2] == 0
    assert app.said[-1] == "I could not read the system just now, sir."
    assert any("psutil unavailable" in line for line in app.ui.log)


def test_an_action_the_reader_cannot_serve_is_declined():
    app, handled = _run(Intent("system_status", "restart"))

    assert handled is False
    assert app.said == []


def test_the_three_intents_are_dispatchable():
    for intent in ("system_status", "brightness_control", "process_kill"):
        assert has_handler(intent), intent


# ── The owner decision ─────────────────────────────────────────────────────

def test_the_owner_may_end_a_program():
    decision = evaluate(
        "process_kill",
        evidence=Evidence(trust=Trust.OWNER, verified=True, source="face"),
    )

    assert decision.policy_allowed is True
    assert decision.denied is False
    assert decision.needs_confirm is True     # recorded, not enforced here


def test_a_guest_may_not_end_a_program():
    decision = evaluate(
        "process_kill",
        evidence=Evidence(trust=Trust.GUEST, verified=True, source="face"),
    )

    assert decision.denied is True
    assert "system.kill" in decision.reason


def test_an_unrecognised_voice_may_not_end_a_program():
    decision = evaluate(
        "process_kill",
        evidence=Evidence(trust=Trust.UNKNOWN, verified=True, source="face"),
    )

    assert decision.denied is True
    assert decision.spoken


def test_a_guest_may_dim_the_screen_and_read_the_vitals():
    guest = Evidence(trust=Trust.GUEST, verified=True, source="face")

    assert evaluate("brightness_control", evidence=guest).denied is False
    assert evaluate("system_status", evidence=guest).denied is False


def test_a_refused_kill_never_reaches_the_handler(monkeypatch, stub_diagnostics):
    monkeypatch.setattr(
        gate,
        "presence_evidence",
        lambda: Evidence(trust=Trust.GUEST, verified=True, source="face"),
    )
    app, handled = _run(Intent("process_kill", "kill", {"target": "chrome"}))

    # The refusal *is* the answer for the turn: falling through would let the
    # legacy rule chain do exactly what the gate just refused.
    assert handled is True
    assert stub_diagnostics == []
    assert app.said, "the refusal is spoken out loud"
    assert any(line.startswith("GATE:") for line in app.ui.log)


# ── The regression: a door with no model behind it ─────────────────────────

def test_the_phone_can_ask_about_ram(monkeypatch, dispatcher):
    from unittest.mock import MagicMock

    monkeypatch.setattr("core.omni_planner.OmniPlanner.is_composite_goal", lambda text: False)
    monkeypatch.setattr(
        "core.memory_cortex.MemoryCortex.get_instance", classmethod(lambda cls: MagicMock())
    )

    result = dispatcher.resolve("how much ram is chrome using", enable_ai_tier=False)

    assert result.tier == 1
    assert result.intent == "system_status"
    assert result.action == "ram_hogs"


# ── And main.py keeps no copy ──────────────────────────────────────────────

def test_main_has_no_diagnostics_ladder_left():
    source = MAIN.read_text(encoding="utf-8")

    for forgotten in (
        "is_ram_check",
        "is_battery_check",
        "is_brightness_req",
        "is_kill_req",
        "is_diag_req",
        "_run_brightness",
        "_run_kill",
        "Memory analysis complete.",
    ):
        assert forgotten not in source, forgotten
