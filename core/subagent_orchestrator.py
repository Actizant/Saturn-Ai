"""
core/subagent_orchestrator.py
=============================
Saturn - Autonomous Subagent Swarms & Parallel Workspace Orchestrator.

Enables Saturn to delegate complex multi-step objectives to specialized child subagents:
1. ResearchSubagent: Surveys files, searches documentation, gathers requirements.
2. CoderSubagent: Writes, edits, and compiles implementations in a clean workspace.
3. TestSubagent: Runs test suites, verifies benchmarks, and reports errors for self-healing.

Runs child agents concurrently without polluting the primary conversation context window.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.user_paths import get_user_data_dir
from core.claude_tools import ClaudeCodeTools

logger = logging.getLogger("SubagentOrchestrator")
logger.setLevel(logging.INFO)


@dataclass
class SubagentTask:
    task_id: str
    role: str  # "researcher", "coder", "tester", "general"
    prompt: str
    workspace: Path
    status: str = "PENDING"  # "PENDING", "RUNNING", "COMPLETED", "FAILED"
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


class SubagentWorker:
    """Autonomous execution loop for a child subagent."""

    def __init__(self, task: SubagentTask, on_update: Optional[Callable[[str], None]] = None):
        self.task = task
        self.on_update = on_update

    def run(self) -> SubagentTask:
        self.task.status = "RUNNING"
        self.task.started_at = time.time()
        logger.info(f"[Subagent:{self.task.task_id}] Starting role={self.task.role}: '{self.task.prompt}'")
        if self.on_update:
            self.on_update(f"🤖 Subagent [{self.task.role.upper()}]: Working on '{self.task.prompt[:60]}...'")

        try:
            # Execute role-tailored pipeline
            if self.task.role == "researcher":
                res = self._execute_researcher()
            elif self.task.role == "tester":
                res = self._execute_tester()
            else:
                res = self._execute_coder()

            self.task.status = "COMPLETED"
            self.task.result = res
            self.task.completed_at = time.time()
            if self.on_update:
                self.on_update(f"✅ Subagent [{self.task.role.upper()}]: Completed successfully.")
        except Exception as e:
            self.task.status = "FAILED"
            self.task.error = str(e)
            self.task.completed_at = time.time()
            logger.error(f"[Subagent:{self.task.task_id}] Failed: {e}")
            if self.on_update:
                self.on_update(f"❌ Subagent [{self.task.role.upper()}]: Failed - {e}")

        return self.task

    def _execute_researcher(self) -> str:
        """Explores codebase and gathers context without modifying files."""
        # 1. Glob relevant files
        files_res = ClaudeCodeTools.glob("*", cwd=str(self.task.workspace), max_results=30)
        file_list = files_res.get("files", [])

        # 2. Grep keywords from prompt
        keywords = [w for w in self.task.prompt.split() if len(w) > 3][:3]
        grep_findings = []
        for kw in keywords:
            g = ClaudeCodeTools.grep(kw, cwd=str(self.task.workspace), max_results=10)
            if g.get("results"):
                grep_findings.extend(g["results"][:5])

        report = [
            f"=== RESEARCH REPORT ({self.task.task_id}) ===",
            f"Workspace: {self.task.workspace}",
            f"Key Files Located: {len(file_list)} files found.",
            ", ".join(file_list[:12]),
        ]
        if grep_findings:
            report.append("\nRelevant Code Snippets Found:")
            report.extend(grep_findings[:8])

        return "\n".join(report)

    def _execute_tester(self) -> str:
        """Runs test verification and failure stack analysis."""
        res = ClaudeCodeTools.test_runner("pytest", cwd=str(self.task.workspace))
        if res.get("success"):
            return "All tests passed successfully (0 failures)."
        else:
            return (
                f"Test Verification: {res.get('failures_detected', 1)} failure(s) detected.\n"
                f"Summary:\n" + "\n".join(res.get("failure_summary", [])) +
                f"\n\nFull Output:\n{res.get('full_output', '')[:3000]}"
            )

    def _execute_coder(self) -> str:
        """Executes terminal commands or builds code in the workspace."""
        cmd = self.task.prompt
        res = ClaudeCodeTools.bash(cmd, cwd=str(self.task.workspace))
        return res.get("output", "Execution completed.")


class SubagentOrchestrator:
    """Manages spawning, tracking, and synchronizing subagent swarms."""

    _instance: Optional[SubagentOrchestrator] = None
    _lock = threading.Lock()

    def __init__(self):
        self._tasks: Dict[str, SubagentTask] = {}
        self._threads: Dict[str, threading.Thread] = {}

    @classmethod
    def get_instance(cls) -> SubagentOrchestrator:
        with cls._lock:
            if cls._instance is None:
                cls._instance = SubagentOrchestrator()
            return cls._instance

    def spawn_subagent(
        self,
        role: str,
        prompt: str,
        workspace: Optional[str] = None,
        on_update: Optional[Callable[[str], None]] = None,
        wait: bool = False
    ) -> SubagentTask:
        """
        Spawns a new subagent worker.
        If wait=True, blocks until completion.
        If wait=False, runs in the background and returns task immediately.
        """
        task_id = f"subagent_{role}_{int(time.time() * 1000) % 100000}"
        ws = Path(workspace).resolve() if workspace else ClaudeCodeTools.get_workspace()

        task = SubagentTask(
            task_id=task_id,
            role=role.lower(),
            prompt=prompt,
            workspace=ws
        )
        self._tasks[task_id] = task

        worker = SubagentWorker(task, on_update=on_update)

        if wait:
            return worker.run()
        else:
            t = threading.Thread(target=worker.run, name=f"Subagent-{task_id}", daemon=True)
            self._threads[task_id] = t
            t.start()
            return task

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        return {
            "task_id": task.task_id,
            "role": task.role,
            "status": task.status,
            "result": task.result,
            "error": task.error,
            "elapsed": round(time.time() - task.started_at, 1)
        }

    def list_active_subagents(self) -> List[Dict[str, Any]]:
        return [
            {
                "task_id": t.task_id,
                "role": t.role,
                "status": t.status,
                "prompt": t.prompt[:50] + "..."
            }
            for t in self._tasks.values()
        ]
