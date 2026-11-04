"""Everything is a task. Every task leaves evidence. One stop stops the branch.

WHY A TREE AND NOT A PLAN
    ``core.omni_planner`` already decomposes a goal into steps, and
    ``agent/executor`` already loops over them with retries. What neither could
    answer is the question that only appears once work nests:

        "set up the printer, and while it heats up, pull the model out of the
         folder and slice it, then start the print"

    That is three tasks, one of which must not outlive its parent, one of which
    may fail without killing the others, and all of which must stop together
    when the owner says stop. A flat step list cannot express any of it.

THE FOUR RULES
    1. **Inherited budget.** A child gets whatever is *left* of its parent's
       deadline and step allowance, never a fresh one. Ten nested tasks cannot
       add up to more time than the one the owner actually asked for.
    2. **Isolated failure.** A failed child fails only its own branch. The
       parent sees ``failed_children`` and decides whether to continue.
    3. **One stop.** ``cancel`` marks the whole subtree; every task checks
       ``stopped`` before it does anything else.
    4. **Evidence or it did not happen.** Completion without a record is
       rejected, because "I started the print" is exactly the sentence that
       must never be said on the strength of a hope.

WHY IT PERSISTS
    The machine restarts, the app is killed, the owner walks away mid-print. A
    task that was ``RUNNING`` when the process died is not ``RUNNING`` now - it
    is ``INTERRUPTED``, and saying so is the difference between an assistant
    that lies about finished work and one that can be trusted with it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ── States ──────────────────────────────────────────────────────────────────

PENDING = "pending"
RUNNING = "running"
WAITING = "waiting"           # a question is outstanding: "shall I start the print?"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
BLOCKED = "blocked"           # refused by policy, not by an error
INTERRUPTED = "interrupted"   # was running when the process died

TERMINAL = frozenset({DONE, FAILED, CANCELLED, BLOCKED, INTERRUPTED})
LIVE = frozenset({PENDING, RUNNING, WAITING})

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_STEPS = 40
DEFAULT_SECONDS = 900.0


class TaskError(Exception):
    """A refusal to change something that cannot be changed."""


# ── Records ─────────────────────────────────────────────────────────────────

@dataclass
class Step:
    id: str
    summary: str
    state: str = PENDING
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    error: str = ""
    evidence: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Task:
    id: str
    goal: str
    parent_id: str = ""
    depth: int = 0
    state: str = PENDING
    steps: List[Step] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    started: float = 0.0
    ended: float = 0.0
    deadline: float = 0.0
    max_steps: int = DEFAULT_MAX_STEPS
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    # -- derived ---------------------------------------------------------

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def steps_used(self) -> int:
        return len(self.steps)

    @property
    def expired(self) -> bool:
        return bool(self.deadline) and time.time() > self.deadline

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.time()) if self.deadline else DEFAULT_SECONDS

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.as_dict() for step in self.steps]
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Task":
        steps = [Step(**{key: value for key, value in step.items()
                         if key in Step.__dataclass_fields__})
                 for step in raw.get("steps") or [] if isinstance(step, dict)]
        fields = {key: value for key, value in raw.items()
                  if key in cls.__dataclass_fields__ and key != "steps"}
        return cls(steps=steps, **fields)


# ── The graph ───────────────────────────────────────────────────────────────

_lock = threading.RLock()
_tasks: Dict[str, Task] = {}
_change_callbacks: List[Callable[[Task], None]] = []

ENV_PATH = "SATURN_TASKS_FILE"
MAX_TASKS = 200


def _path() -> Path:
    override = os.environ.get(ENV_PATH)
    if override:
        return Path(override)
    try:
        from core.user_paths import get_user_data_dir

        return get_user_data_dir() / "config" / "tasks.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "config" / "tasks.json"


def _new_id(prefix: str = "t") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _emit(task: Task) -> None:
    for callback in list(_change_callbacks):
        try:
            callback(task)
        except Exception:
            # A broken listener must never break the work it was listening to.
            pass


def on_change(callback: Optional[Callable[[Task], None]]) -> None:
    """Subscribe (or pass None to clear) to task state changes."""
    _change_callbacks.clear()
    if callback is not None:
        _change_callbacks.append(callback)


# ── Creation ────────────────────────────────────────────────────────────────

def create(goal: str, parent_id: str = "", *, steps: int = DEFAULT_MAX_STEPS,
           seconds: Optional[float] = None,
           meta: Optional[Dict[str, Any]] = None) -> Task:
    """Open a task, optionally inside another one.

    A child inherits the parent's *remaining* time and step allowance. The
    second argument that smaller is used for both, so nesting can shorten a
    budget but never extend it.
    """
    text = str(goal or "").strip()
    if not text:
        raise TaskError("a task needs a goal")

    with _lock:
        parent = _tasks.get(parent_id) if parent_id else None
        if parent_id and parent is None:
            raise TaskError(f"no parent task called '{parent_id}'")
        if parent is not None:
            if parent.terminal:
                raise TaskError(f"parent task is {parent.state}")
            if parent.depth + 1 > DEFAULT_MAX_DEPTH:
                raise TaskError(f"tasks may not nest deeper than {DEFAULT_MAX_DEPTH}")

        now = time.time()
        depth = parent.depth + 1 if parent else 0

        if parent is not None:
            deadline = min(parent.deadline or (now + DEFAULT_SECONDS),
                           now + float(seconds or DEFAULT_SECONDS))
            allowance = min(int(steps), max(1, parent.max_steps - parent.steps_used))
        else:
            deadline = now + float(seconds or DEFAULT_SECONDS)
            allowance = int(steps)

        task = Task(
            id=_new_id(),
            goal=text,
            parent_id=parent.id if parent else "",
            depth=depth,
            deadline=deadline,
            max_steps=allowance,
            meta=dict(meta or {}),
        )
        _tasks[task.id] = task
        _prune_locked()
        _persist_locked()

    _emit(task)
    return task


def start(task_id: str) -> Task:
    task = get(task_id)
    if task.terminal:
        raise TaskError(f"task is {task.state}")
    with _lock:
        if not task.started:
            task.started = time.time()
        task.state = RUNNING
        _persist_locked()
    _emit(task)
    return task


def complete(task_id: str, evidence: Optional[Dict[str, Any]] = None,
             detail: str = "") -> Task:
    """Finish a task. Completion without evidence is refused on purpose."""
    task = get(task_id)
    with _lock:
        if task.terminal:
            raise TaskError(f"task is already {task.state}")
        if evidence is not None:
            _add_evidence_locked(task, evidence)
        if not task.evidence:
            raise TaskError(
                "a finished task must leave evidence; "
                "say what actually happened before closing it")
        task.state = DONE
        task.ended = time.time()
        _persist_locked()
    _emit(task)
    return task


def fail(task_id: str, error: str, evidence: Optional[Dict[str, Any]] = None) -> Task:
    task = get(task_id)
    with _lock:
        if evidence is not None:
            _add_evidence_locked(task, evidence)
        task.state = FAILED
        task.error = str(error or "failed")
        task.ended = time.time()
        _persist_locked()
    _emit(task)
    return task


def block(task_id: str, reason: str) -> Task:
    """Refused by policy - not an error, and not something to retry."""
    task = get(task_id)
    with _lock:
        task.state = BLOCKED
        task.error = str(reason or "blocked")
        task.ended = time.time()
        _persist_locked()
    _emit(task)
    return task


def wait(task_id: str, question: str = "") -> Task:
    """Park a task on a question so a later turn can answer it."""
    task = get(task_id)
    with _lock:
        task.state = WAITING
        if question:
            task.meta["question"] = str(question)
        _persist_locked()
    _emit(task)
    return task


def resume(task_id: str) -> Task:
    task = get(task_id)
    if task.state not in (WAITING, INTERRUPTED, RUNNING):
        raise TaskError(f"task is {task.state}")
    with _lock:
        task.state = RUNNING
        task.meta.pop("question", None)
        task.ended = 0.0
        _persist_locked()
    _emit(task)
    return task


def cancel(task_id: str, reason: str = "") -> List[str]:
    """Stop a task *and everything under it*. Returns the ids that changed."""
    task = get(task_id)
    with _lock:
        affected = [item.id for item in _subtree_locked(task.id)
                    if not item.terminal]
        for item in _subtree_locked(task.id):
            if item.terminal:
                continue
            item.state = CANCELLED
            item.error = str(reason or "cancelled")
            item.ended = time.time()
        _persist_locked()
    _emit(task)
    return affected


# ── Steps ───────────────────────────────────────────────────────────────────

def add_step(task_id: str, summary: str) -> Step:
    task = get(task_id)
    reason = over_budget(task_id)
    if reason:
        raise TaskError(reason)
    if task.terminal:
        raise TaskError(f"task is {task.state}")
    with _lock:
        step = Step(id=_new_id("s"), summary=str(summary or "step").strip())
        task.steps.append(step)
        if task.state == PENDING:
            task.started = task.started or time.time()
            task.state = RUNNING
        _persist_locked()
    _emit(task)
    return step


def finish_step(task_id: str, step_id: str, ok: bool = True, *,
                evidence: Optional[Dict[str, Any]] = None,
                error: str = "") -> Step:
    task = get(task_id)
    step = next((item for item in task.steps if item.id == step_id), None)
    if step is None:
        raise TaskError(f"no step called '{step_id}'")
    with _lock:
        step.state = DONE if ok else FAILED
        step.ended = time.time()
        step.error = "" if ok else str(error or "failed")
        if evidence is not None:
            step.evidence.append(dict(evidence))
            _add_evidence_locked(task, evidence)
        _persist_locked()
    _emit(task)
    return step


# ── Evidence ────────────────────────────────────────────────────────────────

def _add_evidence_locked(task: Task, evidence: Dict[str, Any]) -> None:
    record = dict(evidence or {})
    record.setdefault("at", time.time())
    record.setdefault("task", task.id)
    task.evidence.append(record)


def add_evidence(task_id: str, kind: str = "note", detail: str = "",
                 data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    task = get(task_id)
    record = {"kind": str(kind or "note"), "detail": str(detail or ""),
              "data": dict(data or {})}
    with _lock:
        _add_evidence_locked(task, record)
        _persist_locked()
    _emit(task)
    return task.evidence[-1]


def evidence(task_id: str) -> List[Dict[str, Any]]:
    return list(get(task_id).evidence)


# ── Budget ──────────────────────────────────────────────────────────────────

def over_budget(task_id: str) -> str:
    """Why this task may not take another step, or "" if it may."""
    task = get(task_id)
    if task.terminal:
        return f"task is {task.state}"
    if task.deadline and time.time() > task.deadline:
        return "the time allowance for this task ran out"
    if task.steps_used >= task.max_steps:
        return f"this task is limited to {task.max_steps} steps"
    if task.parent_id:
        parent_reason = over_budget(task.parent_id)
        if parent_reason:
            return f"parent stopped: {parent_reason}"
    return ""


def budget(task_id: str) -> Dict[str, Any]:
    task = get(task_id)
    return {
        "depth": task.depth,
        "max_depth": DEFAULT_MAX_DEPTH,
        "steps_used": task.steps_used,
        "max_steps": task.max_steps,
        "remaining_seconds": round(task.remaining_seconds, 1),
        "spent": bool(over_budget(task_id)),
        "reason": over_budget(task_id),
    }


# ── Reading ─────────────────────────────────────────────────────────────────

def get(task_id: str) -> Task:
    with _lock:
        task = _tasks.get(task_id)
    if task is None:
        raise TaskError(f"no task called '{task_id}'")
    return task


def find(task_id: str) -> Optional[Task]:
    with _lock:
        return _tasks.get(task_id)


def children(task_id: str) -> List[Task]:
    with _lock:
        return [task for task in _tasks.values() if task.parent_id == task_id]


def _subtree_locked(task_id: str) -> List[Task]:
    found: List[Task] = []
    stack = [task_id]
    while stack:
        current = stack.pop()
        task = _tasks.get(current)
        if task is None:
            continue
        found.append(task)
        stack.extend(child_id for child_id, item in _tasks.items()
                     if item.parent_id == current)
    return found


def subtree(task_id: str) -> List[Task]:
    with _lock:
        return _subtree_locked(task_id)


def roots() -> List[Task]:
    with _lock:
        return [task for task in _tasks.values() if not task.parent_id]


def live() -> List[Task]:
    with _lock:
        return [task for task in _tasks.values() if not task.terminal]


def recent(limit: int = 10) -> List[Task]:
    with _lock:
        ordered = sorted(_tasks.values(), key=lambda item: item.created, reverse=True)
    return ordered[:max(1, int(limit))]


def summary() -> Dict[str, Any]:
    with _lock:
        everything = list(_tasks.values())
    counts: Dict[str, int] = {}
    for task in everything:
        counts[task.state] = counts.get(task.state, 0) + 1
    return {"total": len(everything), "states": counts,
            "live": sum(1 for task in everything if not task.terminal),
            "evidence": sum(len(task.evidence) for task in everything)}


def tree(task_id: Optional[str] = None, _depth: int = 0) -> str:
    """A readable outline, for a log panel or the model's own context."""
    lines: List[str] = []
    roots_to_show = [find(task_id)] if task_id else roots()
    for task in [item for item in roots_to_show if item is not None]:
        _render(task, lines, 0)
    return "\n".join(lines) if lines else "No tasks."


def _render(task: Task, lines: List[str], depth: int) -> None:
    mark = {"done": "x", "failed": "!", "cancelled": "-", "blocked": "#",
            "waiting": "?", "running": ">"}.get(task.state, ".")
    lines.append(f"{'  ' * depth}[{mark}] {task.goal} ({task.state})")
    for child in children(task.id):
        _render(child, lines, depth + 1)


# ── Persistence ─────────────────────────────────────────────────────────────

def _prune_locked() -> None:
    if len(_tasks) <= MAX_TASKS:
        return
    finished = sorted((task for task in _tasks.values() if task.terminal),
                      key=lambda item: item.ended or item.created)
    for task in finished[:max(0, len(_tasks) - MAX_TASKS)]:
        _tasks.pop(task.id, None)


def _persist_locked() -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "saved": time.time(),
                   "tasks": [task.as_dict() for task in _tasks.values()]}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    except Exception:
        # Losing the receipt is bad; losing the work because of the receipt is worse.
        pass


def load() -> int:
    """Read the previous session back. Returns how many tasks were restored."""
    global _tasks
    path = _path()
    if not path.exists():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("tasks") if isinstance(raw, dict) else raw
    except Exception:
        return 0

    restored: Dict[str, Task] = {}
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        try:
            task = Task.from_dict(row)
        except Exception:
            continue
        if task.state in (RUNNING, WAITING):
            # The process died while this was in flight. It did not finish; say so.
            task.state = INTERRUPTED
            task.error = task.error or "the process stopped before this finished"
            task.ended = task.ended or time.time()
        restored[task.id] = task

    with _lock:
        _tasks = restored
    return len(restored)


def reset() -> None:
    """Forget everything in memory, leaving the saved file alone.

    Deliberately does not write: ``reset`` is what a test (or a deliberate
    clean slate) uses before loading, and persisting an empty tree here would
    erase the very history it is about to read back.
    """
    with _lock:
        _tasks.clear()


def save() -> None:
    with _lock:
        _persist_locked()
