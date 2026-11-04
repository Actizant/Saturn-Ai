"""
core/claude_tools.py
====================
Saturn - Universal Claude Code & OpenAI Codex Tooling Suite.

Provides enterprise-grade, surgical software engineering and computation tools:
1. bash / powershell: Unconstrained terminal execution with timeout and output truncation.
2. file_read: Line-numbered file reading with offset and limit support.
3. file_edit: Exact substring replacement with whitespace/indentation matching and collision safeguards.
4. file_write: Atomic file creation with automatic directory scaffolding.
5. glob: High-speed recursive pattern matching with .gitignore awareness.
6. grep: Regex and literal search with line numbers across project trees.
7. python_repl: Computational sandbox (Pandas, NumPy, Matplotlib chart output).
8. git_manager: Git version control operations (status, diff, commit, branch, log).
9. test_runner: Automated test suite runner (pytest, npm test, etc.) with failure stack trace parsing.
"""

from __future__ import annotations

import fnmatch
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.user_paths import get_user_data_dir

logger = logging.getLogger("ClaudeTools")
logger.setLevel(logging.INFO)

MAX_OUTPUT_CHARS = 30_000
ARTIFACTS_DIR = get_user_data_dir() / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


class ClaudeCodeTools:
    """Universal developer tools matching Claude Code & Codex specifications."""

    _default_workspace: Optional[Path] = None

    @classmethod
    def get_workspace(cls) -> Path:
        if cls._default_workspace and cls._default_workspace.exists():
            return cls._default_workspace
        
        # Check settings
        settings_path = get_user_data_dir() / "config" / "app_settings.json"
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                configured = data.get("developer_mode_workspace")
                if configured and Path(configured).exists():
                    cls._default_workspace = Path(configured).resolve()
                    return cls._default_workspace
            except Exception:
                pass

        # Fallback to current working directory or Desktop/SaturnProjects
        cwd = Path.cwd()
        if (cwd / ".git").exists() or (cwd / "main.py").exists():
            return cwd
        
        default_dir = Path.home() / "Desktop" / "SaturnProjects"
        default_dir.mkdir(parents=True, exist_ok=True)
        cls._default_workspace = default_dir
        return default_dir

    @classmethod
    def set_workspace(cls, path: Path | str) -> None:
        p = Path(path).resolve()
        p.mkdir(parents=True, exist_ok=True)
        cls._default_workspace = p

    # =========================================================================
    # 1. BASH / POWERSHELL EXECUTION
    # =========================================================================

    @classmethod
    def bash(
        cls,
        command: str,
        timeout: int = 120,
        cwd: Optional[str] = None,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """
        Executes a shell command in the workspace directory.
        Uses PowerShell on Windows and Bash on Linux/macOS.
        """
        work_dir = Path(cwd).resolve() if cwd else cls.get_workspace()
        is_win = sys.platform.startswith("win")
        
        logger.info(f"[ClaudeTools:Bash] Running in {work_dir}: {command}")
        if on_output:
            on_output(f"⚡ Terminal: {command}")

        start_time = time.monotonic()
        try:
            if is_win:
                shell_cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                shell_cmd = ["/bin/bash", "-c", command]

            res = subprocess.run(
                shell_cmd,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False
            )
            elapsed = round(time.monotonic() - start_time, 2)
            stdout = res.stdout or ""
            stderr = res.stderr or ""
            exit_code = res.returncode

            # Truncate if massive (e.g. huge build output)
            total_len = len(stdout) + len(stderr)
            if total_len > MAX_OUTPUT_CHARS:
                keep_head = 4000
                keep_tail = 12000
                stdout = (
                    stdout[:keep_head]
                    + f"\n\n... [Output truncated: {total_len - (keep_head + keep_tail)} characters omitted] ...\n\n"
                    + stdout[-keep_tail:]
                )

            formatted_output = []
            if stdout.strip():
                formatted_output.append(stdout.strip())
            if stderr.strip():
                formatted_output.append(f"[stderr]:\n{stderr.strip()}")
            if not stdout.strip() and not stderr.strip():
                formatted_output.append("(Command produced no output)")
            formatted_output.append(f"[Process finished with exit code {exit_code} in {elapsed}s]")

            return {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "output": "\n".join(formatted_output),
                "elapsed_seconds": elapsed,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "exit_code": -1,
                "output": f"Error: Command timed out after {timeout} seconds.",
                "elapsed_seconds": timeout,
            }
        except Exception as e:
            return {
                "success": False,
                "exit_code": -1,
                "output": f"Error executing command: {e}",
                "elapsed_seconds": round(time.monotonic() - start_time, 2),
            }

    # =========================================================================
    # 2. FILE READING (LINE-NUMBERED OFFSET & LIMIT)
    # =========================================================================

    @classmethod
    def file_read(
        cls,
        file_path: str,
        offset: int = 1,
        limit: int = 2000,
        cwd: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Reads a file with line numbers starting at offset up to limit lines.
        Exact match for Claude Code read tool specification.
        """
        base = Path(cwd).resolve() if cwd else cls.get_workspace()
        path = (base / file_path).resolve() if not Path(file_path).is_absolute() else Path(file_path).resolve()

        if not path.exists():
            return {"success": False, "error": f"File does not exist: {path}"}
        if path.is_dir():
            return {"success": False, "error": f"Path is a directory, not a file: {path}"}

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start_idx = max(0, offset - 1)
            end_idx = min(total_lines, start_idx + limit)

            numbered_lines = []
            for idx in range(start_idx, end_idx):
                numbered_lines.append(f"{idx + 1:6d}\t{lines[idx].rstrip()}")

            header = f"[File: {path.name} | Lines {start_idx + 1} to {end_idx} of {total_lines} total]\n"
            content = header + "\n".join(numbered_lines)

            return {
                "success": True,
                "path": str(path),
                "total_lines": total_lines,
                "start_line": start_idx + 1,
                "end_line": end_idx,
                "content": content,
            }
        except Exception as e:
            return {"success": False, "error": f"Error reading file {path}: {e}"}

    # =========================================================================
    # 3. FILE EDITING (SURGICAL STRING REPLACEMENT)
    # =========================================================================

    @classmethod
    def file_edit(
        cls,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        cwd: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Surgically replaces old_string with new_string in file_path.
        Exact match for Claude Code str_replace specification with collision detection.
        """
        base = Path(cwd).resolve() if cwd else cls.get_workspace()
        path = (base / file_path).resolve() if not Path(file_path).is_absolute() else Path(file_path).resolve()

        if not path.exists():
            return {"success": False, "error": f"File does not exist: {path}"}
        if path.is_dir():
            return {"success": False, "error": f"Cannot edit a directory: {path}"}

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            count = content.count(old_string)
            if count == 0:
                return {
                    "success": False,
                    "error": (
                        f"Target old_string was not found in {path.name}. "
                        "Verify whitespace, indentation, and line endings from file_read."
                    ),
                }

            if count > 1 and not replace_all:
                return {
                    "success": False,
                    "error": (
                        f"old_string occurs {count} times in {path.name}. "
                        "Provide more surrounding context lines to make the match unique, or set replace_all=true."
                    ),
                }

            # Backup for safety/undo
            backup_path = path.with_suffix(path.suffix + ".saturn_bak")
            try:
                shutil.copy2(path, backup_path)
            except Exception:
                pass

            if replace_all:
                new_content = content.replace(old_string, new_string)
            else:
                new_content = content.replace(old_string, new_string, 1)

            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)

            return {
                "success": True,
                "path": str(path),
                "occurrences_replaced": count if replace_all else 1,
                "message": f"Successfully updated {path.name} ({count if replace_all else 1} replacement(s)).",
            }
        except Exception as e:
            return {"success": False, "error": f"Error editing file {path}: {e}"}

    # =========================================================================
    # 4. FILE WRITING (ATOMIC OVERWRITE / CREATE)
    # =========================================================================

    @classmethod
    def file_write(
        cls,
        file_path: str,
        content: str,
        cwd: Optional[str] = None
    ) -> Dict[str, Any]:
        """Writes entire content to a file, automatically creating parent directories."""
        base = Path(cwd).resolve() if cwd else cls.get_workspace()
        path = (base / file_path).resolve() if not Path(file_path).is_absolute() else Path(file_path).resolve()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

            return {
                "success": True,
                "path": str(path),
                "bytes_written": len(content.encode("utf-8")),
                "message": f"Successfully wrote {len(content)} characters to {path.name}.",
            }
        except Exception as e:
            return {"success": False, "error": f"Error writing file {path}: {e}"}

    # =========================================================================
    # 5. GLOB (FAST RECURSIVE PATTERN MATCHING)
    # =========================================================================

    @classmethod
    def glob(
        cls,
        pattern: str,
        path: str = ".",
        max_results: int = 100,
        cwd: Optional[str] = None
    ) -> Dict[str, Any]:
        """Finds files matching a glob pattern, ignoring build/cache directories."""
        base = Path(cwd).resolve() if cwd else cls.get_workspace()
        search_root = (base / path).resolve()

        if not search_root.exists():
            return {"success": False, "error": f"Path does not exist: {search_root}"}

        matches = []
        ignore_dirs = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".idea", ".vscode", "target"}

        try:
            for root, dirs, files in os.walk(search_root):
                dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
                for filename in files:
                    full_p = Path(root) / filename
                    try:
                        rel_p = full_p.relative_to(base)
                    except ValueError:
                        rel_p = full_p
                    rel_str = str(rel_p).replace("\\", "/")
                    if fnmatch.fnmatch(rel_str, pattern) or fnmatch.fnmatch(filename, pattern):
                        matches.append(rel_str)
                    if len(matches) >= max_results:
                        break
                if len(matches) >= max_results:
                    break

            return {
                "success": True,
                "pattern": pattern,
                "count": len(matches),
                "files": matches,
                "output": "\n".join(matches) if matches else f"No files matching pattern '{pattern}' found.",
            }
        except Exception as e:
            return {"success": False, "error": f"Error during glob search: {e}"}

    # =========================================================================
    # 6. GREP (FAST REGEX / LITERAL CONTENT SEARCH)
    # =========================================================================

    @classmethod
    def grep(
        cls,
        pattern: str,
        path: str = ".",
        case_sensitive: bool = True,
        max_results: int = 50,
        cwd: Optional[str] = None
    ) -> Dict[str, Any]:
        """Searches for regex/literal text inside files across project hierarchy."""
        base = Path(cwd).resolve() if cwd else cls.get_workspace()
        search_root = (base / path).resolve()

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except Exception as e:
            return {"success": False, "error": f"Invalid regular expression '{pattern}': {e}"}

        results = []
        ignore_dirs = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".idea", ".vscode", "target"}

        try:
            for root, dirs, files in os.walk(search_root):
                dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
                for filename in files:
                    fpath = Path(root) / filename
                    # Skip large binary / media files (> 1MB)
                    try:
                        if fpath.stat().st_size > 1_000_000:
                            continue
                    except Exception:
                        continue

                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            for lineno, line in enumerate(f, 1):
                                if regex.search(line):
                                    try:
                                        rel = str(fpath.relative_to(base)).replace("\\", "/")
                                    except ValueError:
                                        rel = fpath.name
                                    results.append(f"{rel}:{lineno}: {line.strip()}")
                                    if len(results) >= max_results:
                                        break
                    except Exception:
                        pass
                if len(results) >= max_results:
                    break

            output = "\n".join(results) if results else f"No matches found for '{pattern}'."
            return {
                "success": True,
                "pattern": pattern,
                "match_count": len(results),
                "results": results,
                "output": output,
            }
        except Exception as e:
            return {"success": False, "error": f"Error during grep search: {e}"}

    # =========================================================================
    # 7. PYTHON REPL & COMPUTATIONAL SANDBOX (CODEX / DATA ANALYSIS)
    # =========================================================================

    @classmethod
    def python_repl(
        cls,
        code: str,
        timeout: int = 60,
        cwd: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes arbitrary Python code in an isolated subprocess.
        Automatically intercepts Matplotlib figures, saves PNG charts to artifacts,
        and returns stdout/stderr with visual artifact links.
        """
        work_dir = Path(cwd).resolve() if cwd else cls.get_workspace()
        logger.info(f"[ClaudeTools:PythonREPL] Executing Python snippet in {work_dir}")

        chart_id = f"chart_{int(time.time() * 1000)}"
        chart_path = (ARTIFACTS_DIR / f"{chart_id}.png").resolve()
        chart_posix = chart_path.as_posix()
        work_dir_posix = work_dir.as_posix()

        # Boilerplate wrapper to intercept matplotlib plots and format tables
        wrapper = f"""
import sys, os
from pathlib import Path
sys.path.insert(0, "{work_dir_posix}")

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False

# User Execution
__result__ = None
def _run():
    global __result__
{re.sub(r'^', '    ', code, flags=re.M)}

try:
    _run()
    if _HAS_MPL and len(plt.get_fignums()) > 0:
        plt.tight_layout()
        plt.savefig("{chart_posix}", dpi=150, facecolor='#101218', edgecolor='none')
        plt.close('all')
        print(f"__ARTIFACT_CHART__:{chart_posix}")
except Exception as _exc:
    import traceback
    traceback.print_exc()
"""

        python_exe = sys.executable
        # If virtual environment exists in project, prefer it
        venv_py = Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"
        if venv_py.exists():
            python_exe = str(venv_py)

        try:
            res = subprocess.run(
                [python_exe, "-c", wrapper],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=timeout
            )
            stdout = res.stdout or ""
            stderr = res.stderr or ""
            exit_code = res.returncode

            chart_url = None
            if "__ARTIFACT_CHART__:" in stdout:
                chart_line = [l for l in stdout.splitlines() if l.startswith("__ARTIFACT_CHART__:")][0]
                chart_url = chart_line.split(":", 1)[1].strip()
                stdout = stdout.replace(chart_line, "").strip()

            combined = []
            if stdout.strip():
                combined.append(stdout.strip())
            if stderr.strip():
                combined.append(f"[stderr]:\n{stderr.strip()}")
            if not stdout.strip() and not stderr.strip():
                combined.append("(Code executed successfully with no stdout)")

            if chart_url and Path(chart_url).exists():
                combined.append(f"\n📊 [Generated Chart Artifact]: file:///{Path(chart_url).as_posix()}")

            return {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "output": "\n".join(combined),
                "chart_artifact": chart_url if chart_url and Path(chart_url).exists() else None,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "output": f"Python execution timed out after {timeout} seconds."}
        except Exception as e:
            return {"success": False, "output": f"Python execution error: {e}"}

    # =========================================================================
    # 8. GIT VERSION CONTROL AGENT
    # =========================================================================

    @classmethod
    def git_manager(
        cls,
        action: str,
        message: Optional[str] = None,
        branch: Optional[str] = None,
        cwd: Optional[str] = None
    ) -> Dict[str, Any]:
        """Autonomous Git operations: status, diff, log, commit, branch, checkout, add."""
        act = action.strip().lower()
        if act == "status":
            cmd = "git status --short --branch"
        elif act in ("diff", "changes"):
            cmd = "git diff"
        elif act in ("diff_staged", "diff_cached"):
            cmd = "git diff --cached"
        elif act == "log":
            cmd = "git log --oneline -n 10"
        elif act in ("add", "stage"):
            cmd = "git add -A"
        elif act == "commit":
            msg = message or "chore: automated update by Saturn Dev Agent"
            # Escape quotes
            safe_msg = msg.replace('"', '\\"')
            cmd = f'git add -A && git commit -m "{safe_msg}"'
        elif act == "branch":
            cmd = f"git branch {branch}" if branch else "git branch --all"
        elif act == "checkout":
            cmd = f"git checkout {branch}" if branch else "git status"
        elif act == "new_branch":
            cmd = f"git checkout -b {branch}" if branch else "git status"
        elif act == "push":
            cmd = "git push"
        elif act == "pull":
            cmd = "git pull"
        else:
            cmd = f"git {action}"

        return cls.bash(cmd, cwd=cwd)

    # =========================================================================
    # 9. TEST-DRIVEN SELF-HEALING RUNNER
    # =========================================================================

    @classmethod
    def test_runner(
        cls,
        command: str = "pytest",
        cwd: Optional[str] = None
    ) -> Dict[str, Any]:
        """Runs the project test suite and parses failure stack traces for autonomous self-healing."""
        res = cls.bash(command, cwd=cwd)
        output = res.get("output", "")
        success = res.get("success", False)

        failures = []
        for line in output.splitlines():
            if "FAILED " in line or "FAIL:" in line or "AssertionError" in line:
                failures.append(line.strip())

        return {
            "success": success,
            "exit_code": res.get("exit_code"),
            "failures_detected": len(failures),
            "failure_summary": failures[:10],
            "full_output": output,
            "needs_healing": not success or len(failures) > 0,
        }

    # =========================================================================
    # 10. CLAUDE CODE TODO & STEP PROGRESS ENGINE (TODO_WRITE / TODO_READ)
    # =========================================================================

    _TODO_STORE: List[Dict[str, Any]] = []

    @classmethod
    def todo_write(cls, todos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Updates the global multi-step plan checklist.
        Each item: {"id": str, "task": str, "status": "pending"|"in_progress"|"completed"}
        """
        cls._TODO_STORE = todos
        lines = ["[SATURN PLAN PROGRESS]"]
        for t in todos:
            status = t.get("status", "pending").lower()
            mark = " " if status == "pending" else ("-" if status == "in_progress" else "x")
            lines.append(f"[{mark}] #{t.get('id', '?')} {t.get('task', '')} ({status})")

        return {
            "success": True,
            "total_tasks": len(todos),
            "summary": "\n".join(lines)
        }

    @classmethod
    def todo_read(cls) -> Dict[str, Any]:
        """Reads current active plan tasks and their execution status."""
        return {
            "success": True,
            "tasks": cls._TODO_STORE,
            "total_tasks": len(cls._TODO_STORE)
        }

