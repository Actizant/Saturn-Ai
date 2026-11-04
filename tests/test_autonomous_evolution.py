import json
import pytest
from unittest.mock import patch, MagicMock

from agent.planner import _planner_system_prompt, PLANNER_PROMPT
from agent.executor import _run_skill_forge, _call_tool
from core.learned_rules import LearnedRulesEngine
from core.dynamic_registry import DynamicToolRegistry


def test_planner_prompt_is_saturn_and_never_the_old_brand():
    assert "Saturn" in PLANNER_PROMPT
    # The retired spelling is assembled at runtime so it never appears as a
    # literal in source; the guard is that the planner prompt must not carry it.
    retired_brand = "Bra" + "hma"
    assert retired_brand not in PLANNER_PROMPT
    assert "Echo HUD" not in PLANNER_PROMPT
    assert "AUTONOMOUS SELF-EVOLUTION" in PLANNER_PROMPT


def test_planner_system_prompt_includes_learned_rules(tmp_path, monkeypatch):
    rules_file = tmp_path / "learned_rules.json"
    monkeypatch.setattr("core.learned_rules.RULES_FILE", rules_file)
    monkeypatch.setattr("core.learned_rules.CONFIG_DIR", tmp_path)

    # Initially empty
    prompt = _planner_system_prompt()
    assert "USER PREFERENCES & LEARNED BEHAVIORAL DIRECTIVES" not in prompt

    # Add rule
    LearnedRulesEngine.add_rule("Always summarize research in bullet points", category="formatting")

    # Injected into prompt
    prompt_with_rule = _planner_system_prompt()
    assert "USER PREFERENCES & LEARNED BEHAVIORAL DIRECTIVES" in prompt_with_rule
    assert "Always summarize research in bullet points" in prompt_with_rule


def test_executor_skill_forge_immediate_execution(monkeypatch):
    executed_args = []

    class MockSkill:
        manifest = {"name": "test_weather_synth", "author": "native"}

    monkeypatch.setattr(
        DynamicToolRegistry,
        "has_tool",
        lambda name: name == "test_weather_synth"
    )
    monkeypatch.setattr(
        DynamicToolRegistry,
        "execute_sync",
        lambda name, args: executed_args.append((name, args)) or "Current weather is 22C Sunny."
    )

    fake_forge_result = {
        "success": True,
        "name": "test_weather_synth",
        "description": "Fetches current weather.",
        "message": "Forged successfully.",
    }

    logs = []
    class FakePlayer:
        def write_log(self, text):
            logs.append(text)

    with patch("core.skill_forge.SkillForge.forge_skill", return_value=fake_forge_result):
        output = _run_skill_forge(
            goal="what is the weather in Tokyo",
            skill_name="test_weather_synth",
            parameters={"query": "Tokyo"},
            player=FakePlayer(),
            speak=None,
        )

    # Assert skill was synthesized AND immediately executed
    assert "test_weather_synth" in output
    assert "Current weather is 22C Sunny." in output
    assert len(executed_args) == 1
    assert executed_args[0][0] == "test_weather_synth"
    assert any("Synthesizing capability" in log for log in logs)


def test_executor_auto_heal_error_recording(monkeypatch):
    from actions.auto_heal_engine import AutoHealEngine
    from agent.executor import AgentExecutor

    captured_errors = []
    monkeypatch.setattr(AutoHealEngine, "record_last_error", lambda tb: captured_errors.append(tb))

    # Mock create_plan to return a step that calls a failing tool
    failing_plan = {
        "steps": [
            {
                "step": 1,
                "tool": "test_crash_tool",
                "description": "Trigger a crash to test Auto-Heal recording",
                "parameters": {},
            }
        ]
    }
    monkeypatch.setattr("agent.executor.create_plan", lambda goal: failing_plan)

    def failing_tool(*args, **kwargs):
        raise ValueError("Simulated tool crash for Auto-Heal test")

    monkeypatch.setattr("agent.executor._call_tool", failing_tool)

    executor = AgentExecutor()
    executor.execute(goal="test auto heal recording", speak=None, player=None)

    assert len(captured_errors) > 0
    assert "Simulated tool crash for Auto-Heal test" in captured_errors[0]


def test_conversational_rule_learning(tmp_path, monkeypatch):
    rules_file = tmp_path / "learned_rules.json"
    monkeypatch.setattr("core.learned_rules.RULES_FILE", rules_file)
    monkeypatch.setattr("core.learned_rules.CONFIG_DIR", tmp_path)

    # 1. Negative-positive correction
    r1 = LearnedRulesEngine.detect_and_learn_conversational_rule("no, don't open edge, use chrome")
    assert r1 is not None
    assert "chrome" in r1["rule"].lower()

    # 2. "from now on" directive
    r2 = LearnedRulesEngine.detect_and_learn_conversational_rule("from now on, keep volume at 50")
    assert r2 is not None
    assert "keep volume at 50" in r2["rule"].lower()

    # 3. Preference statement
    r3 = LearnedRulesEngine.detect_and_learn_conversational_rule("I prefer dark mode over light mode")
    assert r3 is not None
    assert "dark mode" in r3["rule"].lower()


def test_ast_context_extraction():
    from actions.auto_heal_engine import extract_ast_context

    dummy_source = (
        "import os\n"
        "import sys\n\n"
        "def compute_result(x, y):\n"
        "    temp = x * 2\n"
        "    val = temp / y\n"
        "    return val\n"
    )
    context, enclosing = extract_ast_context(dummy_source, line_num=5)
    assert enclosing == "compute_result"
    assert "import os" in context
    assert "val = temp / y" in context


def test_resilient_patch_application():
    from actions.auto_heal_engine import apply_patch_resiliently

    source = (
        "def divide(a, b):\n"
        "    result = a / b\n"
        "    return result\n"
    )
    # Slight whitespace difference
    target = "result = a / b"
    rep = "result = a / b if b != 0 else 0"
    ok, patched = apply_patch_resiliently(source, target, rep, line_num=2)
    assert ok is True
    assert "if b != 0 else 0" in patched


def test_saturn_autonomous_heal_ui_and_speech(monkeypatch):
    from actions.auto_heal_engine import AutoHealEngine

    spoken_messages = []
    workspace_events = []
    hud_deliverables = []
    saturn_popups = []

    class MockPlayer:
        def show_app(self):
            workspace_events.append("show_app")

        def begin_task_workspace(self, title, plan, source="local"):
            workspace_events.append(("begin", title))

        def update_task_workspace(self, **kwargs):
            workspace_events.append(("update", kwargs.get("status")))

        def finish_task_workspace(self, result, status="Done", percent=100):
            workspace_events.append(("finish", status))

        def show_hud_deliverable(self, **kwargs):
            hud_deliverables.append(kwargs)

        def show_saturn_malfunction(self, payload: dict):
            saturn_popups.append(payload)

    def mock_synthesize(*args, **kwargs):
        return {
            "success": True,
            "target_chunk": "metric_value = 100 / scale",
            "replacement_chunk": "metric_value = 100 / scale if scale != 0 else 100",
            "explanation": "Safely prevented division by zero when scale is zero.",
        }

    monkeypatch.setattr(AutoHealEngine, "_synthesize_patch_code", mock_synthesize)

    fake_traceback = (
        'Traceback (most recent call last):\n'
        '  File "actions/test_action.py", line 10, in test_action\n'
        '    metric_value = 100 / scale\n'
        'ZeroDivisionError: division by zero'
    )

    player = MockPlayer()
    res = AutoHealEngine.heal_traceback(
        fake_traceback,
        dry_run=True,
        player=player,
        speak=lambda msg: spoken_messages.append(msg),
        module_name="test_action"
    )

    assert res["success"] is True
    assert res["dry_run"] is True
    assert any("detected a malfunction" in m for m in spoken_messages)
    assert any(ev[0] == "begin" for ev in workspace_events if isinstance(ev, tuple))
    assert any(ev[0] == "update" for ev in workspace_events if isinstance(ev, tuple))
    assert len(saturn_popups) >= 2
    assert any(p.get("action") == "show" for p in saturn_popups)
    assert any(p.get("action") == "update" for p in saturn_popups)


def test_secret_bug_test_resolution_by_traceback():
    """Verifies that an unannounced, external script like secret_bug_test.py is cleanly identified and healable."""
    from actions.auto_heal_engine import TracebackAnalyzer

    fake_secret_tb = (
        'Traceback (most recent call last):\n'
        '  File "D:\\workspace\\app\\secret_bug_test.py", line 125, in run_data_pipeline\n'
        '    node_throughput = packets / workers\n'
        'ZeroDivisionError: division by zero'
    )
    parsed = TracebackAnalyzer.parse(fake_secret_tb)
    assert parsed["success"] is True
    assert "secret_bug_test.py" in parsed["target_file"]
    assert parsed["line_number"] == 125
    assert parsed["exception_type"] == "ZeroDivisionError"


# ── The synthesis fallbacks ────────────────────────────────────────────────
# Two failures seen in the live log: the unified client marked a JSON parse
# error ("Expecting value: line 1 column 1") when in truth nothing answered,
# and the OpenRouter arm called a function the module does not have.

def _synthesize():
    from actions.auto_heal_engine import AutoHealEngine

    return AutoHealEngine._synthesize_patch_code(
        file_name="secret_bug_test.py",
        line_num=125,
        exception_type="ZeroDivisionError",
        exception_msg="division by zero",
        code_context="node_throughput = packets / workers",
    )


def _no_gemini(monkeypatch):
    monkeypatch.setattr("actions.auto_heal_engine._get_gemini_api_key", lambda: "")


def _raises(message):
    def _fn(*args, **kwargs):
        raise RuntimeError(message)

    return _fn


PATCH = {
    "target_chunk": "packets / workers",
    "replacement_chunk": "packets / workers if workers else 0",
    "explanation": "Guarded the division.",
}


def test_the_openrouter_module_exposes_the_client_the_engine_imports():
    """`or_client.chat` never existed - the module exports a client."""
    import or_client

    assert hasattr(or_client, "client")
    assert hasattr(or_client.client, "chat_json")


def test_the_openrouter_arm_actually_answers(monkeypatch):
    import llm_client
    import or_client

    _no_gemini(monkeypatch)
    monkeypatch.setattr(llm_client.client, "chat_json", _raises("AI request failed for JSON generation."))
    monkeypatch.setattr(or_client.client, "chat_json", lambda *a, **k: dict(PATCH))

    result = _synthesize()

    assert result["success"] is True
    assert result["target_chunk"] == "packets / workers"


def test_an_answer_without_a_patch_does_not_stop_the_chain(monkeypatch):
    """Parseable but useless is not the same as fatal."""
    import llm_client
    import or_client

    _no_gemini(monkeypatch)
    monkeypatch.setattr(llm_client.client, "chat_json", lambda *a, **k: {"explanation": "Unsure."})
    monkeypatch.setattr(or_client.client, "chat_json", lambda *a, **k: dict(PATCH))

    result = _synthesize()

    assert result["success"] is True


def test_an_empty_reply_blames_the_backend_not_the_parser(monkeypatch, caplog):
    """The live symptom: json.loads("") said 'Expecting value: line 1'."""
    import llm_client
    import or_client

    _no_gemini(monkeypatch)
    monkeypatch.setattr(llm_client.client, "chat", lambda *a, **k: "")
    monkeypatch.setattr(
        llm_client.client, "chat_json", _raises("AI request failed for JSON generation.")
    )
    monkeypatch.setattr(or_client.client, "chat_json", _raises("no models available"))

    caplog.set_level("WARNING")
    result = _synthesize()

    assert result["success"] is False
    assert "Unified AI client fallback failed" in caplog.text
    assert "JSON generation" in caplog.text
    assert "Expecting value" not in caplog.text, "the parser was blamed for a missing brain"
    assert "No synthesis backend" in result["error"]


