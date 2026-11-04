"""
tests/test_action_router.py
===========================
Hermetic tests for the intent -> handler registry extracted out of main.py.

These cover the two contracts that matter:

1. Structural - every intent the dispatcher can emit has exactly one handler,
   and main.py itself no longer branches on ``intent_res.intent``.
2. Behavioural - handlers really start their workers. The old ladder built
   ``_run_vision_process`` and ``_run_shopping`` without ever calling
   ``.start()``, so both silently did nothing; those regressions are pinned
   here with a real thread + event round trip.
"""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest

import agent.actions as actions_pkg
from agent.action_router import (
    ActionContext,
    dispatch_actions,
    get_action_router,
    has_handler,
    register,
    registered_intents,
)
from core.intent_dispatcher import IntentResult

ROOT = Path(__file__).resolve().parents[1]

# Every intent name the three dispatcher tiers can return (general_conversation
# and the Tier-3-only categories deliberately fall through to the LLM chain).
DISPATCHABLE = (
    "brightness_control",
    "developer_agent",
    "flowstate",
    "forget_rule",
    "game_sentinel",
    "generate_image",
    "google_workspace",
    "learn_rule",
    "media_control",
    "meeting_mode",
    "mobile_connect",
    "net_sentinel",
    "omni_planner",
    "open_app",
    "presentation",
    "process_kill",
    "reverse_engineer",
    "rule_book",
    "self_heal_repair",
    "self_heal_report",
    "shopping",
    "space_cleaner",
    "spreadsheet",
    "study_guard",
    "system_status",
    "vision",
)


# =============================================================================
# Fakes
# =============================================================================

class FakeUI:
    def __init__(self) -> None:
        self.calls = []

    def write_log(self, text: str) -> None:
        self.calls.append(("log", str(text)))

    def begin_task_workspace(self, title, steps, source=None) -> None:
        self.calls.append(("begin", title, list(steps)))

    def update_task_workspace(self, **kwargs) -> None:
        self.calls.append(("update", kwargs))

    def finish_task_workspace(self, **kwargs) -> None:
        self.calls.append(("finish", kwargs))

    def show_hud_deliverable(self, **kwargs) -> None:
        self.calls.append(("hud", kwargs))

    def methods(self):
        return [call[0] for call in self.calls]


class FakeApp:
    def __init__(self) -> None:
        self.ui = FakeUI()
        self.spoken = []

    def speak(self, message) -> None:
        self.spoken.append(message)

    def _handle_saturn_connect_command(self, text, source=None, parsed_intent=None):
        return False

    def _start_meeting_mode(self, event=None) -> None:
        self.spoken.append("__start_meeting__")

    def _stop_meeting_mode(self, reason="") -> None:
        self.spoken.append("__stop_meeting__")


def make_intent(intent: str, action: str = "", **params) -> IntentResult:
    return IntentResult(
        tier=1, intent=intent, action=action, parameters=dict(params)
    )


def ctx_for(intent: str, action: str = "", text: str = "do the thing", **params):
    app = FakeApp()
    return ActionContext(
        text=text,
        intent=make_intent(intent, action, **params),
        app=app,
        source="local",
    ), app


WORKER_THREADS = (
    "vision-worker",
    "shopping-agent",
    "media-control",
    "space-cleaner",
    "net-sentinel",
    "study-guard",
    "game-sentinel",
    "omni-planner",
    "flowstate",
    "presentation-gen",
    "spreadsheet-gen",
    "saturn-dev",
    "image-gen-worker",
    "reverse-engineer-worker",
    "open-local-app",
)


def join_workers(timeout: float = 3.0) -> None:
    """Wait for the daemon workers a handler spawned to finish."""
    for thread in threading.enumerate():
        if thread is threading.current_thread():
            continue
        if thread.name in WORKER_THREADS and thread.is_alive():
            thread.join(timeout)


# =============================================================================
# Structural contract
# =============================================================================

def test_handler_modules_all_imported():
    assert actions_pkg.FAILED_MODULES == []


@pytest.mark.parametrize("intent", DISPATCHABLE)
def test_every_dispatchable_intent_has_a_handler(intent):
    assert has_handler(intent), f"no action handler registered for '{intent}'"


def test_registry_covers_exactly_the_dispatchable_set():
    assert set(registered_intents()) == set(DISPATCHABLE)


def test_only_conversation_is_deliberately_unhandled():
    # Conversation is the model's job, so it must fall through to the Gemini
    # Live / REST chain. Every other intent the dispatcher can name has a
    # handler - google_workspace used to be listed here, and is not any more:
    # it has a reader now (agent/actions/productivity.py).
    assert not has_handler("general_conversation")
    assert not has_handler("")


def test_main_delegates_instead_of_branching():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "dispatch_actions(" in source
    # The 800-line ladder must never come back.
    assert "intent_res.intent ==" not in source


def test_ladder_handlers_no_longer_live_in_main():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    for worker in (
        "_run_vision_process",
        "_run_shopping",
        "_run_media_control",
        "_run_net_sentinel",
        "_run_study_guard",
        "_run_game_sentinel",
        "_run_space_cleaner",
        "_run_omni_planner",
        "_run_flowstate",
    ):
        assert worker not in source, f"{worker} should live in agent/actions/"


def test_register_rejects_empty_intent():
    @register("")
    def _never_used(context):  # pragma: no cover - must not be reachable
        return True

    assert not has_handler("")


# =============================================================================
# Behavioural contract - the workers must actually start
# =============================================================================

def test_vision_starts_its_worker(monkeypatch):
    calls = []
    done = threading.Event()

    def fake_screen_process(**kwargs):
        calls.append(kwargs)
        done.set()
        return True

    monkeypatch.setitem(
        sys.modules,
        "actions.screen_processor",
        types.SimpleNamespace(screen_process=fake_screen_process),
    )

    ctx, app = ctx_for("vision", "inspect_screen", angle="screen")
    assert get_action_router().dispatch(ctx) is True

    assert done.wait(3.0), "vision handler never started its worker"
    join_workers()
    assert calls and calls[0]["parameters"]["angle"] == "screen"
    assert calls[0]["player"] is app.ui
    # The HUD was told we are scanning this screen.
    titles = [call[1].get("title") for call in app.ui.calls if call[0] == "update"]
    assert "Screen Analysis" in titles, titles


def test_vision_worker_reports_failure(monkeypatch):
    done = threading.Event()

    def failing_screen_process(**kwargs):
        done.set()
        return False

    monkeypatch.setitem(
        sys.modules,
        "actions.screen_processor",
        types.SimpleNamespace(screen_process=failing_screen_process),
    )

    ctx, app = ctx_for("vision", "inspect_camera", angle="camera")
    assert get_action_router().dispatch(ctx) is True
    assert done.wait(3.0)
    join_workers()

    updates = [call[1] for call in app.ui.calls if call[0] == "update"]
    assert any(u.get("percent") == 0 for u in updates), updates


def test_shopping_search_starts_its_worker(monkeypatch):
    searched = threading.Event()

    class FakeAgent:
        def search_both_parallel(self, query, limit_per_store=3):
            searched.set()
            return {"amazon": [], "flipkart": []}

    monkeypatch.setitem(
        sys.modules,
        "core.shopping_agent",
        types.SimpleNamespace(get_shopping_agent=lambda: FakeAgent()),
    )

    ctx, app = ctx_for("shopping", "search_and_compare", text="buy a keyboard")
    assert get_action_router().dispatch(ctx) is True

    assert searched.wait(3.0), "shopping search never ran"
    join_workers()
    assert any("couldn't find live listings" in str(m) for m in app.spoken)


def test_media_control_spawns_and_speaks(monkeypatch):
    pressed = threading.Event()

    monkeypatch.setitem(
        sys.modules,
        "actions.media_keys",
        types.SimpleNamespace(
            volume_up=lambda: pressed.set(),
            volume_down=lambda: None,
            volume_mute=lambda: None,
            volume_set=lambda value: None,
            media_play_pause=lambda: None,
            media_next=lambda: None,
            media_previous=lambda: None,
        ),
    )

    ctx, app = ctx_for("media_control", "volume_up")
    ctx.intent.voice_ack = "Louder, sir."

    assert get_action_router().dispatch(ctx) is True
    assert pressed.wait(3.0), "media reflex never ran"
    join_workers()
    assert "Louder, sir." in app.spoken


def test_media_control_survives_a_missing_media_engine(monkeypatch):
    monkeypatch.setitem(sys.modules, "actions.media_keys", None)
    ctx, app = ctx_for("media_control", "volume_up")

    assert get_action_router().dispatch(ctx) is True
    join_workers()
    assert any("Media keys unavailable" in str(call) for call in app.ui.calls)


def test_media_keys_module_exists_for_both_call_sites():
    """main.py and dashboard/server.py both import this module by name."""
    import importlib.util

    assert importlib.util.find_spec("actions.media_keys") is not None

    from actions import media_keys

    for name in (
        "volume_up",
        "volume_down",
        "volume_mute",
        "volume_set",
        "media_play_pause",
        "media_next",
        "media_previous",
    ):
        assert callable(getattr(media_keys, name, None)), name


def test_media_transport_uses_the_shared_key_helper(monkeypatch):
    from actions import media_keys

    pressed = []
    monkeypatch.setattr(
        media_keys, "_press_media_key", lambda name: pressed.append(name) or True
    )

    assert media_keys.media_play_pause() is True
    assert media_keys.media_next() is True
    assert media_keys.media_previous() is True
    assert pressed == ["playpause", "nexttrack", "prevtrack"]


def test_meeting_mode_toggles(monkeypatch):
    ctx, app = ctx_for("meeting_mode", "start")
    assert get_action_router().dispatch(ctx) is True
    assert app.spoken == ["__start_meeting__"]

    ctx, app = ctx_for("meeting_mode", "stop")
    assert get_action_router().dispatch(ctx) is True
    assert app.spoken == ["__stop_meeting__"]


# =============================================================================
# Fall-through contract
# =============================================================================

def test_open_app_without_a_name_declines_the_turn():
    ctx, app = ctx_for("open_app", "open_local_app")
    assert get_action_router().dispatch(ctx) is False
    assert app.spoken == []


def test_mobile_connect_declines_for_instagram():
    app = FakeApp()
    ctx = ActionContext(
        text="ring my phone",
        intent=make_intent("mobile_connect", "ring_phone"),
        app=app,
        source="instagram",
    )
    assert get_action_router().dispatch(ctx) is False


def test_unknown_intent_is_not_handled():
    ctx, _ = ctx_for("general_conversation", "chat")
    assert get_action_router().dispatch(ctx) is False


def test_handler_exception_falls_through_to_legacy(monkeypatch):
    def exploding(context):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        get_action_router(), "handler_for", lambda name: exploding, raising=False
    )
    ctx, app = ctx_for("space_cleaner", "open_hud")

    assert get_action_router().dispatch(ctx) is False
    assert any("handler failed" in str(call) for call in app.ui.calls)


def test_dispatch_actions_never_raises(monkeypatch):
    import agent.action_router as router_module

    def exploding_router(*args, **kwargs):
        raise RuntimeError("registry offline")

    monkeypatch.setattr(router_module, "get_action_router", exploding_router)
    assert dispatch_actions("hi", make_intent("vision"), FakeApp()) is False


# =============================================================================
# ActionContext helpers degrade gracefully
# =============================================================================

def test_ui_call_is_a_noop_without_a_ui():
    app = types.SimpleNamespace(speak=lambda message: None)
    ctx = ActionContext(text="x", intent=make_intent("vision"), app=app)

    assert ctx.has_ui("update_task_workspace") is False
    assert ctx.ui_call("update_task_workspace", status="nope") is False


def test_ui_call_swallows_widget_errors():
    class AngryUI:
        def update_task_workspace(self, **kwargs):
            raise RuntimeError("widget deleted")

    app = types.SimpleNamespace(ui=AngryUI(), speak=lambda message: None)
    ctx = ActionContext(text="x", intent=make_intent("vision"), app=app)

    assert ctx.ui_call("update_task_workspace", status="nope") is False


def test_speak_never_raises_into_the_handler():
    class MuteApp:
        ui = types.SimpleNamespace()

        def speak(self, message):
            raise RuntimeError("tts engine dead")

    ctx = ActionContext(text="x", intent=make_intent("vision"), app=MuteApp())
    ctx.speak("hello")  # must not raise


def test_context_accessors_normalise_intent_fields():
    intent = IntentResult(
        tier=3,
        intent="  SHOPPING ",
        action=" Search_And_Compare ",
        parameters={"query": "laptop"},
        voice_ack="Scanning, sir.",
    )
    ctx = ActionContext(text="buy a laptop", intent=intent, app=FakeApp())

    assert ctx.name == "shopping"
    assert ctx.action == "search_and_compare"
    assert ctx.param("query") == "laptop"
    assert ctx.param("missing", "fallback") == "fallback"
    assert ctx.ack("default") == "Scanning, sir."


def test_context_survives_a_malformed_intent():
    ctx = ActionContext(text="x", intent=object(), app=FakeApp())

    assert ctx.name == ""
    assert ctx.action == ""
    assert ctx.params == {}
    assert ctx.ack("fallback") == "fallback"


# =============================================================================
# The trust gate sits in front of the handlers
# =============================================================================

PROBE = "__gate_probe__"


@pytest.fixture
def probe_intent():
    """A throwaway intent with no capability mapping, so it gets the default rule."""
    import agent.action_router as router_module

    calls = []

    @register(PROBE)
    def handler(ctx):
        calls.append(ctx.text)
        return True

    yield calls
    router_module._HANDLERS.pop(PROBE, None)


def test_a_refused_intent_does_not_reach_its_handler(probe_intent):
    from core.trust_gate import Decision, Trust

    denial = Decision(allowed=False, capability=PROBE, trust=Trust.UNKNOWN,
                      verified=True, policy_allowed=False,
                      reason="unrecognised person", spoken="I won't do that.")
    ctx, app = ctx_for(PROBE)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("core.trust_gate.evaluate", lambda *a, **k: denial)
        assert get_action_router().dispatch(ctx) is True

    assert probe_intent == []
    assert app.spoken == ["I won't do that."]
    assert any("GATE:" in line for kind, line in app.ui.calls if kind == "log")


def test_an_allowed_intent_runs(probe_intent):
    from core.trust_gate import Decision, Trust

    permission = Decision(allowed=True, capability=PROBE, trust=Trust.OWNER, verified=True)
    ctx, _app = ctx_for(PROBE)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("core.trust_gate.evaluate", lambda *a, **k: permission)
        assert get_action_router().dispatch(ctx) is True

    assert probe_intent == ["do the thing"]


def test_a_pending_confirmation_does_not_stop_the_turn(probe_intent):
    """The router cannot hold a half-asked question, so it must not refuse."""
    from core.trust_gate import Decision, Trust

    pending = Decision(allowed=False, capability=PROBE, trust=Trust.OWNER,
                       verified=True, needs_confirm=True, policy_allowed=True,
                       reason="needs a spoken yes")
    ctx, _app = ctx_for(PROBE)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("core.trust_gate.evaluate", lambda *a, **k: pending)
        get_action_router().dispatch(ctx)

    assert probe_intent == ["do the thing"]


def test_a_broken_gate_does_not_break_the_turn(probe_intent):
    def boom(*args, **kwargs):
        raise RuntimeError("gate offline")

    ctx, _app = ctx_for(PROBE)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("core.trust_gate.evaluate", boom)
        get_action_router().dispatch(ctx)

    assert probe_intent == ["do the thing"]


def test_a_stranger_at_an_armed_perimeter_cannot_route_an_action(probe_intent):
    """End to end, with the real gate and a fake camera verdict."""
    from core import trust_gate
    from core.presence_watch import PRESENT_UNVERIFIED

    class Watcher:
        def snapshot(self):
            return {"state": PRESENT_UNVERIFIED, "identity": {}}

    ctx, app = ctx_for(PROBE)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("core.security_perimeter.load_config",
                      lambda: {"enabled": True, "armed": True})
        patch.setattr("core.presence_watch.get_presence_watcher",
                      lambda **kw: Watcher())
        patch.setattr(trust_gate, "_owner_name", lambda: "Ada")
        dispatched = get_action_router().dispatch(ctx)

    assert dispatched is True
    assert probe_intent == []
    assert "Ada" in app.spoken[0]


def test_the_owner_at_an_armed_perimeter_gets_through(probe_intent):
    from core import trust_gate
    from core.presence_watch import PRESENT_OWNER

    class Watcher:
        def snapshot(self):
            return {"state": PRESENT_OWNER, "identity": {"name": "Ada"}}

    ctx, _app = ctx_for(PROBE)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("core.security_perimeter.load_config",
                      lambda: {"enabled": True, "armed": True})
        patch.setattr("core.presence_watch.get_presence_watcher",
                      lambda **kw: Watcher())
        patch.setattr(trust_gate, "_owner_name", lambda: "Ada")
        get_action_router().dispatch(ctx)

    assert probe_intent == ["do the thing"]