"""
core/permissions.py
===================
The declared-needs + approval-gate model behind forged tools.

The design (saturn_command.html, CREATORS detail panel) puts a three-way
select on every forged tool row - **Always allow / Ask each time / Block** -
and a NETWORK / FILES / COMMANDS / SANDBOX grid next to it. This module is
the engine half of that picture:

* ``classify_defaults`` turns a tool's *declared* needs (recorded in the
  forge manifest at creation time) into a sensible starting level, so a pure
  read-only verb does not nag and a verb that shells out does.
* ``resolve`` is what the execution gates call before a forged tool runs:
  the user's override if there is one, otherwise the classified default.
* ``PermissionsStore`` persists overrides in one small JSON file so the
  choices survive restarts and the page can show which rows are overridden.

Honesty rule inherited from the design: we gate on *declared* needs and
approval. We never claim an OS sandbox we do not have - the SANDBOX cell in
the UI says what is actually enforced (approval gating, secret-free child
env, lazy start, kill switch), all of which live elsewhere.

Like the audit log, everything here is best-effort: a corrupt store falls
back to defaults, and a failed classification falls back to "ask", which is
the conservative direction (asking is annoying; guessing is dangerous).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.user_paths import get_user_data_dir

logger = logging.getLogger("Permissions")

# ── The three levels, exactly as the design's <select> options ───────────────
ALLOW = "allow"    # "Always allow"  - run without asking
ASK = "ask"        # "Ask each time" - park behind core/confirm before running
BLOCK = "block"    # "Block"         - refuse outright

LEVELS = (ALLOW, ASK, BLOCK)

# Human labels the UI can reuse so the page and the engine never drift.
LEVEL_LABELS = {
    ALLOW: "Always allow",
    ASK: "Ask each time",
    BLOCK: "Block",
}

# The declared-need buckets the manifest records and the .pmg grid displays.
NEED_NETWORK = "network"
NEED_FILES = "files"
NEED_COMMANDS = "commands"
DECLARED_NEEDS = (NEED_NETWORK, NEED_FILES, NEED_COMMANDS)

# What the SANDBOX cell is allowed to claim, because each of these is real:
# approval gating (this module + core/confirm), secret-free child env and the
# forged-tools kill switch (core/mcp_manager), the redacted audit trail
# (core/audit_log). Per-tool CPU/memory caps are NOT enforced, so we do not
# say they are.
SANDBOX_FACTS = [
    "Approval gating on risky calls",
    "Secret-free environment for server processes",
    "Every call recorded in the audit trail",
]

STORE_FILE = get_user_data_dir() / "config" / "tool_permissions.json"

_lock = threading.Lock()

# ── Word lists used by the conservative classifier ──────────────────────────
# Order matters: a verb matching a risky token is never demoted by also
# matching a harmless one ("read_and_send_logs" asks, it does not allow).
_RISKY_NAME_TOKENS = (
    "delete", "remove", "unregister", "erase", "wipe", "drop",
    "write", "save", "create", "mkdir", "move", "rename", "install",
    "run", "exec", "shell", "command", "subprocess", "system",
    "send", "post", "upload", "publish", "message", "email",
    "shutdown", "restart", "kill", "pay", "purchase", "unsubscribe",
)
_READ_NAME_TOKENS = (
    "get", "list", "search", "fetch", "read", "show", "find", "lookup",
    "describe", "count", "check", "view", "query", "score", "weather",
    "analyze", "compare",
)


def _norm(tool_name: str) -> str:
    return (tool_name or "").strip().lower()


def has_write_verb(name: str) -> bool:
    """True when a tool *name* alone suggests it changes something.

    Shared with the forge fallback classifier so both sides agree on what
    counts as mutating, e.g. after a path is moved or deleted.
    """
    n = _norm(name)
    return any(tok in n for tok in _RISKY_NAME_TOKENS)


def classify_defaults(requires: Optional[List[str]],
                      tool_name: str = "",
                      description: str = "") -> str:
    """Pick the starting level for a tool from its declared needs and words.

    Conservative ladder:
      * commands declared, or a name that mutates the machine or talks to the
        world (delete/run/send/write...)  -> ASK
      * no declared needs and the name looks purely read-only              -> ALLOW
      * network or files declared without risky words                      -> ALLOW
        for an explicit read verb, otherwise ASK - "get_weather" is free,
        "process_invoice" with files declared is not obviously harmless.
    """
    needs = {str(r).lower() for r in (requires or []) if r}
    n = _norm(tool_name)
    d = (description or "").lower()

    if NEED_COMMANDS in needs or any(tok in n for tok in
                                     ("run", "exec", "shell", "delete",
                                      "remove", "write", "send", "install",
                                      "shutdown", "restart")):
        return ASK

    read_ish = any(tok in n for tok in _READ_NAME_TOKENS)
    if not needs:
        # Nothing declared and nothing scary in the name: a pure in-process
        # calculation. Still refuse to auto-trust descriptions that promise
        # to touch things.
        if any(tok in d for tok in ("delete", "send ", "write to", "runs ",
                                    "execute", "uploads")):
            return ASK
        return ALLOW

    if needs & {NEED_NETWORK, NEED_FILES}:
        return ALLOW if read_ish and not has_write_verb(tool_name) else ASK

    return ASK


def risk_level(requires: Optional[List[str]], level: str = ASK,
               tool_name: str = "") -> str:
    """LOW / MEDIUM / HIGH for the ``.rk`` badge - declared blast radius.

    commands or a mutating verb name is HIGH, network/files access is
    MEDIUM, a pure in-process read is LOW. A blocked row runs nothing, so
    its live risk is LOW regardless of what it could have done."""
    if level == BLOCK:
        # Nothing runs, so nothing is at risk; show the lowest the user chose.
        return "LOW"
    needs = {str(r).lower() for r in (requires or []) if r}
    if NEED_COMMANDS in needs or has_write_verb(tool_name):
        return "HIGH"
    if needs & {NEED_NETWORK, NEED_FILES}:
        return "MEDIUM"
    return "LOW"


# ── The little store ────────────────────────────────────────────────────────

class PermissionsStore:
    """``{capability: {tool: level}}`` persisted as plain JSON.

    Capability is the server/skill name; tool is the verb. A capability-level
    entry under ``"*"`` applies to every tool in that capability, which is
    what the page's per-row selects write (per tool), while ``"*"`` gives a
    future "block this whole server" affordance a place to live.
    """

    @staticmethod
    def _load() -> Dict[str, Any]:
        try:
            if STORE_FILE.exists():
                data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            logger.debug(f"[Permissions] store unreadable, using defaults: {exc}")
        return {}

    @staticmethod
    def get_level(capability: str, tool: str) -> Optional[str]:
        """The stored override for this exact tool, or the capability-wide
        one, or None when the user never touched this row."""
        try:
            with _lock:
                caps = PermissionsStore._load().get("overrides", {})
            entry = caps.get(str(capability))
            if not isinstance(entry, dict):
                return None
            for key in (str(tool), "*"):
                lvl = entry.get(key)
                if lvl in LEVELS:
                    return lvl
        except Exception as exc:
            logger.debug(f"[Permissions] get failed: {exc}")
        return None

    @staticmethod
    def set_level(capability: str, tool: str, level: Optional[str]) -> bool:
        """Set (or with level=None, clear) one override. Returns success."""
        try:
            with _lock:
                data = PermissionsStore._load()
                caps = data.setdefault("overrides", {})
                entry = caps.setdefault(str(capability), {})
                if level is None:
                    entry.pop(str(tool), None)
                    if not entry:
                        caps.pop(str(capability), None)
                else:
                    if level not in LEVELS:
                        return False
                    entry[str(tool)] = level
                data["updated_at"] = time.time()
                STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
                STORE_FILE.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            return True
        except Exception as exc:
            logger.debug(f"[Permissions] set failed: {exc}")
            return False

    @staticmethod
    def all_overrides() -> Dict[str, Dict[str, str]]:
        try:
            with _lock:
                caps = PermissionsStore._load().get("overrides", {})
            return {c: {t: l for t, l in (e or {}).items() if l in LEVELS}
                    for c, e in caps.items() if isinstance(e, dict)}
        except Exception:
            return {}


def resolve(capability: str, tool: str,
            requires: Optional[List[str]] = None,
            tool_name: str = "",
            description: str = "") -> str:
    """The level a gate must honour right now: the user's choice, else the
    classified default. Never returns anything outside ``LEVELS``."""
    override = PermissionsStore.get_level(capability, tool)
    if override:
        return override
    return classify_defaults(requires, tool_name or tool, description)


def effective(capability: str, tool: str,
              requires: Optional[List[str]] = None,
              tool_name: str = "",
              description: str = "") -> Dict[str, Any]:
    """The full picture one permission row needs for the UI: current level,
    what it would be without the override, declared needs and risk."""
    default = classify_defaults(requires, tool_name or tool, description)
    level = PermissionsStore.get_level(capability, tool) or default
    return {
        "level": level,
        "level_label": LEVEL_LABELS.get(level, level),
        "default": default,
        "declared_requires": sorted({str(r).lower() for r in (requires or []) if r}),
        "risk": risk_level(requires, level, tool_name or tool),
        "overridden": level != default,
        "sandbox_facts": list(SANDBOX_FACTS),
    }


def blocked_capabilities() -> Dict[str, str]:
    """Capability-wide ``"*"`` blocks, for the batch gate: server name -> the
    tool name it was refused under is resolved by the caller; this just says
    which capabilities the user switched off at the permission layer."""
    out: Dict[str, str] = {}
    for cap, entry in PermissionsStore.all_overrides().items():
        if entry.get("*") == BLOCK:
            out[cap] = BLOCK
    return out
