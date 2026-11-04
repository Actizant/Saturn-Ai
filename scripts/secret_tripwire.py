#!/usr/bin/env python3
"""secret_tripwire.py - Saturn pre-commit secret guard.

Blocks a commit when staged files either

  1. violate the config/ default-deny policy: the only files that may be
     committed under config/ are __init__.py, mcp_servers.json,
     create_desktop_shortcut.ps1, *.example.json and config/models/**.
     This mirrors .gitignore but also catches `git add -f` additions.

  2. contain a real-world credential shape anywhere in their text: Google,
     OpenAI, Anthropic, GitHub, Slack, AWS, Azure, Hugging Face keys, OAuth
     tokens and private key blocks. These fire regardless of placeholder
     rules - a committed `AIza...` string is a live key, period.

  3. assign a literal to a secret-named field (`api_key`, `bot_token`,
     `password`, ...) whose value is 12+ characters and does not look like a
     placeholder (YOUR_X, CHANGE_ME, <token>, example, dummy...).

Usage:
    python scripts/secret_tripwire.py --staged    # what the hook runs
    python scripts/secret_tripwire.py --all       # audit every tracked file
    python scripts/secret_tripwire.py path ...    # audit specific files

Escape hatch for a deliberate, reviewed line: end it with the comment
    # tripwire: allow

Install (once per clone; the versioned copy lives in scripts/git-hooks/):
    cp scripts/git-hooks/pre-commit .git/hooks/pre-commit
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# -- config/ default-deny allowlist (keep in sync with .gitignore) ----------
CONFIG_ALLOW_EXACT = {
    "config/__init__.py",
    "config/mcp_servers.json",
    "config/create_desktop_shortcut.ps1",
}
CONFIG_ALLOW_SUFFIXES = (".example.json",)
CONFIG_ALLOW_PREFIXES = ("config/models/",)

# -- credential shapes: always violations, no placeholder excuse ------------
SHAPES = [
    ("Google/Gemini API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("OpenAI API key", re.compile(r"(?<![A-Za-z0-9])sk-(proj-)?[A-Za-z0-9_]{20,}\b")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9])(ghp|gho|ghu|ghs|glt)_[0-9A-Za-z]{30,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[0-9A-Za-z_]{20,}")),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Azure secret", re.compile(r"(?<![A-Za-z0-9])~[a-z0-9]{43}\b")),
    ("Hugging Face token", re.compile(r"hf_[A-Za-z0-9]{30,}")),
    ("Google OAuth access token", re.compile(r"ya29\.[0-9A-Za-z_\-]{40,}")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# -- generic secret-name assignments ----------------------------------------
ASSIGN = re.compile(
    r"(?i)\b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|bot[_-]?token|"
    r"client[_-]?secret|secret[_-]?key|password|passwd)\b"
    r"['\"]?\s*[:=]\s*['\"]([^'\"]{12,})['\"]"
)
PLACEHOLDER = re.compile(
    r"(?i)(your[_\-]|change[_\-]|replace|placeholder|example|dummy|sample|test"
    r"|xxx+|\.{3}|<[^>]+>|\{\{|\$\{|\*\*\*)"
)
INLINE_ALLOW = re.compile(r"tripwire:\s*allow")

MAX_SCAN_BYTES = 4 * 1024 * 1024


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _split_z(raw: bytes) -> list[str]:
    return [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]


def candidate_paths(mode: str, explicit: list[str]) -> list[str]:
    if explicit:
        return [p.replace("\\", "/") for p in explicit]
    if mode == "staged":
        return _split_z(_git("diff", "--cached", "-z", "--name-only",
                             "--diff-filter=ACM"))
    return _split_z(_git("ls-files", "-z"))


def read_content(path: str, staged: bool) -> str | None:
    """Return the file's text, or None when it is binary/unreadable/too big."""
    if path.endswith((".task", ".png", ".jpg", ".ico", ".wav", ".zip", ".pyc")):
        return None
    if staged:
        proc = subprocess.run(["git", "show", f":{path}"], capture_output=True)
        if proc.returncode != 0:
            return None
        raw = proc.stdout
    else:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > MAX_SCAN_BYTES:
            return None
        raw = p.read_bytes()
    if b"\0" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def config_policy_violation(path: str) -> bool:
    if not path.startswith("config/"):
        return False
    norm = path.replace("\\", "/")
    if norm in CONFIG_ALLOW_EXACT:
        return False
    if norm.endswith(CONFIG_ALLOW_SUFFIXES):
        return False
    if norm.startswith(CONFIG_ALLOW_PREFIXES):
        return False
    return True


def mask(value: str) -> str:
    if len(value) <= 12:
        return value[:4] + "*" * (len(value) - 4)
    return value[:8] + "…" + "*" * 8 + value[-3:]


def scan_text(path: str, text: str, findings: list[str]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        if INLINE_ALLOW.search(line):
            continue
        for label, rx in SHAPES:
            m = rx.search(line)
            if m:
                findings.append(
                    f"  BLOCKED {path}:{lineno} [{label}] {mask(m.group(0))}")
        m = ASSIGN.search(line)
        if m:
            val = m.group(1)
            # real tokens never contain whitespace or ellipses; prose values
            # like "run npm auth to get this" are illustrative, not secrets
            if " " not in val and not PLACEHOLDER.search(val):
                findings.append(
                    f"  BLOCKED {path}:{lineno} [literal assigned to a "
                    f"secret-named field] {mask(val)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true",
                       help="check the staged index (pre-commit mode)")
    group.add_argument("--all", action="store_true",
                       help="check every tracked file")
    group.add_argument("paths", nargs="*", help="explicit files to check")
    args = ap.parse_args()

    staged = args.staged and not args.paths and not args.all
    paths = candidate_paths("staged" if args.staged else "all", args.paths)

    findings: list[str] = []
    for path in paths:
        if config_policy_violation(path):
            findings.append(
                f"  BLOCKED {path} [config/ is default-deny - only "
                f"__init__.py, mcp_servers.json, create_desktop_shortcut.ps1, "
                f"*.example.json and models/** may be committed]")
            continue
        text = read_content(path, staged)
        if text is not None:
            scan_text(path, text, findings)

    if findings:
        print("secret-tripwire: commit blocked - credentials or disallowed "
              "config files found:", file=sys.stderr)
        for f in findings:
            print(f, file=sys.stderr)
        print("Move real keys out of these files (config/api_keys.json is "
              "gitignored and read at runtime), or append "
              "'# tripwire: allow' to a deliberately reviewed line.",
              file=sys.stderr)
        return 1
    print(f"secret-tripwire: clean ({len(paths)} file(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
