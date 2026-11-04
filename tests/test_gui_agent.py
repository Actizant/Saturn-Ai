"""The look/act/verify loop. An action is not done until the screen says so.

Every test here drives the loop with a scripted pair of eyes and a scripted
hand, so the behaviour under test is the policy - when to move, when to refuse,
and what counts as proof - not the desktop.
"""

from __future__ import annotations

import pytest

from core import gui_agent, task_graph, ui_probe
from core.gui_agent import GuiAgent, verify
from core.ui_probe import Observation


def look(title="untitled - Blender", elements=(), text="", process="blender.exe"):
    return Observation(
        window_title=title,
        process=process,
        elements=[{"name": name, "type": "Button", "box": [10, 20, 80, 24]}
                  for name in elements],
        text=text,
    )


class Eyes:
    """Returns each scripted look in turn, repeating the last one."""

    def __init__(self, *observations):
        self.looks = list(observations)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        index = min(self.calls - 1, len(self.looks) - 1)
        return self.looks[index]


class Hands:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"ok": True, "detail": "sent"}

    def __call__(self, action, params):
        self.calls.append((action, dict(params)))
        return self.result


def agent(eyes, hands=None, settle=0.0):
    return GuiAgent(eyes=eyes, hands=hands or Hands(), settle=settle,
                    sleeper=lambda seconds: None)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(task_graph.ENV_PATH, str(tmp_path / "tasks.json"))
    task_graph.reset()
    monkeypatch.setattr("core.trust_gate.evaluate",
                        lambda *a, **k: gui_agent_allow())
    yield
    task_graph.reset()


def gui_agent_allow(capability="desktop.control"):
    from core.trust_gate import Decision, Trust

    return Decision(allowed=True, capability=capability, trust=Trust.OWNER,
                    verified=True)


# ── Verification, on its own ───────────────────────────────────────────────

def test_a_changed_screen_verifies():
    before = look(elements=["Render"])
    after = look(elements=["Render"], text="rendering")

    assert verify({"changed": True}, before, after)["ok"] is True


def test_an_unchanged_screen_does_not_verify():
    same = look(elements=["Render"])
    verdict = verify({"changed": True}, same, same)

    assert verdict["ok"] is False
    assert "changed" in verdict["reason"]


def test_expected_text_must_appear():
    verdict = verify({"text_contains": "Saved"},
                     look(), look(text="File saved successfully"))
    assert verdict["ok"] is True

    verdict = verify({"text_contains": "Saved"}, look(), look(text="File opened"))
    assert verdict["ok"] is False
    assert "missing" in verdict["reason"]


def test_text_that_should_be_gone_must_be_gone():
    verdict = verify({"text_absent": "Rendering"}, look(),
                     look(text="Rendering complete"))
    assert verdict["ok"] is False


def test_an_expected_control_must_be_present():
    verdict = verify({"element": "Cancel"}, look(elements=["Save"]),
                     look(elements=["Save", "Cancel"]))
    assert verdict["ok"] is True


def test_a_control_that_should_have_vanished_must_be_gone():
    verdict = verify({"element_absent": "Render"}, look(),
                     look(elements=["Render"]))
    assert verdict["ok"] is False
    assert "still on screen" in verdict["reason"]


def test_a_blind_screen_can_never_verify_a_change():
    """Silence is not success: this is the whole point of the module."""
    blind = Observation(window_title="Game")

    verdict = verify({"changed": True}, blind, blind)

    assert verdict["ok"] is False
    assert "could not be read" in verdict["reason"]


def test_an_unknown_check_is_a_failure_not_a_pass():
    verdict = verify({"looks_about_right": True}, look(), look(text="x"))
    assert verdict["ok"] is False
    assert "do not know how to check" in verdict["reason"]


def test_several_checks_must_all_pass():
    verdict = verify({"text_contains": "Done", "element_absent": "Cancel"},
                     look(), look(elements=["Save"], text="Done"))
    assert verdict["ok"] is True
    assert len(verdict["checks"]) == 2


# ── Refusals before anything moves ─────────────────────────────────────────

def test_a_destructive_goal_is_parked_not_started():
    hands = Hands()
    report = agent(Eyes(look()), hands).run("delete every old render", [])

    assert report.needs_confirm is True
    assert hands.calls == []
    assert report.task_id
    assert task_graph.get(report.task_id).state == task_graph.WAITING


def test_the_parked_task_can_be_resumed_with_a_yes():
    eyes = Eyes(look(elements=["Render"]), look(elements=["Render"], text="done"))
    hands = Hands()
    driver = agent(eyes, hands)

    parked = driver.run("delete the old renders", [])
    resumed = driver.run("delete the old renders",
                         [{"target": "Render", "expect": {"changed": True}}],
                         task_id=parked.task_id, confirmed=True)

    assert resumed.ok is True
    assert hands.calls


def test_a_trust_refusal_stops_before_the_first_look(monkeypatch):
    from core.trust_gate import Decision, Trust

    monkeypatch.setattr("core.trust_gate.evaluate", lambda *a, **k: Decision(
        allowed=False, capability="desktop.control", trust=Trust.UNKNOWN,
        verified=True, policy_allowed=False, reason="stranger",
        spoken="I don't recognise who's asking."))
    eyes = Eyes(look())
    report = agent(eyes, Hands()).run("open blender", [{"target": "Render"}])

    assert report.ok is False
    assert "recognise" in report.blocked
    assert eyes.calls == 0


def test_an_empty_goal_is_refused():
    assert agent(Eyes(look())).run("  ", []).detail


def test_no_steps_says_so_rather_than_reporting_success():
    report = agent(Eyes(look())).run("open blender", [])

    assert report.ok is False
    assert "nothing to do" in report.detail


# ── The loop ───────────────────────────────────────────────────────────────

def test_a_found_control_is_clicked_at_its_centre():
    eyes = Eyes(look(elements=["Render"]),
                look(elements=["Render"], text="rendering started"))
    hands = Hands()

    report = agent(eyes, hands).run("start a render", [
        {"target": "Render", "expect": {"text_contains": "rendering started"}}])

    assert report.ok is True
    action, params = hands.calls[0]
    assert action == "click"
    assert (params["x"], params["y"]) == (50, 32)


def test_a_missing_target_is_never_clicked():
    hands = Hands()
    report = agent(Eyes(look(elements=["Cancel"]), look(elements=["Cancel"])),
                   hands).run("start a render", [{"target": "Render"}])

    assert report.ok is False
    assert "can't find" in report.detail
    assert hands.calls == []


def test_the_loop_looks_again_before_giving_up():
    eyes = Eyes(look(elements=["Cancel"]),
                look(elements=["Cancel"]),
                look(elements=["Render"]),
                look(elements=["Render"], text="done"))
    hands = Hands()

    report = agent(eyes, hands).run("start a render", [
        {"target": "Render", "expect": {"text_contains": "done"}}])

    assert report.ok is True
    assert hands.calls


def test_a_failed_verification_is_retried_then_reported():
    eyes = Eyes(look(elements=["Render"]), look(elements=["Render"]))
    report = agent(eyes, Hands()).run(
        "start a render",
        [{"target": "Render", "expect": {"text_contains": "rendering started"}}],
        max_attempts=2)

    assert report.ok is False
    assert len(report.attempts) == 2
    assert "missing" in report.detail


def test_hands_that_refuse_stop_the_attempt_early():
    hands = Hands(result={"ok": False, "detail": "the window has no focus"})
    eyes = Eyes(look(elements=["Render"]))

    report = agent(eyes, hands).run("start a render",
                                    [{"target": "Render"}], max_attempts=3)

    assert report.ok is False
    assert "no focus" in report.detail
    assert len(hands.calls) == 1


def test_a_keyboard_step_needs_no_target():
    eyes = Eyes(look(elements=["Save"]), look(elements=["Save"], text="Saved"))
    hands = Hands()

    report = agent(eyes, hands).run("save the file", [
        {"action": "hotkey", "params": {"keys": "ctrl+s"},
         "expect": {"text_contains": "Saved"}}])

    assert report.ok is True
    assert hands.calls[0] == ("hotkey", {"keys": "ctrl+s"})


def test_several_steps_run_in_order():
    eyes = Eyes(look(elements=["Save"]),
                look(elements=["Save"], text="saved"),
                look(elements=["Close"], text="saved"),
                look(text="Goodbye"))
    hands = Hands()

    report = agent(eyes, hands).run("save and close", [
        {"target": "Save", "expect": {"text_contains": "saved"}},
        {"target": "Close", "expect": {"text_contains": "Goodbye"}},
    ])

    assert report.ok is True
    assert report.steps_done == 2
    assert [call[1].get("x") for call in hands.calls] == [50, 50]


def test_a_later_step_fails_after_an_earlier_one_succeeded():
    eyes = Eyes(look(elements=["Save"]),
                look(elements=["Save"], text="saved"),
                look(elements=["Cancel"], text="saved"),
                look(elements=["Cancel"], text="saved"))
    report = agent(eyes, Hands()).run("save and close", [
        {"target": "Save", "expect": {"text_contains": "saved"}},
        {"target": "Close"},
    ])

    assert report.ok is False
    assert report.steps_done == 1


# ── Evidence and the task tree ─────────────────────────────────────────────

def test_a_verified_run_leaves_evidence_behind():
    eyes = Eyes(look(elements=["Render"]),
                look(elements=["Render"], text="rendering started"))

    report = agent(eyes, Hands()).run(
        "start a render",
        [{"target": "Render", "expect": {"text_contains": "rendering started"}}])

    task = task_graph.get(report.task_id)
    assert task.state == task_graph.DONE
    kinds = [record["kind"] for record in task.evidence]
    assert "gui-attempt" in kinds
    assert "gui" in kinds


def test_a_failed_run_closes_the_task_as_failed_not_done():
    eyes = Eyes(look(elements=["Render"]), look(elements=["Render"]))
    hands = Hands()

    report = agent(eyes, hands).run("start a render", [{"target": "Render"}],
                                    max_attempts=1)

    assert task_graph.get(report.task_id).state == task_graph.FAILED


def test_every_attempt_is_recorded_even_the_failed_ones():
    eyes = Eyes(look(elements=["Render"]), look(elements=["Render"]))
    report = agent(eyes, Hands()).run("start a render", [{"target": "Render"}],
                                      max_attempts=2)

    assert len(report.attempts) == 2
    assert all(attempt.found for attempt in report.attempts)
    assert all(attempt.verdict["ok"] is False for attempt in report.attempts)


def test_a_run_nests_under_whatever_asked_for_it():
    parent = task_graph.create("prepare the print")
    eyes = Eyes(look(elements=["Slice"]), look(elements=["Slice"], text="done"))

    report = agent(eyes, Hands()).run("slice the model",
                                      [{"target": "Slice",
                                        "expect": {"text_contains": "done"}}],
                                      parent_id=parent.id)

    assert task_graph.get(report.task_id).parent_id == parent.id


def test_a_spent_parent_budget_stops_the_child():
    """A child inherits what is *left* of the parent, never more.

    Steps do not accumulate upwards - a child finishing its work does not spend
    the parent's allowance - so the way to demonstrate inheritance is to spend
    the parent's budget first and watch the child refuse to start.
    """
    parent = task_graph.create("budget", steps=1)
    task_graph.add_step(parent.id, "the only step it had")

    hands = Hands()
    report = agent(Eyes(look(elements=["Slice"])), hands).run(
        "slice the model", [{"target": "Slice"}], parent_id=parent.id)

    assert report.ok is False
    assert "parent stopped" in report.detail
    assert "limited to 1" in report.detail
    assert hands.calls == []


def test_the_report_serialises_for_a_tool_result():
    report = agent(Eyes(look(elements=["Render"]), look(elements=["Render"],
                                                        text="ok"))).run(
        "start a render", [{"target": "Render", "expect": {"text_contains": "ok"}}])

    payload = report.as_dict()

    assert payload["ok"] is True
    assert payload["steps_done"] == 1
    assert payload["attempts"][0]["found"]["source"] == "control"


def test_run_gui_task_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(gui_agent.GuiAgent, "run", boom)
    payload = gui_agent.run_gui_task("do it", [{"target": "x"}])

    assert payload["ok"] is False
    assert "exploded" in payload["detail"]


# ── Destructive verbs ──────────────────────────────────────────────────────

@pytest.mark.parametrize("goal", [
    "delete the old renders",
    "uninstall Blender",
    "pay the invoice",
    "send the email",
    "shutdown the machine",
])
def test_destructive_goals_are_recognised(goal):
    assert gui_agent.is_destructive(goal) is True


@pytest.mark.parametrize("goal", [
    "open blender and draw a sphere",
    "read the current window",
    "render the animation",
    "reorder the layers",
])
def test_ordinary_goals_are_not_destructive(goal):
    assert gui_agent.is_destructive(goal) is False
