"""The four tools that let Saturn *choose* the capability layer.

The registry, the task graph and the GUI agent were built as libraries. They
could be called from Python but not by Saturn himself, because nothing in the
tool surface mentioned them - so "use the 3D printer" had no road to travel.
These four functions are that road, and they are deliberately thin: every
decision (may this run? which adapter? did it work?) stays in the module that
owns it.

The rule carried over from the layers underneath: **a result quotes what
proved it.** "Done" with nothing behind it is indistinguishable from nothing
having happened at all, and a model that can't tell the difference will say
the cheerful thing.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# Importing the bridge registers a source, so a phone paired through Saturn
# Connect appears in the same list as the lamp without being written to disk.
from core import connect_bridge  # noqa: F401  (registration)

MAX_LISTED = 40


# ── Small shared helpers ────────────────────────────────────────────────────

def _params(raw: Any) -> Dict[str, Any]:
    """The model may hand back a JSON string where an object was asked for."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _steps(raw: Any) -> List[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _short(value: Any, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "\u2026"


def _spoke(speak, message: str) -> None:
    """Say one line, if anyone is listening. A dead voice is not a failure."""
    if speak is None:
        return
    try:
        speak(message)
    except Exception:
        pass


# ── 1. What can be reached ──────────────────────────────────────────────────

def _live_note(device: Any) -> str:
    """The one thing worth saying about a device that reports its own state.

    A phone can be paired and still be in a pocket. Listing it without saying
    so is how a model ends up promising something it cannot do.
    """
    state = getattr(device, "state", None) or {}
    if state.get("online") is False:
        return " - offline"
    battery = state.get("battery")
    if isinstance(battery, int):
        return f" - {battery}% battery"
    return ""


def describe_devices(parameters: Any = None, player: Any = None,
                     speak: Any = None) -> str:
    """Every configured device, with the capability names it answers to."""
    try:
        from core import capability_registry as registry

        devices = [device for device in registry.load() if device.enabled]
    except Exception as exc:
        return f"I can't read the device list right now: {exc}"

    if not devices:
        try:
            families = ", ".join(sorted(getattr(registry, "KNOWN_FAMILIES", ())))
        except Exception:
            families = ""
        tail = f" The vocabulary I understand is: {families}." if families else ""
        return ("No devices are configured yet. A device is one entry in "
                "config/devices.json - it needs an id, an adapter and the "
                "capabilities it supports; I don't need a new Python file for "
                f"it.{tail}")

    lines = [f"I can reach {len(devices)} device"
             f"{'' if len(devices) == 1 else 's'}:"]
    for device in devices[:MAX_LISTED]:
        caps = ", ".join(device.capability_ids) or "nothing yet"
        kind = f"{device.kind}, " if device.kind else ""
        lines.append(f"- {device.name} [{device.id}] ({kind}{device.adapter}): "
                     f"{caps}{_live_note(device)}")
    if len(devices) > MAX_LISTED:
        lines.append(f"- and {len(devices) - MAX_LISTED} more")
    lines.append("Name the capability and the device, for example: switch.on on the desk lamp.")
    return "\n".join(lines)


# ── 2. One device, one thing ────────────────────────────────────────────────

def run_capability(parameters: Any = None, player: Any = None,
                   speak: Any = None) -> str:
    """Ask one device for one capability and report what the adapter said.

    The call is wrapped in a task on purpose: an action that leaves no record
    cannot be reviewed, undone, or told apart from a daydream.
    """
    args = _params(parameters)
    device = str(args.get("device") or args.get("target") or "").strip()
    capability = str(args.get("capability") or args.get("action") or "").strip()
    params = _params(args.get("params") or args.get("arguments"))

    if not device or not capability:
        return ("I need both a device and a capability - for example "
                "switch.on on the desk lamp. Ask me what I can reach if you're "
                "not sure what to call them.")

    try:
        from core import capability_registry as registry
        from core import task_graph
    except Exception as exc:
        return f"The device layer is unavailable: {exc}"

    task = None
    try:
        task = task_graph.create(f"{capability} on {device}", steps=1,
                                 meta={"kind": "capability", "device": device,
                                       "capability": capability})
        task_graph.start(task.id)
    except Exception:
        # A missing notebook must not stop the hands.
        task = None

    try:
        result = registry.execute(
            device, capability, params,
            confirmed=bool(args.get("confirmed")),
            passphrase=bool(args.get("passphrase")),
        )
    except Exception as exc:
        result = {"ok": False, "error": "TOOL_FAILED", "detail": str(exc)}
    if not isinstance(result, dict):
        result = {"ok": False, "error": "TOOL_CONTRACT", "detail": repr(result)}

    ok = bool(result.get("ok"))
    detail = _short(result.get("detail") or result.get("error") or "")
    error = str(result.get("error") or "")

    if error == "NO_SUCH_DEVICE":
        names = ", ".join(item.name for item in registry.load()[:8]) or "nothing yet"
        detail = f"{detail} I know: {names}."

    recorded = ""
    if task is not None:
        try:
            task_graph.add_evidence(task.id, "capability", detail or error, {
                "ok": ok,
                "device": str(result.get("device") or device),
                "capability": str(result.get("capability") or capability),
                "error": error,
                "params": params,
            })
            if ok:
                task_graph.complete(task.id)
            elif error == "REFUSED":
                # Refused by policy: not a malfunction, and not to be retried.
                task_graph.block(task.id, detail)
            else:
                task_graph.fail(task.id, detail or error)
            recorded = f" [task {task.id}]"
        except Exception:
            recorded = ""

    if ok:
        _spoke(speak, detail or f"{capability} done.")
        return f"{detail}{recorded}" if detail else f"{capability}: done.{recorded}"

    return f"{detail or 'That did not work.'}{recorded}"


# ── 3. Look, act, look again ────────────────────────────────────────────────

def _stopped_at(report: Dict[str, Any]) -> str:
    """Where the run gave up, in the run's own words."""
    attempts = report.get("attempts") or []
    for attempt in reversed(attempts):
        if attempt.get("ok"):
            continue
        detail = _short(attempt.get("detail") or "", 180)
        target = _short(attempt.get("target") or "", 60)
        if target and detail:
            return f"I got to '{target}' and it did not take: {detail}"
        if detail:
            return detail
    return _short(report.get("detail") or "it did not finish")


def run_goal(parameters: Any = None, player: Any = None,
             speak: Any = None) -> str:
    """Drive any app by looking at it: do a step, then check the screen."""
    args = _params(parameters)
    goal = _short(args.get("goal") or args.get("task") or "", 300)
    steps = _steps(args.get("steps") or args.get("actions"))

    if not goal:
        return "What should I do on screen? Give me the goal in a sentence."
    if not steps:
        return ("I need at least one step: what to press or type, and what should "
                "be true afterwards. For example {\"target\": \"Save\", "
                "\"action\": \"click\", \"expect\": {\"text_contains\": \"Saved\"}}.")

    seconds = args.get("seconds")
    try:
        seconds = float(seconds) if seconds not in (None, "") else None
    except (TypeError, ValueError):
        seconds = None

    try:
        from core import gui_agent
    except Exception as exc:
        return f"The screen agent is unavailable: {exc}"

    try:
        report = gui_agent.run_gui_task(
            goal, steps,
            confirmed=bool(args.get("confirmed")),
            parent_id=str(args.get("parent_task") or ""),
            seconds=seconds,
        )
    except Exception as exc:
        return f"That failed before it started: {exc}"

    if not isinstance(report, dict):
        return f"The screen agent answered in a shape I don't understand: {report!r}"

    task_id = str(report.get("task_id") or "")
    stamp = f" [task {task_id}]" if task_id else ""

    if report.get("ok"):
        done = _short(report.get("detail") or "verified on screen")
        _spoke(speak, done)
        return f"{goal}: {done}.{stamp}"

    if report.get("needs_confirm"):
        return f"{_short(report.get('detail'))} It is parked{stamp} until you say yes."

    if report.get("blocked"):
        return f"I didn't start it: {_short(report.get('blocked'))}"

    return f"{goal}: {_stopped_at(report)}{stamp}"


# ── 4. What has been asked, and what is still open ──────────────────────────

def _outline(task, depth: int = 0, lines: Optional[List[str]] = None) -> List[str]:
    """The tree, with the ids a model needs in order to point at anything."""
    lines = [] if lines is None else lines
    mark = {"done": "x", "failed": "!", "cancelled": "-", "blocked": "#",
            "waiting": "?", "running": ">"}.get(task.state, ".")
    lines.append(f"{'  ' * depth}[{mark}] {task.goal} [{task.id}] ({task.state})")
    try:
        from core import task_graph

        for child in task_graph.children(task.id):
            _outline(child, depth + 1, lines)
    except Exception:
        pass
    return lines


def task_tree(parameters: Any = None, player: Any = None,
              speak: Any = None) -> str:
    """Show the task tree, or stop one branch of it."""
    args = _params(parameters)
    task_id = str(args.get("task_id") or args.get("id") or "").strip()

    try:
        from core import task_graph
    except Exception as exc:
        return f"The task list is unavailable: {exc}"

    if args.get("stop") or str(args.get("action") or "").lower() in ("stop", "cancel"):
        if not task_id:
            open_tasks = [task for task in task_graph.live() if not task.parent_id]
            if not open_tasks:
                return "There is nothing running to stop."
            lines = ["Which one? Say the task id:"] + [
                f"- {task.goal} [{task.id}]" for task in open_tasks[:MAX_LISTED]
            ]
            return "\n".join(lines)
        try:
            affected = task_graph.cancel(task_id, reason="stopped on request")
        except Exception as exc:
            return f"I couldn't stop that: {exc}"
        stopped = len(affected)
        _spoke(speak, "Stopped.")
        return (f"Stopped {stopped} task{'' if stopped == 1 else 's'}, "
                f"including anything underneath it.")

    if task_id:
        task = task_graph.find(task_id)
        if task is None:
            return f"I have no task called '{task_id}'."
        lines = _outline(task)
        lines.append(f"budget: {task.steps_used}/{task.max_steps} steps, "
                     f"{int(max(0, task.remaining_seconds))}s left")
        proof = task_graph.evidence(task_id)
        if proof:
            lines.append("evidence:")
            for index, item in enumerate(proof[-6:], start=max(1, len(proof) - 5)):
                lines.append(f"  {index}. [{item.get('kind')}] {_short(item.get('detail'), 140)}")
        elif not task.terminal:
            lines.append("evidence: none yet - it has not finished anything.")
        if task.error:
            lines.append(f"note: {_short(task.error, 160)}")
        return "\n".join(lines)

    counts = task_graph.summary()
    lines = [f"{counts.get('total', 0)} task(s): "
             f"{counts.get('live', 0)} still open, "
             f"{counts.get('evidence', 0)} piece(s) of evidence."]
    open_roots = [task for task in task_graph.live() if not task.parent_id]
    finished = [task for task in task_graph.recent(MAX_LISTED)
                if not task.parent_id and task.terminal]
    shown = (open_roots + finished)[:MAX_LISTED]
    if shown:
        # Open work first: that is the part someone can still change.
        for task in shown:
            _outline(task, 0, lines)
    else:
        lines.append("Nothing has been asked of me yet.")
    return "\n".join(lines)
