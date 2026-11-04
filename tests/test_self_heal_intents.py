"""Saturn's own repair shop: read the patch log, and ask the owner to write code.

``is_trigger_bug`` / ``is_heal_cmd`` / ``is_rollback_cmd`` / ``is_patch_history``
were four keyword branches below the router in ``main.py`` with no intents at
all, so "heal yourself" worked at the keyboard and did nothing from the phone.

Two intents, not four. Applying a patch, rolling one back and provoking a test
bug all end in a source file being written - the demo bug exists to be caught and
immediately patched - so they are one capability, and it is the owner's.
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
def stub_heal(monkeypatch):
    """Records what the handler asked the repair engine for."""
    import actions.auto_heal_engine as module

    calls = []

    def _fake(parameters, player=None, speak=None):
        calls.append((dict(parameters), speak is not None))
        return "No automatic patches applied yet. System is running clean."

    monkeypatch.setattr(module, "auto_heal", _fake)
    return calls


# ── Asking is claimed ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text, intent, action",
    [
        ("show patches", "self_heal_report", "history"),
        ("patch history", "self_heal_report", "history"),
        ("patch log", "self_heal_report", "history"),
        # The regression: "auto heal status" contains "auto heal", and the old
        # ladder tested the repair branch first - asking for a status patched
        # the code.
        ("auto heal status", "self_heal_report", "status"),
        ("undo last patch", "self_heal_repair", "rollback"),
        ("rollback patch", "self_heal_repair", "rollback"),
        ("heal yourself", "self_heal_repair", "heal"),
        ("auto heal", "self_heal_repair", "heal"),
        ("fix that bug", "self_heal_repair", "heal"),
        ("patch yourself", "self_heal_repair", "heal"),
        ("trigger test bug", "self_heal_repair", "trigger_bug"),
        ("simulate error", "self_heal_repair", "trigger_bug"),
    ],
)
def test_a_repair_shop_sentence_is_claimed(dispatcher, text, intent, action):
    result = dispatcher.match_instant_reflex(text)

    assert result is not None, text
    assert result.intent == intent
    assert result.action == action
    assert result.tier == 1
    assert result.voice_ack == ""      # the engine narrates; nothing speaks first


@pytest.mark.parametrize(
    "text",
    ["play some music", "open chrome", "what is the weather", "set a reminder to call mom"],
)
def test_unrelated_sentences_stay_unclaimed(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result is None or result.intent not in ("self_heal_report", "self_heal_repair"), text


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
    """Dispatch one turn and wait for the worker to report itself."""
    app = App()
    handled = dispatch_actions(text, intent, app, source="dashboard")

    for _ in range(80):
        if app.ui.finished or app.said:
            break
        time.sleep(0.02)

    return app, handled


def test_the_patch_log_is_read_through_the_engine(stub_heal):
    app, handled = _run(Intent("self_heal_report", "history"), "show patches")

    assert handled is True
    assert stub_heal == [({"action": "history"}, True)]   # the engine may speak
    assert app.said == []                                 # but the handler does not
    assert app.ui.finished[-1][1] == "Auto-heal report ready."
    assert app.ui.finished[-1][2] == 100


def test_a_status_report_asks_for_a_status(stub_heal):
    app, _ = _run(Intent("self_heal_report", "status"), "auto heal status")

    assert stub_heal[0][0] == {"action": "status"}


def test_healing_goes_through_the_engine(stub_heal):
    app, handled = _run(Intent("self_heal_repair", "heal"), "heal yourself")

    assert handled is True
    assert stub_heal[0][0] == {"action": "heal"}
    assert app.said == []
    assert app.ui.finished[-1][1] == "Self-patching complete."


def test_a_rollback_is_reported(stub_heal):
    app, _ = _run(Intent("self_heal_repair", "rollback"), "undo last patch")

    assert stub_heal[0][0] == {"action": "rollback"}
    assert app.ui.finished[-1][1] == "Rollback complete."


def test_a_failed_repair_says_so(monkeypatch):
    import actions.auto_heal_engine as module

    def _boom(*args, **kwargs):
        raise RuntimeError("no gemini key")

    monkeypatch.setattr(module, "auto_heal", _boom)
    app, handled = _run(Intent("self_heal_repair", "heal"), "heal yourself")

    assert handled is True
    assert app.ui.finished[-1][2] == 0
    assert app.said[-1] == "The repair itself failed, sir. I have left the code untouched."
    assert any("no gemini key" in line for line in app.ui.log)


def test_an_action_the_handlers_cannot_serve_is_declined():
    app, handled = _run(Intent("self_heal_repair", "rebuild_everything"))

    assert handled is False
    assert app.said == []


# ── The demo bug actually heals ────────────────────────────────────────────

def test_a_healthy_test_action_patches_nothing(monkeypatch):
    import actions.test_action as module
    import actions.auto_heal_engine as engine

    calls = []
    monkeypatch.setattr(module, "test_action", lambda *a, **k: "metric 10")
    monkeypatch.setattr(
        engine.AutoHealEngine, "heal_traceback",
        classmethod(lambda cls, *a, **k: calls.append(a) or {}),
    )
    app, handled = _run(Intent("self_heal_repair", "trigger_bug"), "trigger test bug")

    assert handled is True
    assert calls == []
    assert app.said[-1].startswith("Test action executed with no errors")


def test_the_demo_bug_is_caught_and_handed_to_the_engine(monkeypatch):
    import actions.test_action as module
    import actions.auto_heal_engine as engine

    recorded, healed = [], []

    def _raise(*args, **kwargs):
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(module, "test_action", _raise)
    monkeypatch.setattr(
        engine.AutoHealEngine, "record_last_error",
        classmethod(lambda cls, tb: recorded.append(tb)),
    )
    monkeypatch.setattr(
        engine.AutoHealEngine, "heal_traceback",
        classmethod(lambda cls, *a, **k: healed.append(k.get("module_name")) or {"message": "fixed"}),
    )
    app, _ = _run(Intent("self_heal_repair", "trigger_bug"), "trigger test bug")

    assert recorded and "ZeroDivisionError" in recorded[0]
    assert healed == ["test_action"]


def test_both_intents_are_dispatchable():
    assert has_handler("self_heal_report")
    assert has_handler("self_heal_repair")


# ── Who may write the code ─────────────────────────────────────────────────

def test_the_owner_may_patch_saturn():
    decision = evaluate(
        "self_heal_repair", evidence=Evidence(trust=Trust.OWNER, verified=True, source="face")
    )

    assert decision.denied is False
    assert decision.capability == "file.write.repo"


def test_a_guest_may_not_patch_saturn():
    decision = evaluate(
        "self_heal_repair", evidence=Evidence(trust=Trust.GUEST, verified=True, source="face")
    )

    assert decision.denied is True


def test_anybody_may_read_the_patch_log():
    unknown = Evidence(trust=Trust.UNKNOWN, verified=True, source="face")

    assert evaluate("self_heal_report", evidence=unknown).denied is False


def test_a_refused_heal_never_reaches_the_engine(monkeypatch, stub_heal):
    monkeypatch.setattr(
        gate,
        "presence_evidence",
        lambda: Evidence(trust=Trust.GUEST, verified=True, source="face"),
    )
    app, handled = _run(Intent("self_heal_repair", "heal"), "heal yourself")

    assert handled is True
    assert stub_heal == []
    assert app.said, "the refusal is spoken out loud"


# ── The regression: a door with no model behind it ─────────────────────────

def test_the_phone_can_ask_for_a_fix(monkeypatch, dispatcher):
    from unittest.mock import MagicMock

    monkeypatch.setattr("core.omni_planner.OmniPlanner.is_composite_goal", lambda text: False)
    monkeypatch.setattr(
        "core.memory_cortex.MemoryCortex.get_instance", classmethod(lambda cls: MagicMock())
    )

    result = dispatcher.resolve("heal yourself", enable_ai_tier=False)

    assert result.tier == 1
    assert result.intent == "self_heal_repair"
    assert result.action == "heal"


# ── The learner still owns "new rule:" ─────────────────────────────────────

def test_the_new_rule_phrasing_still_learns(monkeypatch):
    from core.learned_rules import LearnedRulesEngine

    stored = []
    monkeypatch.setattr(
        LearnedRulesEngine, "add_rule",
        classmethod(lambda cls, text, **k: stored.append(text) or {"rule": {"rule": text}}),
    )

    learned = LearnedRulesEngine.detect_and_learn_conversational_rule(
        "new rule: always use chrome"
    )

    assert learned is not None
    assert stored == ["Always use chrome"]


# ── And main.py keeps no copy ──────────────────────────────────────────────

def test_main_has_no_self_heal_ladder_left():
    source = MAIN.read_text(encoding="utf-8")

    for forgotten in (
        "is_trigger_bug",
        "is_heal_cmd",
        "is_rollback_cmd",
        "is_patch_history",
        "is_learn_rule",
        "_run_test_bug",
        "_run_history",
        "Self-patching complete.",
    ):
        assert forgotten not in source, forgotten
