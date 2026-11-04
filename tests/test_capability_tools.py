"""The four tools that put the capability layer in front of the model.

Built as libraries, the registry and the task graph were invisible: Saturn had
no way to *choose* them. These tests pin the two things that make them real -
the tools are declared in the tool surface, and they report what happened
rather than what was intended.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from actions import capability_tools as tools
from core import capability_registry as registry
from core import gui_agent
from core import task_graph as graph
from core import trust_gate
from core.capability_registry import Device

ROOT = Path(__file__).resolve().parent.parent
TOOL_NAMES = ("describe_devices", "run_capability", "run_goal", "task_tree")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No test in this file may reach the house, the disk, or a real screen."""
    monkeypatch.setenv(registry.ENV_PATH, str(tmp_path / "devices.json"))
    monkeypatch.setenv(graph.ENV_PATH, str(tmp_path / "tasks.json"))
    registry.reset_cache()
    graph.reset()
    monkeypatch.setattr("core.trust_gate.evaluate",
                        lambda *a, **k: trust_gate.Decision(
                            allowed=True, capability=str(a[0] if a else ""),
                            trust=trust_gate.Trust.OWNER, verified=True))
    yield
    registry.reset_cache()
    graph.reset()


def seed_lamp() -> None:
    registry.save([Device(id="lamp", name="Desk lamp", adapter="dry_run",
                          kind="light",
                          capabilities={"switch.on": {}, "switch.off": {},
                                        "level.set": {}})])


def task_id_in(text: str) -> str:
    match = re.search(r"\[task (t-[0-9a-f]+)\]", text)
    assert match, f"no task id was quoted in: {text!r}"
    return match.group(1)


# ── The declarations ────────────────────────────────────────────────────────

def _declared() -> list[dict]:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "TOOL_DECLARATIONS"
                        for target in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError("TOOL_DECLARATIONS was not found in main.py")


def test_all_four_tools_are_declared_once():
    names = [tool.get("name") for tool in _declared()]
    for name in TOOL_NAMES:
        assert names.count(name) == 1, f"{name} is declared {names.count(name)} time(s)"


def test_every_declared_tool_has_a_handler_to_answer_with():
    for name in TOOL_NAMES:
        assert callable(getattr(tools, name, None)), f"no handler for {name}"


def test_a_declared_tool_name_reaches_the_dispatch_chain():
    """A name in the declarations but not in the chain is an unbound result."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in TOOL_NAMES:
        mentions = [node for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and node.value == name]
        assert len(mentions) >= 2, f"{name} is declared but never dispatched"


def test_the_step_list_is_described_as_an_array_of_objects():
    run_goal = next(tool for tool in _declared() if tool["name"] == "run_goal")
    steps = run_goal["parameters"]["properties"]["steps"]
    assert steps["type"] == "ARRAY"
    assert steps["items"]["type"] == "OBJECT"
    assert set(run_goal["parameters"]["required"]) == {"goal", "steps"}


# ── describe_devices ────────────────────────────────────────────────────────

def test_an_empty_registry_says_how_to_add_a_device():
    text = tools.describe_devices()

    assert "No devices are configured" in text
    assert "config/devices.json" in text


def test_a_configured_device_is_listed_with_the_names_it_answers_to():
    seed_lamp()

    text = tools.describe_devices()

    assert "Desk lamp" in text
    assert "[lamp]" in text
    assert "switch.on" in text and "level.set" in text


def test_a_device_turned_off_is_not_offered():
    registry.save([Device(id="lamp", name="Desk lamp", adapter="dry_run",
                          capabilities={"switch.on": {}}, enabled=False)])

    assert "No devices are configured" in tools.describe_devices()


# ── run_capability ──────────────────────────────────────────────────────────

def test_a_capability_call_quotes_the_task_that_recorded_it():
    seed_lamp()

    text = tools.run_capability({"device": "lamp", "capability": "switch.on"})

    assert "switch.on" in text
    task = graph.find(task_id_in(text))
    assert task is not None and task.state == graph.DONE
    assert graph.evidence(task.id)[0]["kind"] == "capability"


def test_the_reported_text_is_the_adapter_s_own_words():
    seed_lamp()

    text = tools.run_capability({"device": "Desk lamp", "capability": "switch.on"})

    # dry run says what it *would* have done, and that is what gets reported.
    assert "[dry run]" in text


def test_a_refusal_is_blocked_rather_than_failed():
    seed_lamp()
    registry_result = {"ok": False, "error": "REFUSED",
                       "detail": "Locking needs you to say yes."}

    import core.capability_registry as module
    original = module.execute
    module.execute = lambda *a, **k: dict(registry_result)
    try:
        text = tools.run_capability({"device": "lamp", "capability": "switch.on"})
    finally:
        module.execute = original

    assert "needs you to say yes" in text
    assert graph.find(task_id_in(text)).state == graph.BLOCKED


def test_an_unsupported_capability_names_what_the_device_does_offer():
    seed_lamp()

    text = tools.run_capability({"device": "lamp", "capability": "lock.unlock"})

    assert "cannot lock.unlock" in text
    assert "switch.on" in text


def test_an_unknown_device_suggests_the_ones_that_do_exist():
    seed_lamp()

    text = tools.run_capability({"device": "fridge", "capability": "switch.on"})

    assert "no device called 'fridge'" in text
    assert "Desk lamp" in text


def test_a_half_given_call_asks_for_the_missing_half():
    assert "device and a capability" in tools.run_capability({"device": "lamp"})
    assert "device and a capability" in tools.run_capability({})


def test_a_crashing_registry_is_reported_not_raised():
    import core.capability_registry as module
    original = module.execute

    def explode(*_a, **_k):
        raise RuntimeError("adapter vanished")

    module.execute = explode
    try:
        text = tools.run_capability({"device": "lamp", "capability": "switch.on"})
    finally:
        module.execute = original

    assert "adapter vanished" in text


# ── run_goal ────────────────────────────────────────────────────────────────

def fake_report(monkeypatch, **payload):
    base = {"ok": False, "goal": "g", "task_id": "t-abc123", "detail": "",
            "attempts": [], "needs_confirm": False, "blocked": "",
            "steps_done": 0}
    base.update(payload)
    seen: dict = {}

    def fake(goal, steps, **kwargs):
        seen["goal"] = goal
        seen["steps"] = steps
        seen.update(kwargs)
        return dict(base)

    monkeypatch.setattr(gui_agent, "run_gui_task", fake)
    return seen


def test_a_verified_run_says_so(monkeypatch):
    fake_report(monkeypatch, ok=True, detail="2 step(s) done and verified")

    text = tools.run_goal({"goal": "draw a cube", "steps": [
        {"target": "Add", "action": "click"}]})

    assert "2 step(s) done and verified" in text
    assert "t-abc123" in text


def test_the_step_list_reaches_the_agent(monkeypatch):
    seen = fake_report(monkeypatch, ok=True)
    steps = [{"target": "Add", "action": "click"}]

    tools.run_goal({"goal": "draw a cube", "steps": steps, "seconds": 30})

    assert seen["steps"] == steps
    assert seen["seconds"] == 30
    assert seen["confirmed"] is False


def test_a_run_that_needs_a_yes_is_parked_not_started(monkeypatch):
    fake_report(monkeypatch, needs_confirm=True,
                detail="'delete the file' can't be undone, so I won't start it without a yes.")

    text = tools.run_goal({"goal": "delete the file", "steps": [
        {"target": "Delete", "action": "click"}]})

    assert "parked" in text
    assert "t-abc123" in text


def test_a_refused_run_says_who_refused_it(monkeypatch):
    fake_report(monkeypatch, blocked="A guest may not drive the screen.")

    text = tools.run_goal({"goal": "open the bank", "steps": [
        {"target": "Open", "action": "click"}]})

    assert "didn't start it" in text
    assert "guest" in text


def test_a_failed_run_names_the_step_it_stopped_at(monkeypatch):
    fake_report(monkeypatch, ok=False, detail="it did not finish", attempts=[
        {"ok": True, "target": "Add", "detail": "verified"},
        {"ok": False, "target": "Save", "detail": "I can't find 'Save' on screen"},
    ])

    text = tools.run_goal({"goal": "draw a cube", "steps": [
        {"target": "Add", "action": "click"},
        {"target": "Save", "action": "click"}]})

    assert "Save" in text
    assert "can't find" in text


def test_a_goal_without_steps_is_asked_to_name_one(monkeypatch):
    fake_report(monkeypatch, ok=True)

    text = tools.run_goal({"goal": "draw a cube"})

    assert "at least one step" in text


def test_a_step_list_given_as_json_text_is_accepted(monkeypatch):
    seen = fake_report(monkeypatch, ok=True)

    tools.run_goal({"goal": "draw", "steps": '[{"target": "Add", "action": "click"}]'})

    assert seen["steps"] == [{"target": "Add", "action": "click"}]


def test_a_goal_without_a_goal_is_asked_for_one(monkeypatch):
    fake_report(monkeypatch, ok=True)

    assert "goal in a sentence" in tools.run_goal({"steps": [{"target": "x", "action": "click"}]})


def test_a_broken_agent_is_reported_not_raised(monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("no display")

    monkeypatch.setattr(gui_agent, "run_gui_task", explode)

    text = tools.run_goal({"goal": "draw", "steps": [{"target": "x", "action": "click"}]})

    assert "no display" in text


# ── task_tree ───────────────────────────────────────────────────────────────

def test_an_empty_tree_says_nothing_has_been_asked():
    assert "Nothing has been asked" in tools.task_tree()


def test_open_work_is_counted_and_named():
    task = graph.create("wire the lamp")

    text = tools.task_tree()

    assert "1 task(s)" in text
    assert "wire the lamp" in text
    assert task.id in text, "without the id there is nothing to point at next"


def test_a_child_is_shown_under_its_parent():
    parent = graph.create("prepare the print")
    graph.create("heat the bed", parent_id=parent.id)

    text = tools.task_tree()

    assert "  [" in text, "a nested task should be indented under its parent"
    assert "heat the bed" in text


def test_one_task_shows_its_budget_and_its_proof():
    task = graph.create("print the bracket", steps=3)
    graph.start(task.id)
    graph.add_step(task.id, "slice the model")
    graph.add_evidence(task.id, "file", "bracket.gcode written, 42 layers")

    text = tools.task_tree({"task_id": task.id})

    assert "print the bracket" in text
    assert "1/3 steps" in text
    assert "[file]" in text
    assert "bracket.gcode" in text


def test_an_unfinished_task_says_it_has_no_proof_yet():
    task = graph.create("slice the model")
    graph.start(task.id)

    assert "none yet" in tools.task_tree({"task_id": task.id})


def test_an_unknown_task_id_is_reported():
    text = tools.task_tree({"task_id": "t-nope"})

    assert "no task called 't-nope'" in text


def test_stopping_without_a_task_offers_the_open_ones():
    task = graph.create("wire the lamp")

    text = tools.task_tree({"stop": True})

    assert task.id in text
    assert "wire the lamp" in text


def test_stopping_with_nothing_running_says_so():
    assert "nothing running" in tools.task_tree({"stop": True})


def test_stopping_one_task_stops_what_is_under_it():
    parent = graph.create("prepare the print")
    graph.create("heat the bed", parent_id=parent.id)

    text = tools.task_tree({"task_id": parent.id, "stop": True})

    assert "Stopped 2 tasks" in text
    assert all(item.terminal for item in graph.subtree(parent.id))


def test_stopping_something_that_is_gone_is_reported():
    assert "couldn't stop" in tools.task_tree({"task_id": "t-nope", "stop": True})
