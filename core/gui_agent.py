"""Look, act, look again. The second look is the whole idea.

WHY THIS EXISTS
    Saturn could already click and type - ``actions/computer_control.py`` has
    twenty verbs and ``core/vision.py`` can read a window two ways. What it
    could not do is *know whether anything happened*. So it clicked, then
    reported success, and the report was a guess dressed as a fact.

    The fix is not more actions. It is one loop:

        observe -> find the thing -> act -> observe again -> compare

    An action is not finished when it is sent. It is finished when the screen
    says so. Every claim this module makes is backed by a before/after pair that
    went into the task's evidence.

WHAT IT REFUSES
    * **Moving first.** A goal that contains a destructive verb ("delete",
      "send", "pay", "uninstall") is returned unstarted with ``needs_confirm``
      set. Nothing is clicked while the question is outstanding.
    * **Clicking on a guess.** If the target is not on screen, the loop looks
      again (up to ``MAX_LOOKS``) and then says it could not find it. It does
      not click the nearest thing that looked similar.
    * **Reading silence as success.** If the window cannot be read at all -
      no accessibility tree, no legible text - verification reports "cannot
      confirm" rather than "done". Silently unverifiable is the one outcome
      that must never be dressed up as success.

HOW IT FITS THE REST
    Every run is a :mod:`core.task_graph` task, so it inherits a budget, nests
    under whatever asked for it, and stops with one cancel. The trust gate is
    asked once, before the first look, because a run is one act as far as
    authority is concerned.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core import task_graph
from core import ui_probe

SETTLE_SECONDS = float(os.environ.get("SATURN_GUI_SETTLE", "0.45"))
MAX_ATTEMPTS = 3
MAX_LOOKS = 3
GATE_CAPABILITY = "desktop.control"

# Actions whose whole point is that they cannot be usefully undone.
DESTRUCTIVE_VERBS = (
    "delete", "remove", "uninstall", "format", "erase", "wipe", "reset",
    "pay", "buy", "purchase", "order", "transfer", "send", "submit", "confirm",
    "shutdown", "restart", "overwrite",
)

_KEYBOARD_ONLY = frozenset({"type", "hotkey", "press", "wait", "scroll"})


# ── Records ─────────────────────────────────────────────────────────────────

@dataclass
class Action:
    """One thing to do, and what should be true afterwards."""

    target: str = ""
    act: str = "click"
    params: Dict[str, Any] = field(default_factory=dict)
    expect: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Action":
        return cls(
            target=str(raw.get("target") or ""),
            act=str(raw.get("action") or raw.get("act") or "click").lower(),
            params=dict(raw.get("params") or {}),
            expect=dict(raw.get("expect") or {}),
            summary=str(raw.get("summary") or ""),
        )

    @property
    def label(self) -> str:
        return self.summary or (f"{self.act} {self.target}".strip())


@dataclass
class Attempt:
    index: int
    target: str = ""
    found: Optional[Dict[str, Any]] = None
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    verdict: Dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    screenshot: str = ""
    refused: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.verdict.get("ok"))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "target": self.target,
            "found": self.found,
            "before": self.before,
            "after": self.after,
            "verdict": self.verdict,
            "detail": self.detail,
            "screenshot": self.screenshot,
            "refused": self.refused,
        }


@dataclass
class Report:
    ok: bool = False
    goal: str = ""
    task_id: str = ""
    detail: str = ""
    attempts: List[Attempt] = field(default_factory=list)
    needs_confirm: bool = False
    blocked: str = ""
    steps_done: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "goal": self.goal,
            "task_id": self.task_id,
            "detail": self.detail,
            "needs_confirm": self.needs_confirm,
            "blocked": self.blocked,
            "steps_done": self.steps_done,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }


# ── Verification ────────────────────────────────────────────────────────────

def _contains(haystack: str, needle: str) -> bool:
    return str(needle or "").lower() in str(haystack or "").lower()


def verify(expect: Dict[str, Any], before: "ui_probe.Observation",
           after: "ui_probe.Observation") -> Dict[str, Any]:
    """Did the screen change the way it was supposed to?

    Every check is explicit. With no expectation given the default is "it
    changed at all", which is weak but honest, and is never reported for a
    screen that could not be read.
    """
    checks: List[Dict[str, Any]] = []
    plan = dict(expect or {})
    if not plan:
        plan = {"changed": True}

    for key, value in plan.items():
        key = str(key).lower()
        if key == "changed":
            if not after.content_readable or not before.content_readable:
                checks.append({"check": "changed", "ok": False,
                               "detail": "the screen could not be read, so I cannot "
                                         "tell whether anything changed"})
                continue
            moved = before.digest() != after.digest()
            checks.append({"check": "changed", "ok": bool(moved) == bool(value),
                           "detail": f"before {before.digest()} / after {after.digest()}"})
        elif key == "text_contains":
            wanted = value if isinstance(value, list) else [value]
            missing = [item for item in wanted if not _contains(after.text, item)]
            checks.append({"check": "text_contains", "ok": not missing,
                           "detail": "" if not missing else f"missing: {missing}"})
        elif key == "text_absent":
            wanted = value if isinstance(value, list) else [value]
            present = [item for item in wanted if _contains(after.text, item)]
            checks.append({"check": "text_absent", "ok": not present,
                           "detail": "" if not present else f"still there: {present}"})
        elif key == "element":
            wanted = value if isinstance(value, list) else [value]
            names = after.names()
            missing = [item for item in wanted
                       if max((ui_probe.score(name, item) for name in names), default=0) < 60]
            checks.append({"check": "element", "ok": not missing,
                           "detail": "" if not missing else f"not on screen: {missing}"})
        elif key == "element_absent":
            wanted = value if isinstance(value, list) else [value]
            names = after.names()
            present = [item for item in wanted
                       if max((ui_probe.score(name, item) for name in names), default=0) >= 60]
            checks.append({"check": "element_absent", "ok": not present,
                           "detail": "" if not present else f"still on screen: {present}"})
        elif key == "title_contains":
            checks.append({"check": "title_contains",
                           "ok": _contains(after.window_title, value),
                           "detail": after.window_title})
        elif key == "rung":
            checks.append({"check": "rung", "ok": after.rung == value,
                           "detail": f"{after.rung} (wanted {value})"})
        else:
            checks.append({"check": key, "ok": False,
                           "detail": "I do not know how to check that"})

    failed = [check for check in checks if not check["ok"]]
    return {
        "ok": not failed,
        "checks": checks,
        "reason": "" if not failed else "; ".join(
            f"{check['check']}: {check['detail']}" for check in failed),
    }


def is_destructive(goal: str) -> bool:
    text = f" {str(goal or '').lower()} "
    return any(f" {verb} " in text or text.startswith(f" {verb} ")
               for verb in DESTRUCTIVE_VERBS)


# ── The agent ───────────────────────────────────────────────────────────────

class GuiAgent:
    def __init__(self, eyes: Optional[Callable[..., "ui_probe.Observation"]] = None,
                 hands: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
                 settle: float = SETTLE_SECONDS,
                 sleeper: Callable[[float], None] = time.sleep):
        self.eyes = eyes or self._observe
        self.hands = hands or self._act
        self.settle = max(0.0, float(settle))
        self.sleeper = sleeper

    # -- default senses and hands ---------------------------------------

    @staticmethod
    def _observe(force: bool = False, **_kwargs) -> "ui_probe.Observation":
        # Verification is a comparison, so it needs a look that happened *after*
        # the action. A cached observation would answer with the pre-click tree.
        return ui_probe.observe(use_cache=not force)

    @staticmethod
    def _act(action: str, params: Dict[str, Any]) -> Any:
        try:
            from actions.computer_control import computer_control

            payload = {**params, "action": action}
            return computer_control(payload)
        except Exception as exc:
            return {"ok": False, "error": "HANDS_FAILED", "detail": str(exc)}

    # -- one run ---------------------------------------------------------

    def run(self, goal: str, steps: List[Any], *, task_id: str = "",
            parent_id: str = "", confirmed: bool = False,
            max_attempts: int = MAX_ATTEMPTS, seconds: Optional[float] = None,
            screenshot_on_failure: bool = True) -> Report:
        """Carry out a short list of actions, verifying each one."""
        text = str(goal or "").strip()
        if not text:
            return Report(detail="a GUI task needs a goal")

        if is_destructive(text) and not confirmed:
            # Nothing moves until the owner says yes. The task is parked rather
            # than dropped, so the answer has somewhere to land.
            parked = self._park(text, parent_id, seconds)
            return Report(
                goal=text, task_id=parked, needs_confirm=True,
                detail=(f"'{text}' can't be undone, so I won't start it without a yes. "
                        "Confirm and I'll go ahead."))

        decision = self._admit()
        if decision is not None and not decision.allowed:
            return Report(goal=text, blocked=decision.spoken or decision.reason,
                          detail="refused by the trust gate")

        actions = [Action.from_dict(step) if isinstance(step, dict) else step
                   for step in (steps or [])]
        if not actions:
            return Report(goal=text, detail="there was nothing to do")

        report = Report(goal=text, task_id=task_id)
        report.task_id, task = self._open(text, task_id, parent_id, seconds, report)
        if task is None:
            return report

        for action in actions:
            reason = task_graph.over_budget(task.id)
            if reason:
                task_graph.block(task.id, reason)
                report.detail = reason
                return report
            report.attempts.extend(self._perform(task, action, max_attempts,
                                                 screenshot_on_failure))
            if not report.attempts[-1].ok:
                task_graph.fail(task.id, report.attempts[-1].detail)
                report.detail = report.attempts[-1].detail
                return report
            report.steps_done += 1

        task_graph.complete(task.id, {
            "kind": "gui",
            "detail": f"{report.steps_done} step(s) verified on screen",
            "data": {"attempts": len(report.attempts)},
        })
        report.ok = True
        report.detail = f"{report.steps_done} step(s) done and verified"
        return report

    # -- setup -----------------------------------------------------------

    def _park(self, goal: str, parent_id: str, seconds: Optional[float]) -> str:
        """Create the task and leave it waiting on a question."""
        try:
            task = task_graph.create(goal, parent_id=parent_id, seconds=seconds,
                                     meta={"kind": "gui"})
            task_graph.wait(task.id, f"Shall I go ahead with: {goal}?")
            return task.id
        except Exception:
            return ""

    def _admit(self):
        try:
            from core.trust_gate import evaluate

            return evaluate(GATE_CAPABILITY)
        except Exception:
            return None

    def _open(self, goal: str, task_id: str, parent_id: str,
              seconds: Optional[float], report: Report):
        if task_id:
            existing = task_graph.find(task_id)
            if existing is not None:
                # A parked question or a crash-resumed task: both continue here.
                if existing.state in (task_graph.WAITING, task_graph.INTERRUPTED):
                    try:
                        task_graph.resume(existing.id)
                    except Exception:
                        pass
                return existing.id, existing
        try:
            task = task_graph.create(goal, parent_id=parent_id, seconds=seconds,
                                     meta={"kind": "gui"})
            task_graph.start(task.id)
        except Exception as exc:
            report.detail = f"could not open a task: {exc}"
            return "", None
        return task.id, task

    # -- one action, up to max_attempts -----------------------------------

    def _perform(self, task, action: Action, max_attempts: int,
                 screenshot_on_failure: bool) -> List[Attempt]:
        attempts: List[Attempt] = []
        for index in range(1, max(1, int(max_attempts)) + 1):
            attempt = self._attempt(task, action, index)
            attempts.append(attempt)
            if attempt.ok:
                return attempts
            if attempt.refused:
                # The hands said no (no focus, no permission). Asking again would
                # produce the same no, and a loop that hammers a refusal is how a
                # stuck agent looks busy while doing nothing.
                break
        if screenshot_on_failure and attempts:
            attempts[-1].screenshot = self._screenshot()
        return attempts

    def _attempt(self, task, action: Action, index: int) -> Attempt:
        before = self.eyes()
        attempt = Attempt(index=index, target=action.target,
                          before=before.as_dict())

        match: Optional[Dict[str, Any]] = None
        if action.target:
            match, observation = self._locate(action.target, before)
            before = observation
            attempt.before = before.as_dict()
            attempt.found = match
            if match is None:
                attempt.verdict = {"ok": False, "checks": [],
                                   "reason": f"I can't find '{action.target}' on screen"}
                attempt.detail = attempt.verdict["reason"]
                self._record(task, attempt)
                return attempt

        params = dict(action.params)
        if match is not None and action.act in ("click", "double_click", "right_click"):
            x, y = match["center"]
            params.setdefault("x", x)
            params.setdefault("y", y)

        sent = self.hands(action.act, params)
        if isinstance(sent, dict) and sent.get("ok") is False:
            attempt.refused = True
            attempt.verdict = {"ok": False, "checks": [],
                               "reason": str(sent.get("detail") or sent.get("error")
                                             or "the action was refused")}
            attempt.detail = attempt.verdict["reason"]
            self._record(task, attempt)
            return attempt

        if self.settle:
            self.sleeper(self.settle)

        after = self.eyes(force=True)
        attempt.after = after.as_dict()
        attempt.verdict = verify(action.expect, before, after)
        attempt.detail = attempt.verdict["reason"] or f"{action.label}: verified"
        self._record(task, attempt)

        if attempt.ok:
            task_graph.add_step(task.id, action.label)
            task_graph.add_evidence(task.id, "gui", attempt.detail, {
                "target": action.target,
                "act": action.act,
                "rung": after.rung,
                "before": before.digest(),
                "after": after.digest(),
                "checks": attempt.verdict.get("checks"),
            })
        return attempt

    def _locate(self, target: str, first: "ui_probe.Observation"):
        """Find the target, looking again if the screen needed a moment."""
        observation = first
        match = ui_probe.find(target, observation)
        for _ in range(max(0, MAX_LOOKS - 1)):
            if match is not None:
                break
            if self.settle:
                self.sleeper(self.settle)
            observation = self.eyes(force=True)
            match = ui_probe.find(target, observation)
        return match, observation

    def _record(self, task, attempt: Attempt) -> None:
        try:
            task_graph.add_evidence(task.id, "gui-attempt", attempt.detail, {
                "index": attempt.index,
                "target": attempt.target,
                "found": bool(attempt.found),
                "before": attempt.before.get("digest"),
                "after": attempt.after.get("digest"),
                "checks": attempt.verdict.get("checks"),
            })
        except Exception:
            pass

    @staticmethod
    def _screenshot() -> str:
        try:
            from core.window_context import capture_active_window_screenshot

            path = capture_active_window_screenshot()
            return str(path or "")
        except Exception:
            return ""


# ── Module-level entry point ────────────────────────────────────────────────

_default_agent: Optional[GuiAgent] = None


def get_gui_agent() -> GuiAgent:
    global _default_agent
    if _default_agent is None:
        _default_agent = GuiAgent()
    return _default_agent


def look() -> str:
    """What is on screen right now, in words."""
    return ui_probe.summarise()


def run_gui_task(goal: str, steps: List[Any], **kwargs: Any) -> Dict[str, Any]:
    """Run a short verified task. Never raises."""
    try:
        return get_gui_agent().run(goal, steps, **kwargs).as_dict()
    except Exception as exc:
        return {"ok": False, "goal": goal, "detail": f"gui agent failed: {exc}",
                "attempts": [], "needs_confirm": False, "blocked": "",
                "steps_done": 0, "task_id": ""}
