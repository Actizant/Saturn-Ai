"""
core/omni_planner.py
====================
Saturn - Autonomous Multi-Step Goal Planner & DAG Orchestrator.

Enables the AI to break down composite, high-level user commands into
coordinated sub-actions across all system domains:
- Hardware & Memory Optimization (MemReductEngine, GameSentinel)
- Network Omniscience (NetSentinel, Router inspection, Speed tests)
- FlowState & Workspace Snapshotting (FlowStateManager)
- StudyGuard & Focus Supervision (StudyGuardEngine)
- System Hardware Controls (Volume, Power, Alerts)

Produces an executive, consolidated status briefing with zero robotic clutter.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("OmniPlanner")
logger.setLevel(logging.INFO)


@dataclass
class PlanStep:
    step_id: int
    subsystem: str  # "mem_reduct", "net_sentinel", "game_sentinel", "flowstate", "study_guard", "system"
    action: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[int] = field(default_factory=list)


@dataclass
class StepResult:
    step_id: int
    subsystem: str
    action: str
    success: bool
    data: Dict[str, Any]
    error: Optional[str] = None
    elapsed_ms: float = 0.0


@dataclass
class PlanExecutionReport:
    goal: str
    total_steps: int
    successful_steps: int
    failed_steps: int
    step_results: List[StepResult]
    total_elapsed_ms: float
    executive_briefing: str


class OmniPlanner:
    """Decomposes and executes multi-step system workflows."""

    _instance: Optional[OmniPlanner] = None

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> OmniPlanner:
        if cls._instance is None:
            cls._instance = OmniPlanner()
        return cls._instance

    # =========================================================================
    # COMPOSITE GOAL DETECTION
    # =========================================================================

    @staticmethod
    def is_composite_goal(text: str) -> bool:
        """Determines if the text contains multiple linked intents or composite goals."""
        low = text.lower().strip()

        # If user explicitly asks for reverse engineering, coding, browsing, or dev work, do NOT hijack as omni_planner
        if any(kw in low for kw in ("reverse engineer", "decompile", "remake", "clone", "replicate", "dev_agent", "build website", "create app")):
            return False

        # High-level macro phrases
        macro_phrases = [
            "prepare my system", "prepare my pc", "prepare my setup",
            "full system optimization", "optimize everything", "prep for gaming",
            "gaming setup", "study mode", "deep work mode", "prep for work",
            "clean up and save", "audit network and speed", "turbo boost everything"
        ]
        has_macro = any(m in low for m in macro_phrases)

        # Conjunction patterns linking multiple imperatives
        conjunctions = [
            " and ", " then ", " also ", " as well as ", " plus ", " followed by ",
            " after that ", " once done "
        ]
        has_conjunction = any(c in low for c in conjunctions)

        if not (has_macro or (has_conjunction and len(low.split()) >= 5)):
            return False

        # Verify that it actually decomposes into at least 1 real action step (or >= 2 for conjunctions)
        try:
            planner = OmniPlanner.get_instance()
            steps = planner.decompose_goal(text)
            if has_macro:
                return len(steps) >= 1
            return len(steps) >= 2
        except Exception:
            return False

    # =========================================================================
    # GOAL DECOMPOSITION
    # =========================================================================

    def decompose_goal(self, goal: str) -> List[PlanStep]:
        """Parses a composite user goal into an ordered DAG of executable PlanSteps."""
        low = goal.lower().strip()
        steps: List[PlanStep] = []
        step_idx = 1

        # 1. Memory / RAM Optimization
        if any(kw in low for kw in ("clean ram", "purge memory", "free memory", "trim ram", "clean memory", "optimize ram", "optimize memory", "free ram")):
            steps.append(PlanStep(
                step_id=step_idx,
                subsystem="mem_reduct",
                action="clean_ram",
                description="Purge standby memory list and process working sets",
                parameters={"purge_standby": True, "empty_working_sets": True}
            ))
            step_idx += 1

        # 2. Game Mode / Power Scheme
        if any(kw in low for kw in ("game mode", "gaming", "for gaming", "prep for gaming", "gaming setup", "turbo boost", "hardware boost")):
            steps.append(PlanStep(
                step_id=step_idx,
                subsystem="game_sentinel",
                action="activate_game_mode",
                description="Switch power plan to Ultimate Performance and prioritize CPU",
                parameters={"game_name": "Turbo Mode", "manual": True}
            ))
            step_idx += 1

        # 3. FlowState Workspace Snapshot
        if any(kw in low for kw in ("save workspace", "snapshot workspace", "save tabs", "save my session", "snapshot my setup", "clean up and save")):
            steps.append(PlanStep(
                step_id=step_idx,
                subsystem="flowstate",
                action="snapshot_workspace",
                description="Capture open Chrome/Edge tabs and active VS Code workspaces",
                parameters={"name": "Auto Snapshot"}
            ))
            step_idx += 1

        # 4. StudyGuard / Focus Session
        if any(kw in low for kw in ("study mode", "focus mode", "deep work mode", "start studying")):
            m_dur = re.search(r"(\d+)\s*(?:min|minute)", low)
            duration = int(m_dur.group(1)) if m_dur else 25
            steps.append(PlanStep(
                step_id=step_idx,
                subsystem="study_guard",
                action="start_session",
                description=f"Engage StudyGuard focus session for {duration} minutes",
                parameters={"duration_minutes": duration}
            ))
            step_idx += 1

        # 5. Network / Internet Health
        if any(kw in low for kw in ("check network", "internet health", "ping", "test internet", "speed test", "check connection")):
            steps.append(PlanStep(
                step_id=step_idx,
                subsystem="net_sentinel",
                action="check_internet",
                description="Benchmark DNS latency, public uplink, and ISP telemetry",
                parameters={}
            ))
            step_idx += 1

        # 6. Router Audit
        if any(kw in low for kw in ("inspect router", "check router", "audit router", "router status")):
            steps.append(PlanStep(
                step_id=step_idx,
                subsystem="net_sentinel",
                action="inspect_router",
                description="Probe gateway router web management console and ports",
                parameters={}
            ))
            step_idx += 1

        # 7. Space / Disk Cleaning
        if any(kw in low for kw in ("clean disk", "free disk space", "clean storage", "purge temp files", "clean junk")):
            steps.append(PlanStep(
                step_id=step_idx,
                subsystem="space_cleaner",
                action="purge_safe",
                description="Purge disposable system temp files, browser caches, and error logs",
                parameters={"categories": ["system_temp", "browser_cache", "crash_logs"]}
            ))
            step_idx += 1

        # Return parsed steps (empty if no system macro patterns matched)
        return steps

    # =========================================================================
    # STEP EXECUTOR
    # =========================================================================

    def _execute_step(self, step: PlanStep) -> StepResult:
        """Executes a single step against its subsystem with isolation and timing."""
        t0 = time.perf_counter()
        sub = step.subsystem
        act = step.action

        try:
            if sub == "mem_reduct":
                from core.mem_reduct import MemReductEngine
                purge_standby = step.parameters.get("purge_standby", True)
                empty_ws = step.parameters.get("empty_working_sets", True)
                res = MemReductEngine.clean_ram(purge_standby=purge_standby, empty_working_sets=empty_ws)
                elapsed = (time.perf_counter() - t0) * 1000
                return StepResult(step.step_id, sub, act, True, res, elapsed_ms=elapsed)

            elif sub == "game_sentinel":
                from core.game_sentinel import GameSentinelManager
                gsm = GameSentinelManager.get_instance()
                game_name = step.parameters.get("game_name", "Macro Turbo")
                res = gsm.activate_game_mode(game_name=game_name, manual=True, speak=False)
                elapsed = (time.perf_counter() - t0) * 1000
                return StepResult(step.step_id, sub, act, True, res, elapsed_ms=elapsed)

            elif sub == "flowstate":
                from core.flowstate import FlowStateManager
                fsm = FlowStateManager()
                snap_name = step.parameters.get("name", "Omni Snapshot")
                res = fsm.snapshot_workspace(snap_name)
                elapsed = (time.perf_counter() - t0) * 1000
                return StepResult(step.step_id, sub, act, True, res, elapsed_ms=elapsed)

            elif sub == "study_guard":
                from core.study_guard import StudyGuardEngine
                sge = StudyGuardEngine.get_instance()
                dur = step.parameters.get("duration_minutes", 25)
                res = sge.start_session(duration_minutes=dur)
                elapsed = (time.perf_counter() - t0) * 1000
                return StepResult(step.step_id, sub, act, True, res, elapsed_ms=elapsed)

            elif sub == "net_sentinel":
                from core.net_sentinel import get_net_sentinel
                ns = get_net_sentinel()
                if act == "inspect_router":
                    res = ns.inspect_router()
                elif act == "check_internet":
                    res = ns.get_internet_telemetry()
                elif act == "scan_network":
                    res = ns.scan_local_network(deep=False)
                else:
                    res = ns.get_internet_telemetry()
                elapsed = (time.perf_counter() - t0) * 1000
                return StepResult(step.step_id, sub, act, True, res, elapsed_ms=elapsed)

            elif sub == "space_cleaner":
                from core.space_cleaner import get_space_cleaner
                sc = get_space_cleaner()
                cats = step.parameters.get("categories", ["system_temp", "browser_cache", "crash_logs"])
                res = sc.purge_selected(cats)
                elapsed = (time.perf_counter() - t0) * 1000
                return StepResult(step.step_id, sub, act, True, res, elapsed_ms=elapsed)

            else:
                elapsed = (time.perf_counter() - t0) * 1000
                return StepResult(step.step_id, sub, act, False, {}, error=f"Unknown subsystem: {sub}", elapsed_ms=elapsed)

        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(f"Error executing step {step.step_id} ({sub}:{act}): {exc}", exc_info=True)
            return StepResult(step.step_id, sub, act, False, {}, error=str(exc), elapsed_ms=elapsed)

    # =========================================================================
    # ORCHESTRATION & EXECUTIVE SYNTHESIS
    # =========================================================================

    def execute_plan(self, goal: str, steps: Optional[List[PlanStep]] = None) -> PlanExecutionReport:
        """Executes all plan steps sequentially and synthesizes a high-level briefing."""
        t_start = time.perf_counter()
        active_steps = steps or self.decompose_goal(goal)
        results: List[StepResult] = []

        logger.info(f"Executing Omni-Plan with {len(active_steps)} steps for goal: '{goal}'")
        for s in active_steps:
            res = self._execute_step(s)
            results.append(res)

        total_elapsed = (time.perf_counter() - t_start) * 1000
        success_cnt = sum(1 for r in results if r.success)
        fail_cnt = len(results) - success_cnt

        briefing = self._synthesize_briefing(goal, results)

        return PlanExecutionReport(
            goal=goal,
            total_steps=len(results),
            successful_steps=success_cnt,
            failed_steps=fail_cnt,
            step_results=results,
            total_elapsed_ms=round(total_elapsed, 1),
            executive_briefing=briefing
        )

    @staticmethod
    def _synthesize_briefing(goal: str, results: List[StepResult]) -> str:
        """Generates a natural, elegant executive verbal summary."""
        bullet_points: List[str] = []

        for r in results:
            if not r.success:
                bullet_points.append(f"• {r.subsystem.capitalize()} encountered an issue: {r.error or 'Failed'}")
                continue

            d = r.data
            if r.subsystem == "mem_reduct":
                freed = d.get("freed_mb", 0)
                bullet_points.append(f"• Memory Purged: Freed {freed} MB of standby RAM (Usage: {d.get('after_percent', 0)}%)")

            elif r.subsystem == "game_sentinel":
                bullet_points.append("• Turbo Hardware: Power scheme elevated to Ultimate Performance with maximum CPU priority")

            elif r.subsystem == "flowstate":
                app_cnt = len(d.get("apps", []))
                tab_cnt = len(d.get("chrome_tabs", [])) + len(d.get("edge_tabs", []))
                bullet_points.append(f"• Workspace Saved: Captured {app_cnt} running apps and {tab_cnt} browser tabs")

            elif r.subsystem == "study_guard":
                bullet_points.append(f"• StudyGuard: Focus lock engaged for {d.get('duration_minutes', 25)} minutes")

            elif r.subsystem == "net_sentinel":
                if r.action == "inspect_router":
                    bullet_points.append(f"• Router: {d.get('vendor', 'Gateway')} at {d.get('gateway_ip')} ({d.get('latency_ms', 1)}ms latency)")
                else:
                    isp = d.get("isp", "Online")
                    speed = d.get("download_speed_mbps", 0)
                    bullet_points.append(f"• Uplink: {isp} ({d.get('ping_latency_ms', 0)}ms ping, {speed} Mbps down)")

            elif r.subsystem == "space_cleaner":
                freed = d.get("freed_mb", 0)
                bullet_points.append(f"• Disk Space Freed: Purged {freed} MB of disposable temporary files")

        if not bullet_points:
            return "Multi-step plan completed successfully, sir."

        header = "Plan executed successfully, sir:\n"
        return header + "\n".join(bullet_points)


# Singleton accessor
def get_omni_planner() -> OmniPlanner:
    return OmniPlanner.get_instance()
