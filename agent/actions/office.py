"""
agent/actions/office.py
=======================
Document factories: PowerPoint, Excel, and the Saturn Dev coding agent. Each
speaks its acknowledgement on the turn thread (so the user hears something
immediately) and then does generation on a worker.
"""

from __future__ import annotations

from pathlib import Path

from agent.action_router import ActionContext, register


@register("presentation")
def handle_presentation(ctx: ActionContext) -> bool:
    topic = str(ctx.param("topic") or ctx.text)

    ctx.speak(ctx.ack("Designing your presentation slides..."))
    ctx.ui_call(
        "begin_task_workspace",
        ctx.text,
        [
            "Researching topic",
            "Outlining slides",
            "Applying typography & theme",
            "Generating PowerPoint deck",
        ],
        source=ctx.source or "local",
    )

    def _run() -> None:
        try:
            from actions.office_generator import generate_presentation_from_prompt

            result = generate_presentation_from_prompt(
                topic, player=ctx.app.ui, speak=ctx.speak
            )
            ctx.log(f"[SaturnOffice] {result}")
            ctx.ui_call(
                "update_task_workspace",
                status="Presentation Completed",
                output=result,
                percent=100,
            )
            ctx.speak("Your presentation has been created and saved to Desktop, sir.")
        except Exception as exc:
            ctx.log(f"ERR: Presentation generation failed: {exc}")
            ctx.ui_call(
                "update_task_workspace",
                status="Generation Failed",
                output=str(exc),
                percent=0,
            )
            ctx.speak(
                "There was an issue creating the presentation, sir. "
                "Please check the logs."
            )

    ctx.spawn(_run, name="presentation-gen")
    return True


@register("spreadsheet")
def handle_spreadsheet(ctx: ActionContext) -> bool:
    topic = str(ctx.param("topic") or ctx.text)

    ctx.speak(ctx.ack("Building your spreadsheet workbook..."))
    ctx.ui_call(
        "begin_task_workspace",
        ctx.text,
        [
            "Analyzing data structure",
            "Defining headers & formulas",
            "Formatting workbook",
            "Saving Excel spreadsheet",
        ],
        source=ctx.source or "local",
    )

    def _run() -> None:
        try:
            from actions.office_generator import generate_spreadsheet_from_prompt

            result = generate_spreadsheet_from_prompt(
                topic, player=ctx.app.ui, speak=ctx.speak
            )
            ctx.log(f"[SaturnOffice] {result}")
            ctx.ui_call(
                "update_task_workspace",
                status="Spreadsheet Completed",
                output=result,
                percent=100,
            )
            ctx.speak("Your spreadsheet workbook has been created and saved to Desktop, sir.")
        except Exception as exc:
            ctx.log(f"ERR: Spreadsheet generation failed: {exc}")
            ctx.ui_call(
                "update_task_workspace",
                status="Generation Failed",
                output=str(exc),
                percent=0,
            )
            ctx.speak(
                "There was an issue creating the spreadsheet, sir. Please check the logs."
            )

    ctx.spawn(_run, name="spreadsheet-gen")
    return True


@register("developer_agent")
def handle_developer_agent(ctx: ActionContext) -> bool:
    try:
        settings = (
            ctx.app.ui._load_app_settings()
            if hasattr(ctx.app.ui, "_load_app_settings")
            else {}
        )
        workspace = str(settings.get("developer_mode_workspace", "")).strip()
        if not workspace:
            workspace = str(Path.home() / "Desktop" / "SaturnProjects")
            Path(workspace).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        ctx.log(f"ERR: Developer workspace unavailable: {exc}")
        return False

    ctx.speak(ctx.ack("Working on your project with Saturn Dev..."))
    ctx.ui_call(
        "begin_task_workspace",
        ctx.text,
        ["Analyzing specifications", "Scaffolding files", "Writing code", "Verifying build"],
        source=ctx.source or "local",
    )

    def _run() -> None:
        try:
            import webbrowser

            from actions.saturn_dev_agent import run_dev_agent

            result = run_dev_agent(
                {"description": ctx.text, "workspace_path": workspace}, speak=ctx.speak
            )
            ctx.log(f"[SaturnDev] {result[:400]}")

            if "encountered an error during inference" in result or "LLM error:" in result:
                ctx.ui_call(
                    "update_task_workspace", status="Build Error", output=result, percent=0
                )
                ctx.speak(
                    "There was an error building the project, sir. Please check the logs."
                )
                return

            ctx.ui_call(
                "update_task_workspace",
                status="Project Completed",
                output=result,
                percent=100,
            )

            folder_display = workspace
            try:
                folder_display = Path(workspace).name or workspace
            except Exception:
                pass
            ctx.speak(f"Your project has been completed and saved to {folder_display}, sir.")

            # Auto-open index.html in the browser if one was produced.
            try:
                index_path = Path(workspace) / "index.html"
                if index_path.exists():
                    webbrowser.open(str(index_path.resolve()))
            except Exception:
                pass

        except Exception as exc:
            ctx.log(f"ERR: Saturn Dev failed: {exc}")
            ctx.ui_call(
                "update_task_workspace", status="Build Failed", output=str(exc), percent=0
            )
            ctx.speak("There was an issue building the project, sir. Please check the logs.")

    ctx.spawn(_run, name="saturn-dev")
    return True
