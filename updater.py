"""Check for and apply fast-forward updates from the Saturn GitHub repository."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REMOTE = "https://github.com/Actizant/Saturn.git"
# Fallback only. The branch to update from is resolved from the live checkout
# (see _active_branch); this constant is used solely when HEAD is detached and
# there is no branch name to read.
BRANCH = "main"


def _run_git(base_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=base_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _active_branch(base_dir: Path) -> str:
    """Return the branch this checkout is actually on.

    Pinning the update target to one hard-coded name meant any checkout sitting
    on a different release branch never found its upstream and silently stopped
    receiving updates. Read the real branch from the working tree instead, and
    only fall back to the default when HEAD is detached.
    """
    probe = _run_git(base_dir, "rev-parse", "--abbrev-ref", "HEAD")
    if probe.returncode == 0:
        name = probe.stdout.strip()
        if name and name != "HEAD":
            return name
    return BRANCH


def update_from_github(base_dir: Path) -> bool:
    """Update a clean Git checkout and return whether the app should restart."""
    if os.environ.get("SATURN_SKIP_UPDATE") == "1":
        return False

    if not (base_dir / ".git").exists():
        return False

    status = _run_git(base_dir, "status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        return False

    remote = _run_git(base_dir, "remote", "get-url", "origin")
    if remote.returncode != 0 or not remote.stdout.strip():
        _run_git(base_dir, "remote", "add", "origin", REMOTE)

    branch = _active_branch(base_dir)

    fetch = _run_git(base_dir, "fetch", "origin", branch, "--quiet")
    if fetch.returncode != 0:
        return False

    local = _run_git(base_dir, "rev-parse", "HEAD")
    upstream = _run_git(base_dir, "rev-parse", f"origin/{branch}")
    if local.returncode != 0 or upstream.returncode != 0:
        return False
    if local.stdout.strip() == upstream.stdout.strip():
        return False

    ancestor = _run_git(base_dir, "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}")
    if ancestor.returncode != 0:
        return False

    pull = _run_git(base_dir, "pull", "--ff-only", "origin", branch)
    if pull.returncode != 0:
        return False
    return True


def restart_application(base_dir: Path) -> None:
    """Replace the current process with the updated application."""
    os.execv(sys.executable, [sys.executable, str(base_dir / "main.py"), *sys.argv[1:]])
