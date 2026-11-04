"""What the perimeter leaves behind, so the owner can read it later.

Two separate logs, because they answer two different questions:

* ``alerts.jsonl`` - discrete events: an unverified person arrived, evidence was
  captured, the workstation was locked, a bypass was granted. These go into
  persistent memory as well as the file, so they survive a restart and show up
  when the assistant is asked about them.
* ``away_log.jsonl`` - the "blind logging" fallback. It records **metadata**
  only: when the desk was empty, which window had focus, how far the cursor
  moved, how idle the machine was. Never keystrokes, never window contents,
  never text the user typed. That distinction is not a limitation; recording
  what an unverified person types is a keylogger, and this will not be one.

Delivery is a cursor, not a deletion: the alert stays in the file for the audit
trail, and a single integer counts how many have been shown to the owner.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

# Which events are important enough to remember permanently.
MEMORY_KINDS = {"workstation_locked", "bypass_granted", "camera_fault", "lock_aborted"}

_PREVIEW = 400


def security_dir() -> Path:
    override = os.environ.get("SATURN_SECURITY_DIR")
    if override:
        target = Path(override).expanduser()
    else:
        from core.user_paths import get_user_data_dir

        target = get_user_data_dir() / "security"
    target.mkdir(parents=True, exist_ok=True)
    return target


def alerts_path() -> Path:
    return security_dir() / "alerts.jsonl"


def away_path() -> Path:
    return security_dir() / "away_log.jsonl"


def cursor_path() -> Path:
    return security_dir() / "alerts.cursor"


def _append(path: Path, payload: dict) -> bool:
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        return []
    return out


# ── Alerts ──────────────────────────────────────────────────────────────────

def record(kind: str, text: str, evidence: Optional[str] = None,
           state: Optional[dict] = None) -> dict:
    """Append one event, and remember the important ones permanently."""
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "epoch": round(time.time(), 3),
        "kind": str(kind or "event"),
        "text": str(text or "")[:_PREVIEW],
        "evidence": str(evidence) if evidence else None,
        "state": (state or {}).get("state") if isinstance(state, dict) else None,
        "reason": (state or {}).get("reason") if isinstance(state, dict) else None,
    }
    _append(alerts_path(), payload)
    if payload["kind"] in MEMORY_KINDS:
        _remember(payload)
    return payload


def _remember(payload: dict) -> None:
    """Put it in persistent memory so it survives a restart."""
    try:
        from core.memory_cortex import MemoryCortex

        sentence = f"Security perimeter: {payload['text']} (at {payload['ts']})."
        if payload.get("evidence"):
            sentence += f" Evidence: {payload['evidence']}."
        MemoryCortex.get_instance().remember(sentence, category="security_alert",
                                             importance=0.9)
    except Exception:
        # Memory being unavailable must never block the alert file.
        pass


def recent_alerts(limit: int = 20) -> list[dict]:
    items = _read_lines(alerts_path())
    return list(reversed(items))[:max(1, int(limit))]


def _cursor() -> int:
    try:
        return max(0, int(cursor_path().read_text(encoding="utf-8").strip() or 0))
    except (OSError, ValueError):
        return 0


def _set_cursor(value: int) -> None:
    try:
        cursor_path().write_text(str(max(0, int(value))), encoding="utf-8")
    except OSError:
        pass


def pending() -> list[dict]:
    """Events the owner has not been told about yet."""
    items = _read_lines(alerts_path())
    cursor = _cursor()
    if cursor >= len(items):
        return []
    return items[cursor:]


def acknowledge() -> int:
    """Mark everything currently in the file as delivered."""
    total = len(_read_lines(alerts_path()))
    _set_cursor(total)
    return total


def deliver(callback: Callable[[str], None]) -> int:
    """Hand each unread event to ``callback`` once, then mark them read."""
    items = pending()
    if not items:
        return 0
    delivered = 0
    for entry in items:
        try:
            callback(str(entry.get("text") or entry.get("kind") or "event"))
            delivered += 1
        except Exception:
            # Retry this one next time rather than skipping past it forever.
            break
    if delivered:
        _set_cursor(_cursor() + delivered)
    return delivered


def summary_line(entries: Optional[list[dict]] = None) -> str:
    """One sentence a voice can say without reading a file out loud."""
    items = pending() if entries is None else entries
    if not items:
        return ""
    kinds = [str(entry.get("kind") or "event") for entry in items]
    if "workstation_locked" in kinds:
        return ("While you were away the workstation was locked after an unverified "
                "person arrived at the desk.")
    if "camera_fault" in kinds:
        return "The security camera stopped responding while you were away."
    return f"{len(items)} security event{'s' if len(items) != 1 else ''} happened while you were away."


# ── Away log (metadata only) ────────────────────────────────────────────────

def note_away(sample: dict, away_seconds: float) -> bool:
    """One metadata line about an empty desk. No content is ever recorded."""
    sample = sample or {}
    hub = sample.get("hub") or {}
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "epoch": round(time.time(), 3),
        "away_seconds": round(float(away_seconds or 0), 1),
        "state": sample.get("state"),
        "app": sample.get("app") or sample.get("process_name") or "",
        "title": str(sample.get("title") or "")[:120],
        "idle_seconds": sample.get("user_idle_seconds"),
        "cursor_moved_px": sample.get("cursor_moved_px"),
        "camera_ok": hub.get("ok"),
    }
    return _append(away_path(), payload)


def away_report(minutes: float = 60.0) -> dict:
    """What the empty-desk log knows, stated for what it is."""
    window = max(1.0, float(minutes or 60.0))
    cutoff = time.time() - window * 60.0
    entries = [entry for entry in _read_lines(away_path())
               if float(entry.get("epoch") or 0) >= cutoff]
    if not entries:
        return {"ok": True, "minutes": window, "entries": 0,
                "message": f"No unmonitored desk activity in the last {int(window)} minutes."}

    apps: dict[str, int] = {}
    for entry in entries:
        name = str(entry.get("app") or "unknown")
        apps[name] = apps.get(name, 0) + 1
    ranked = sorted(apps.items(), key=lambda item: item[1], reverse=True)[:5]
    first, last = entries[0].get("ts"), entries[-1].get("ts")
    longest = max(float(entry.get("away_seconds") or 0) for entry in entries)
    return {
        "ok": True,
        "minutes": window,
        "entries": len(entries),
        "first": first,
        "last": last,
        "longest_away_minutes": round(longest / 60.0, 1),
        "apps": [{"app": name, "samples": count} for name, count in ranked],
        "content_recorded": False,
        "message": (f"{len(entries)} readings while the desk was empty between {first} and "
                    f"{last}. Longest stretch {round(longest / 60.0)} minutes. Most used: "
                    + ", ".join(f"{name} ({count})" for name, count in ranked)
                    + ". Only timestamps and app names were recorded - never keystrokes "
                      "or window contents."),
    }
