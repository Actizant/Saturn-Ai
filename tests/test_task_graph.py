"""The task tree: inherited budget, isolated failure, one stop, real evidence.

The tests are grouped by the four rules in the module docstring, because those
rules are the reason the tree exists at all.
"""

from __future__ import annotations

import time

import pytest

from core import task_graph as graph
from core.task_graph import TaskError


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(graph.ENV_PATH, str(tmp_path / "tasks.json"))
    graph.reset()
    yield
    graph.on_change(None)
    graph.reset()


def open_task(goal="slice the model", **kwargs):
    task = graph.create(goal, **kwargs)
    graph.start(task.id)
    return task


# ── Open, run, close ───────────────────────────────────────────────────────

def test_a_task_needs_a_goal():
    with pytest.raises(TaskError):
        graph.create("   ")


def test_a_task_starts_pending_and_becomes_running():
    task = graph.create("start the print")
    assert task.state == graph.PENDING

    graph.start(task.id)
    assert task.state == graph.RUNNING
    assert task.started > 0


def test_an_unknown_task_is_an_error_not_a_none():
    with pytest.raises(TaskError):
        graph.get("t-does-not-exist")
    assert graph.find("t-does-not-exist") is None


def test_a_task_cannot_finish_without_saying_what_happened():
    task = open_task()

    with pytest.raises(TaskError) as error:
        graph.complete(task.id)

    assert "evidence" in str(error.value)
    assert task.state == graph.RUNNING


def test_a_task_finishes_with_evidence():
    task = open_task()

    graph.complete(task.id, {"kind": "file", "detail": "cube.gcode written"})

    assert task.state == graph.DONE
    assert task.ended > 0
    assert graph.evidence(task.id)[0]["detail"] == "cube.gcode written"


def test_a_failure_records_its_reason():
    task = open_task()
    graph.fail(task.id, "the slicer is not installed")

    assert task.state == graph.FAILED
    assert "slicer" in task.error


def test_a_blocked_task_is_not_an_error():
    task = open_task()
    graph.block(task.id, "the owner has not approved this")

    assert task.state == graph.BLOCKED
    assert task.state not in (graph.FAILED,)


def test_a_finished_task_cannot_be_restarted():
    task = open_task()
    graph.fail(task.id, "no")

    with pytest.raises(TaskError):
        graph.start(task.id)


def test_a_question_parks_a_task_until_it_is_answered():
    task = open_task()

    graph.wait(task.id, "shall I start the print?")
    assert task.state == graph.WAITING
    assert task.meta["question"] == "shall I start the print?"

    graph.resume(task.id)
    assert task.state == graph.RUNNING
    assert "question" not in task.meta


# ── Rule 1: inheritance ────────────────────────────────────────────────────

def test_a_child_cannot_outlive_its_parent():
    parent = graph.create("set up the printer", seconds=60)

    child = graph.create("heat the nozzle", parent_id=parent.id, seconds=3600)

    assert child.deadline <= parent.deadline
    assert child.depth == 1


def test_a_child_cannot_borrow_more_steps_than_the_parent_has_left():
    parent = graph.create("print everything", steps=5)
    child = graph.create("do one part", parent_id=parent.id, steps=99)

    assert child.max_steps <= 5


def test_nesting_stops_at_four_levels():
    root = graph.create("one")
    second = graph.create("two", parent_id=root.id)
    third = graph.create("three", parent_id=second.id)
    fourth = graph.create("four", parent_id=third.id)

    assert third.depth == 2
    assert fourth.depth == 3
    with pytest.raises(TaskError):
        graph.create("five", parent_id=fourth.id)


def test_a_child_of_a_finished_task_is_refused():
    parent = open_task()
    graph.fail(parent.id, "done")

    with pytest.raises(TaskError):
        graph.create("too late", parent_id=parent.id)


def test_a_child_of_a_missing_task_is_refused():
    with pytest.raises(TaskError):
        graph.create("orphan", parent_id="t-ghost")


def test_the_step_allowance_is_enforced():
    task = open_task()
    task.max_steps = 2

    graph.add_step(task.id, "first")
    graph.add_step(task.id, "second")

    assert "limited to 2 steps" in graph.over_budget(task.id)
    with pytest.raises(TaskError):
        graph.add_step(task.id, "third")


def test_an_expired_deadline_stops_new_steps():
    task = open_task()
    task.deadline = time.time() - 1

    assert "time allowance" in graph.over_budget(task.id)
    with pytest.raises(TaskError):
        graph.add_step(task.id, "too late")


def test_a_child_is_stopped_when_its_parent_runs_out():
    parent = graph.create("parent", seconds=60)
    child = graph.create("child", parent_id=parent.id)
    parent.deadline = time.time() - 1

    assert "parent stopped" in graph.over_budget(child.id)
    with pytest.raises(TaskError):
        graph.add_step(child.id, "nope")


def test_the_budget_report_says_what_is_left():
    task = open_task(seconds=120)
    graph.add_step(task.id, "one")

    report = graph.budget(task.id)

    assert report["steps_used"] == 1
    assert report["max_depth"] == 3
    assert 0 < report["remaining_seconds"] <= 120
    assert report["spent"] is False


# ── Rule 2: isolated failure ───────────────────────────────────────────────

def test_a_failed_child_leaves_its_siblings_and_parent_alone():
    parent = open_task("prepare the print")
    slicing = graph.create("slice", parent_id=parent.id)
    heating = graph.create("heat", parent_id=parent.id)
    graph.start(slicing.id)
    graph.start(heating.id)

    graph.fail(slicing.id, "the model has no manifold edges")

    assert slicing.state == graph.FAILED
    assert heating.state == graph.RUNNING
    assert parent.state == graph.RUNNING


def test_the_parent_can_still_finish_after_a_child_failed():
    parent = open_task("prepare the print")
    child = graph.create("slice", parent_id=parent.id)
    graph.fail(child.id, "slicer crashed")

    graph.complete(parent.id, {"kind": "note", "detail": "printed from the old gcode"})

    assert parent.state == graph.DONE


# ── Rule 3: one stop ───────────────────────────────────────────────────────

def test_cancelling_a_parent_stops_the_whole_branch():
    root = open_task("prepare the print")
    slice_task = graph.create("slice", parent_id=root.id)
    heat_task = graph.create("heat", parent_id=root.id)
    nested = graph.create("verify temperature", parent_id=heat_task.id)
    for task in (slice_task, heat_task, nested):
        graph.start(task.id)

    changed = graph.cancel(root.id, "the owner said stop")

    assert set(changed) == {root.id, slice_task.id, heat_task.id, nested.id}
    assert all(task.state == graph.CANCELLED for task in
               (root, slice_task, heat_task, nested))


def test_cancelling_one_branch_leaves_the_other_running():
    root = open_task()
    left = graph.create("left", parent_id=root.id)
    right = graph.create("right", parent_id=root.id)
    graph.start(left.id)
    graph.start(right.id)

    graph.cancel(left.id)

    assert left.state == graph.CANCELLED
    assert right.state == graph.RUNNING
    assert root.state == graph.RUNNING


def test_cancelling_does_not_reopen_finished_work():
    root = open_task()
    finished = graph.create("already done", parent_id=root.id)
    graph.start(finished.id)
    graph.complete(finished.id, {"kind": "note", "detail": "ok"})

    changed = graph.cancel(root.id)

    assert changed == [root.id]
    assert finished.state == graph.DONE


# ── Rule 4: evidence ───────────────────────────────────────────────────────

def test_a_step_can_carry_its_own_evidence():
    task = open_task()
    step = graph.add_step(task.id, "slice cube.stl")

    graph.finish_step(task.id, step.id, ok=True,
                      evidence={"kind": "file", "detail": "cube.gcode"})

    assert step.state == graph.DONE
    assert step.evidence[0]["detail"] == "cube.gcode"
    assert graph.evidence(task.id)[0]["kind"] == "file"


def test_a_failed_step_keeps_its_error():
    task = open_task()
    step = graph.add_step(task.id, "slice")

    graph.finish_step(task.id, step.id, ok=False, error="no manifold edges")

    assert step.state == graph.FAILED
    assert "manifold" in step.error


def test_finishing_an_unknown_step_is_an_error():
    task = open_task()
    with pytest.raises(TaskError):
        graph.finish_step(task.id, "s-ghost")


def test_adding_a_step_opens_the_task():
    task = graph.create("do it")
    assert task.state == graph.PENDING

    graph.add_step(task.id, "first thing")

    assert task.state == graph.RUNNING
    assert task.started > 0


def test_evidence_is_timestamped_and_attributed():
    task = open_task()
    record = graph.add_evidence(task.id, "http", "HTTP 200", {"status": 200})

    assert record["at"] > 0
    assert record["task"] == task.id
    assert record["data"]["status"] == 200


# ── Reading the tree ───────────────────────────────────────────────────────

def test_the_tree_reads_as_an_outline():
    root = open_task("prepare the print")
    child = graph.create("slice", parent_id=root.id)
    graph.start(child.id)
    graph.add_step(child.id, "running the slicer")

    text = graph.tree(root.id)

    assert "[>] prepare the print (running)" in text
    assert "  [>] slice (running)" in text


def test_children_are_listed_under_their_parent():
    root = open_task()
    graph.create("a", parent_id=root.id)
    graph.create("b", parent_id=root.id)

    assert [task.goal for task in graph.children(root.id)] == ["a", "b"]
    assert len(graph.subtree(root.id)) == 3


def test_the_summary_counts_states_and_evidence():
    root = open_task()
    graph.complete(root.id, {"kind": "note", "detail": "ok"})
    open_task("still going")

    report = graph.summary()

    assert report["total"] == 2
    assert report["states"][graph.DONE] == 1
    assert report["live"] == 1
    assert report["evidence"] == 1


def test_recent_is_newest_first():
    first = graph.create("first")
    second = graph.create("second")

    assert [task.id for task in graph.recent(2)] == [second.id, first.id]


# ── Persistence ────────────────────────────────────────────────────────────

def test_tasks_survive_a_restart():
    task = open_task("print the cube")
    graph.add_evidence(task.id, "note", "started")
    graph.save()

    graph.reset()
    assert graph.load() == 1

    restored = graph.get(task.id)
    assert restored.goal == "print the cube"
    assert restored.evidence[0]["detail"] == "started"


def test_a_running_task_comes_back_interrupted_and_says_so():
    task = open_task("print the cube")
    graph.save()

    graph.reset()
    graph.load()

    restored = graph.get(task.id)
    assert restored.state == graph.INTERRUPTED
    assert "stopped before this finished" in restored.error


def test_an_interrupted_task_can_be_resumed():
    task = open_task("print the cube")
    graph.save()
    graph.reset()
    graph.load()

    graph.resume(task.id)

    assert graph.get(task.id).state == graph.RUNNING


def test_a_broken_file_does_not_stop_startup(tmp_path):
    path = graph._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert graph.load() == 0


def test_loading_an_absent_file_is_zero_tasks():
    assert graph.load() == 0


# ── Listeners ──────────────────────────────────────────────────────────────

def test_a_listener_hears_every_change():
    seen = []
    graph.on_change(lambda task: seen.append((task.id, task.state)))

    task = open_task()

    assert (task.id, graph.PENDING) in seen
    assert (task.id, graph.RUNNING) in seen


def test_a_broken_listener_cannot_break_the_work():
    def angry(task):
        raise RuntimeError("ui went away")

    graph.on_change(angry)
    task = open_task()          # must not raise

    assert task.state == graph.RUNNING


def test_a_listener_can_be_removed():
    seen = []
    graph.on_change(lambda task: seen.append(task.id))
    graph.on_change(None)

    open_task()

    assert seen == []
