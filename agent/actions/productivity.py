"""
agent/actions/productivity.py
=============================
FlowState workspace snapshots, the OmniPlanner composite-goal engine, and
StudyGuard focus sessions. All three are long-running, so each handler paints
its task workspace, hands the real work to a named daemon thread, and returns
immediately.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


# =============================================================================
# FLOWSTATE - workspace snapshots / restoration points
# =============================================================================

@register("flowstate")
def handle_flowstate(ctx: ActionContext) -> bool:
    action = str(ctx.action or "snapshot").lower()
    name = str(ctx.param("name") or "Main Workspace")
    ctx.speak(ctx.ack(f"Processing FlowState workspace '{name}'..."))

    def _run() -> None:
        try:
            from core.flowstate import get_flowstate_manager

            manager = get_flowstate_manager()

            if action in ("snapshot", "save", "create"):
                ctx.ui_call(
                    "begin_task_workspace",
                    f"FlowState: Snapshot '{name}'",
                    [
                        "Enumerate running apps",
                        "Extract Chrome & Edge tabs",
                        "Record VS Code workspaces",
                        "Save to persistent storage",
                    ],
                    source=ctx.source or "local",
                )
                result = manager.snapshot_workspace(name)
                apps_count = result.get("apps_count", 0)
                tabs_count = result.get("chrome_tabs_count", 0) + result.get(
                    "edge_tabs_count", 0
                )
                workspaces = result.get("vscode_workspaces", [])
                summary = (
                    f"Saved '{name}'\n"
                    f"• {apps_count} desktop applications\n"
                    f"• {tabs_count} active browser tabs\n"
                    f"• {len(workspaces)} code projects\n\n"
                    "Survives reboots permanently."
                )
                ctx.ui_call(
                    "update_task_workspace",
                    status="Workspace Snapshotted",
                    output=summary,
                    percent=100,
                )
                ctx.ui_call(
                    "show_hud_deliverable",
                    title=f"FlowState: {name}",
                    summary=f"Snapshot created at {result.get('created_str')}",
                    bullets=[
                        f"Apps: {apps_count} running",
                        f"Browser Tabs: {tabs_count} active",
                        f"Projects: {len(workspaces)} folders",
                        "Status: Saved to persistent disk",
                    ],
                    kind="flowstate",
                )
                ctx.speak(
                    f"Workspace '{name}' has been snapshotted with {apps_count} apps "
                    f"and {tabs_count} browser tabs, sir. You can reboot anytime."
                )

            elif action in ("restore", "load", "switch"):
                ctx.ui_call(
                    "begin_task_workspace",
                    f"FlowState: Restore '{name}'",
                    [
                        "Load snapshot from disk",
                        "Relaunch Chrome & Edge tabs",
                        "Relaunch VS Code projects",
                        "Relaunch desktop applications",
                    ],
                    source=ctx.source or "local",
                )
                result = manager.restore_workspace(name)
                if result.get("success"):
                    items = result.get("restored_items", [])
                    summary = f"Restored '{name}'\n" + "\n".join(
                        f"• {item}" for item in items
                    )
                    ctx.ui_call(
                        "update_task_workspace",
                        status="Workspace Restored",
                        output=summary,
                        percent=100,
                    )
                    ctx.speak(
                        f"Restored workspace '{name}' with {len(items)} applications "
                        "and tabs, sir."
                    )
                else:
                    error = result.get("error", "Workspace not found.")
                    ctx.ui_call(
                        "update_task_workspace",
                        status="Restore Failed",
                        output=error,
                        percent=0,
                    )
                    ctx.speak(f"Could not restore workspace '{name}': {error}")

            elif action in ("list", "show"):
                workspaces = manager.list_workspaces()
                if workspaces:
                    names = [ws["name"] for ws in workspaces]
                    summary = "Saved FlowState Restoration Points:\n" + "\n".join(
                        f"• {ws['name']} ({ws['apps_count']} apps, "
                        f"{ws['tabs_count']} tabs, created {ws['created_str']})"
                        for ws in workspaces
                    )
                    ctx.ui_call(
                        "update_task_workspace",
                        status="Saved Workspaces",
                        output=summary,
                        percent=100,
                    )
                    ctx.speak(
                        f"You have {len(workspaces)} saved workspaces: "
                        f"{', '.join(names[:3])}, sir."
                    )
                else:
                    ctx.ui_call(
                        "update_task_workspace",
                        status="No Workspaces",
                        output="No saved FlowState restoration points found.",
                        percent=100,
                    )
                    ctx.speak(
                        "You don't have any saved workspace restoration points yet, sir."
                    )

        except Exception as exc:
            ctx.log(f"ERR: FlowState failed: {exc}")
            ctx.ui_call(
                "update_task_workspace", status="FlowState Error", output=str(exc), percent=0
            )
            ctx.speak(f"FlowState encountered an error: {exc}")

    ctx.spawn(_run, name="flowstate")
    return True


# =============================================================================
# OMNI PLANNER - multi-step composite goals
# =============================================================================

@register("omni_planner")
def handle_omni_planner(ctx: ActionContext) -> bool:
    goal = str(ctx.param("goal") or ctx.text)
    ctx.speak(ctx.ack("Executing multi-step system plan..."))

    def _run() -> None:
        try:
            from core.omni_planner import get_omni_planner

            planner = get_omni_planner()
            steps = planner.decompose_goal(goal)
            step_descriptions = [
                f"{step.subsystem.capitalize()}: {step.description}" for step in steps
            ]
            ctx.ui_call(
                "begin_task_workspace",
                f"OmniPlanner: {goal[:35]}",
                step_descriptions,
                source=ctx.source or "local",
            )

            report = planner.execute_plan(goal, steps=steps)
            ctx.ui_call(
                "update_task_workspace",
                status=(
                    f"Completed {report.successful_steps}/{report.total_steps} Steps"
                ),
                output=report.executive_briefing,
                percent=100,
            )
            ctx.speak(report.executive_briefing)

        except Exception as exc:
            ctx.log(f"ERR: OmniPlanner failed: {exc}")
            ctx.speak(f"OmniPlanner encountered an issue: {exc}")

    ctx.spawn(_run, name="omni-planner")
    return True


# =============================================================================
# STUDY GUARD - focus sessions
# =============================================================================

@register("study_guard")
def handle_study_guard(ctx: ActionContext) -> bool:
    action = str(ctx.action or "start").lower()
    duration = int(ctx.param("duration", 25) or 25)
    subject = str(ctx.param("subject", "Focus Sprint"))
    lofi = bool(ctx.param("lofi", False))
    timer_only = bool(ctx.param("timer_only", False))

    if action not in ("start", "begin", "launch"):
        ctx.speak(ctx.ack("Processing StudyGuard..."))

    def _run() -> None:
        try:
            from core.study_guard import get_study_guard_manager

            manager = get_study_guard_manager()

            if action in ("start", "begin", "launch"):
                def _hud_update(status_text: str, pct: int) -> None:
                    ctx.ui_call("update_task_workspace", status=status_text, percent=pct)

                result = manager.start_session(
                    duration_minutes=duration,
                    subject=subject,
                    play_lofi=lofi,
                    timer_only=timer_only,
                    speak_cb=ctx.speak,
                    hud_update_cb=_hud_update,
                )
                if result.get("success"):
                    mode = (
                        "Pomodoro Focus Timer" if timer_only else "MediaPipe Face Sentinel"
                    )
                    ctx.ui_call(
                        "begin_task_workspace",
                        f"StudyGuard: {subject} ({duration}m)",
                        [
                            f"Mode: {mode}",
                            "Track Eye Aspect Ratio & posture"
                            if not timer_only
                            else "Pomodoro countdown active",
                            "Monitor distractions & flow state"
                            if not timer_only
                            else "Silent focus interval",
                            "Log sprint analytics to Circuit HUD",
                        ],
                        source=ctx.source or "local",
                    )
                    ctx.speak(
                        result.get(
                            "message", f"Study session started for {duration} minutes."
                        )
                    )
                else:
                    error = result.get("message", "Study session could not start.")
                    ctx.ui_call(
                        "update_task_workspace",
                        status="Startup Blocked",
                        output=error,
                        percent=0,
                    )
                    ctx.speak(error)

            elif action in ("stop", "end", "finish"):
                result = manager.stop_session()
                if result.get("success"):
                    stats = result.get("stats", {})
                    card_path = result.get("card_path")
                    summary = result.get("summary", "")
                    ctx.ui_call(
                        "update_task_workspace",
                        status="Session Finished",
                        output=summary,
                        percent=100,
                    )
                    ctx.ui_call(
                        "show_hud_deliverable",
                        title=f"Study: {stats.get('subject', 'Sprint')}",
                        summary=(
                            f"Focus Efficiency: {stats.get('focus_score')}%\n"
                            f"Total Time: {int(stats.get('total_seconds', 0) // 60)}m"
                        ),
                        bullets=[
                            f"Focus Score: {stats.get('focus_score')}%",
                            f"Focus Time: {int(stats.get('active_focus_seconds', 0) // 60)} minutes",
                            f"Sleep Alerts: {stats.get('sleep_alerts', 0)}",
                            f"Phone Distractions: {stats.get('phone_alerts', 0)}",
                        ],
                        file_path=card_path,
                        kind="study_guard",
                    )
                    ctx.speak(
                        "Study session finished, sir! Your focus score was "
                        f"{stats.get('focus_score')} percent."
                    )
                else:
                    ctx.speak(result.get("message", "No active study session to stop."))

            elif action in ("pause", "hold"):
                manager.pause_session()

            elif action in ("resume", "continue"):
                manager.resume_session()

            elif action in ("status", "stats"):
                status = manager.get_status()
                if status.get("is_active"):
                    stats = status.get("stats", {})
                    ctx.speak(
                        f"Session '{stats.get('subject')}' is active with a focus "
                        f"score of {stats.get('focus_score')} percent, sir."
                    )
                else:
                    ctx.speak("No study session is currently running, sir.")

        except Exception as exc:
            ctx.log(f"ERR: StudyGuard failed: {exc}")
            ctx.speak(f"StudyGuard encountered an issue: {exc}")

    ctx.spawn(_run, name="study-guard")
    return True


# =============================================================================
# GOOGLE WORKSPACE - reading an inbox and a schedule
# =============================================================================
#
# Two reads, nothing else. There is deliberately no handler for send, reply,
# create or cancel: a read that was misheard costs a wasted minute, a write
# that was misheard cannot be taken back. Those still reach the model, which
# asks before it acts.

_GW_READS = {
    "check_email": {
        "ack": "Checking your Gmail inbox, sir...",
        "steps": [
            "Connecting to Gmail",
            "Fetching unread messages",
            "Parsing headers & body",
            "Summarizing inbox",
        ],
        "call": {"service": "gmail", "action": "unread"},
        "status": "Email Check Completed",
        "failed": "Email Check Failed",
        "said": "Here are your latest emails, sir.",
        "unavailable": (
            "Unable to check emails. Please ensure your credentials are set up "
            "in Settings."
        ),
        "thread": "gmail-check",
    },
    "check_calendar": {
        "ack": "Checking your calendar schedule...",
        "steps": [
            "Connecting to Calendar",
            "Retrieving scheduled events",
            "Formatting timeline",
        ],
        "call": {"service": "calendar", "action": "list"},
        "status": "Schedule Retrieved",
        "failed": "Schedule Check Failed",
        "said": "Here is your schedule, sir.",
        "unavailable": "There was an issue checking your schedule, sir.",
        "thread": "calendar-check",
    },
}


@register("google_workspace")
def handle_google_workspace(ctx: ActionContext) -> bool:
    read = _GW_READS.get(str(ctx.action or "check_email").lower())
    if read is None:
        # A write, or a verb this handler cannot serve. Decline the turn rather
        # than answer it wrongly.
        return False

    ctx.speak(ctx.ack(read["ack"]))
    ctx.ui_call(
        "begin_task_workspace", ctx.text, list(read["steps"]), source=ctx.source or "local"
    )

    def _run() -> None:
        try:
            from actions.google_workspace_mcp import google_workspace

            # No ``speak``: the acknowledgement has been spoken already, and the
            # action module would say a second version of the same sentence.
            result = google_workspace(dict(read["call"]))
            ctx.log(f"[GoogleWorkspace] {result}")
            ctx.ui_call(
                "update_task_workspace",
                status=read["status"],
                output=result,
                percent=100,
            )
            ctx.speak(read["said"])
        except Exception as exc:
            ctx.log(f"ERR: Google Workspace read failed: {exc}")
            ctx.ui_call(
                "update_task_workspace",
                status=read["failed"],
                output=str(exc),
                percent=0,
            )
            ctx.speak(read["unavailable"])

    ctx.spawn(_run, name=read["thread"])
    return True
