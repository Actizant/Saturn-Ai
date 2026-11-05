"""
core/audit_log.py
=================
Saturn's persistent tool-call audit trail.

Every tool call - native, forged feature, or (MCP) forged server verb -
appends one JSON line here: time, tool, the arguments it was called with
(secrets scrubbed), the outcome, and how long it took. This is the durable
record the email asked for; the in-memory ``CapabilityHealth`` view resets on
restart, but this file survives so the Creators page can grow a real activity
history and so a call that misbehaved can be traced after the fact.

Design rules:
- Best-effort, never fatal. A broken audit write must not break a real tool
  call, mirroring ``_record_health``. Every public method swallows its own
  exceptions and only logs at debug level.
- Secrets are redacted before they ever touch disk. Any argument whose key
  looks like a credential (key/token/secret/password/api/auth/...) is replaced
  with a placeholder, and long string values are truncated so a tool that
  returns/echoes a huge blob cannot bloat the log.
- Single small file with size-based rotation so it cannot grow without bound.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.user_paths import get_user_data_dir

logger = logging.getLogger("AuditLog")

LOGS_DIR = get_user_data_dir() / "logs"
AUDIT_FILE = LOGS_DIR / "tool_audit.jsonl"

# Rotation: once the active file crosses this size we roll it to
# ``tool_audit.jsonl.1`` (keeping exactly one previous file). Human-scale
# desktop traffic means this is effectively "never", but a runaway loop
# calling a tool in a tight cycle is exactly when we need the cap.
_MAX_BYTES = 2_000_000

# Any argument key that looks like it could carry a credential is scrubbed.
# Matched case-insensitively against the whole key so nested names like
# ``api_key``, ``apiKey`` and ``refresh_token`` are all caught.
_SECRET_KEY = re.compile(
    r"(api[-_]?key|secret|token|passw(or)?d|authorization|auth|credential|"
    r"access[-_]?key|private[-_]?key|client[-_]?secret|bearer|cookie)",
    re.IGNORECASE,
)

# Long string values are clamped so no single entry can dwarf the file.
_MAX_VALUE_CHARS = 240

_write_lock = threading.Lock()


def _redact_value(value: Any) -> Any:
    """Clamp oversized scalars and recurse into containers for structure.

    Nested dicts get the same secret-key rule as the top level, so a
    credential buried in ``{"auth": {"client_secret": ...}}`` never rides
    into the log just because its parent key looked harmless.
    """
    if isinstance(value, str):
        if len(value) > _MAX_VALUE_CHARS:
            return value[:_MAX_VALUE_CHARS] + f"… (+{len(value) - _MAX_VALUE_CHARS} chars)"
        return value
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            out[key] = ("***redacted***" if _SECRET_KEY.search(key)
                        else _redact_value(v))
        return out
    if isinstance(value, (list, tuple)):
        red = [_redact_value(v) for v in list(value)[:20]]
        if len(value) > 20:
            red.append(f"… (+{len(value) - 20} more)")
        return red
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Arbitrary objects: keep only a short type name, never a repr() that
    # might embed secrets or a giant blob.
    return f"<{type(value).__name__}>"


def redact_args(args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a JSON-safe, secret-scrubbed copy of a tool's arguments."""
    if not isinstance(args, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in args.items():
        k = str(key)
        if _SECRET_KEY.search(k):
            out[k] = "***redacted***"
        else:
            out[k] = _redact_value(value)
    return out


def _rotate_if_needed() -> None:
    try:
        if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size > _MAX_BYTES:
            prev = AUDIT_FILE.with_suffix(".jsonl.1")
            try:
                if prev.exists():
                    prev.unlink()
                AUDIT_FILE.replace(prev)
            except Exception:
                # If the rename fails, truncate in place rather than grow.
                AUDIT_FILE.write_text("", encoding="utf-8")
    except Exception as exc:
        logger.debug(f"[Audit] rotation skipped: {exc}")


class AuditLog:
    """Append-only JSONL recorder for every tool call."""

    @staticmethod
    def record(tool: str, ok: bool, duration_ms: float,
               args: Optional[Dict[str, Any]] = None,
               error: Optional[str] = None, tier: str = "mcp") -> None:
        """Persist one tool-call outcome. Best-effort; never raises to caller."""
        try:
            entry = {
                "ts": time.time(),
                "tool": str(tool),
                "tier": str(tier),
                "ok": bool(ok),
                "duration_ms": round(float(duration_ms), 1),
                "args": redact_args(args),
            }
            if error:
                entry["error"] = str(error)[:400]
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with _write_lock:
                _rotate_if_needed()
                with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as exc:
            logger.debug(f"[Audit] record failed: {exc}")

    @staticmethod
    def recent(limit: int = 50) -> list:
        """The most recent ``limit`` audit entries, oldest-first.

        Read helper for the Creators page's activity/telemetry views. Returns
        an empty list if the file is absent or unreadable - the UI degrades
        that section rather than inventing history.
        """
        try:
            if not AUDIT_FILE.exists():
                return []
            with open(AUDIT_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            out = []
            for ln in lines[-max(0, int(limit)):]:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
            return out
        except Exception as exc:
            logger.debug(f"[Audit] recent failed: {exc}")
            return []

    @staticmethod
    def file_path() -> Path:
        return AUDIT_FILE
