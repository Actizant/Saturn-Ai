"""The camera perimeter, as tools the assistant can call.

Nine tools, and one rule that shapes all of them: **the passphrase is never a
tool parameter**. The model can ask for the prompt to appear, but a human types
the secret into a dialog that the model never sees. ``set_perimeter_passphrase``
therefore takes no arguments, and there is deliberately no way to pass a
passphrase in - a fail-safe whose secret is spoken aloud to a language model is
not a fail-safe.

The wording throughout is deliberately unimpressive: it reports what was
measured, says "I cannot confirm" when it cannot, and never claims the machine
is protected when the camera is blind.
"""

from __future__ import annotations

from core import passphrase_vault
from core.presence_watch import get_presence_watcher

# Wired by main.py at startup, the same way core/confirm.py is.
_ui = None


def bind(ui) -> None:
    """Give these tools a way to reach the interface for the passphrase prompt."""
    global _ui
    _ui = ui


# What each perimeter event is worth saying out loud. Everything it reports is
# logged; only these reach speech, because an assistant that narrates every
# camera reading is one you stop listening to.
_SEVERITIES = {
    "workstation_locked": "The workstation was locked because someone I could not "
                          "verify was at the desk.",
    "camera_fault": "The security camera stopped working, so the perimeter cannot see.",
    "bypass_granted": "The perimeter is paused after a correct passphrase.",
    "confirmation_unavailable": "The perimeter has nowhere to ask for a passphrase, so "
                                "an unrecognised face will not lock anything.",
    "unverified_person": "Someone I do not recognise is at the desk.",
    "away_unlocked": "You left the desk with the session unlocked.",
    "lock_failed": "The perimeter tried to lock the workstation and could not.",
    "evidence_captured": "A photo and clip of an unrecognised person were saved.",
}

_SPOKEN = frozenset({"workstation_locked", "camera_fault", "unverified_person"})


def _away_context() -> dict:
    """The desk facts the away-log records, from the one sensor that has them.

    ``note_away`` expects ``title`` and ``app``; the sensorium snapshot calls
    them ``window_title`` and ``process_name``. This is the only place the two
    vocabularies meet, so the rename lives here rather than in either module.
    """
    try:
        from core.sensorium import get_sensorium

        snapshot = get_sensorium().get_snapshot() or {}
    except Exception:
        return {}
    return {
        "title": snapshot.get("window_title") or "",
        "app": snapshot.get("process_name") or "",
        "process_name": snapshot.get("process_name") or "",
        "user_idle_seconds": snapshot.get("user_idle_seconds"),
        "cursor_moved_px": snapshot.get("cursor_moved_px"),
    }


def boot(ui=None, speak=None) -> str:
    """Start the perimeter for this process, honouring "arm at startup".

    Called once from the boot thread. It returns a sentence for the log rather
    than raising, because a boot hook that can take the app down with it is
    worse than one that says what it could not do.

    The passphrase seam matters more than it looks: without it
    ``confirm_before_lock`` can never be satisfied, so the perimeter would hold
    on every unrecognised face instead of asking a human. That is the safe
    direction to fail in, but it is still a failure.
    """
    from core.security_perimeter import start_at_boot

    ui = ui if ui is not None else _ui
    log = getattr(ui, "write_log", None) if ui is not None else None
    ask = getattr(ui, "ask_passphrase", None) if ui is not None else None

    def _announce(kind: str, payload: dict | None = None) -> None:
        payload = payload or {}
        text = _SEVERITIES.get(kind) or str(kind).replace("_", " ")
        reason = str(payload.get("reason") or "").strip()
        line = f"SEC: {text}" + (f" ({reason})" if reason else "")
        if log is not None:
            try:
                log(line)
            except Exception:
                pass
        if speak is not None and kind in _SPOKEN:
            try:
                speak(text)
            except Exception:
                pass

    report = start_at_boot(announce=_announce, ask_passphrase=ask,
                           context=_away_context)

    if report.get("started"):
        message = ("The security perimeter is armed and watching."
                   if report.get("armed") else
                   "The security perimeter is watching but not armed.")
    else:
        message = (f"The security perimeter is not watching: "
                   f"{report.get('reason') or 'unknown reason'}")
    if log is not None:
        try:
            log(f"SEC: {message}")
        except Exception:
            pass
    return message


def _perimeter():
    from core.security_perimeter import get_perimeter

    return get_perimeter()


def _health_line(health: dict) -> str:
    reason = str((health or {}).get("reason") or "")
    if (health or {}).get("ok"):
        index = health.get("index")
        return f"The camera is working on device {index}."
    if reason == "LENS_COVERED":
        return "The camera is covered - the picture is black."
    if reason == "NO_FRAME":
        return "The camera opened but is not delivering pictures."
    if reason == "BUSY":
        return "Another part of Saturn is using the camera right now."
    if reason == "OPENCV_UNAVAILABLE":
        return "OpenCV is not installed, so there is no camera pipeline."
    if reason.startswith("OPEN_FAILED"):
        return f"The camera would not open: {reason.split(':', 1)[-1].strip()}."
    return "No working camera was found."


def camera_status(parameters: dict, player=None) -> str:
    watcher = get_presence_watcher()
    health = watcher.hub.health()
    sample = watcher.snapshot()
    identity = "and it can name people" if sample.get("identity_available") else \
        "but naming people needs the two face models"
    state = sample.get("state") or "STARTING"
    return (
        f"{_health_line(health)} Presence monitoring is {state.lower()} "
        f"({sample.get('samples', 0)} readings). It can see faces {identity}."
    )


def security_status(parameters: dict, player=None) -> str:
    status = _perimeter().status()
    parts = []

    if not status.get("enabled"):
        parts.append("The perimeter is switched off.")
    elif status.get("armed"):
        parts.append(f"The perimeter is armed for {status.get('owner') or 'the owner'}.")
    else:
        parts.append("The perimeter is watching but not armed, so it will not lock "
                     "anything.")

    if status.get("identity_available"):
        enrolled = status.get("enrolled") or []
        parts.append(f"Identity is available; enrolled: {', '.join(enrolled) or 'nobody'}.")
    else:
        parts.append("Identity is not available yet, so an unrecognised face can never "
                     "cause a lock - only a camera fault report.")

    if status.get("confirm_before_lock", True):
        if status.get("can_prompt", True):
            parts.append("Before any lock it will ask for your passphrase, so a face it "
                         "fails to recognise cannot lock you out by itself.")
        else:
            parts.append("It is set to ask for your passphrase before acting, but no "
                         "prompt is available right now, so it will not act on an "
                         "unrecognised face at all.")
    else:
        parts.append("It is set to lock immediately, without asking you first.")

    passphrase = status.get("passphrase") or {}
    if not passphrase.get("configured"):
        parts.append("No passphrase is set.")
    elif passphrase.get("bypass_active"):
        minutes = int(passphrase.get("bypass_minutes") or 0)
        parts.append(f"The passphrase bypass has {minutes} minutes left.")

    mode = status.get("away_lock")
    if mode == "lock":
        parts.append(f"If the desk is empty for {int(status.get('away_seconds', 300) // 60)} "
                     "minutes it will lock the workstation.")
    elif mode == "announce":
        parts.append("If you leave the desk unlocked it will tell you rather than lock.")
    else:
        parts.append("Empty-desk warnings are switched off.")

    health = status.get("camera") or {}
    if not health.get("ok"):
        parts.append(f"The camera is not proving itself: {health.get('reason')}.")

    pending = int(status.get("pending_alerts") or 0)
    if pending:
        parts.append(f"{pending} security event{'s' if pending != 1 else ''} are waiting "
                     "to be reviewed.")

    return " ".join(parts)


def arm_security_perimeter(parameters: dict, player=None) -> str:
    owner = str((parameters or {}).get("owner") or "").strip()
    result = _perimeter().arm(owner=owner)
    if not result.get("ok"):
        problems = result.get("problems") or [result.get("message", "")]
        return ("I have not armed the perimeter. " + " ".join(problems)).strip()
    return str(result.get("message") or "Perimeter armed.")


def disarm_security_perimeter(parameters: dict, player=None) -> str:
    result = _perimeter().disarm()
    return str(result.get("message") or "Perimeter disarmed.")


def take_photo(parameters: dict, player=None) -> str:
    from core import media_capture

    note = str((parameters or {}).get("note") or "requested")
    result = media_capture.take_photo(note=note)
    if not result.get("ok"):
        return f"I could not take a photo: {result.get('message') or result.get('error')}"
    return (f"Saved a photo to {result.get('path')} at "
            f"{result.get('resolution')} ({result.get('bytes', 0) // 1024} KB).")


def record_video(parameters: dict, player=None) -> str:
    from core import media_capture

    params = parameters or {}
    try:
        seconds = float(params.get("seconds") or 10.0)
    except (TypeError, ValueError):
        seconds = 10.0
    seconds = max(1.0, min(seconds, 60.0))
    note = str(params.get("note") or "requested")

    result = media_capture.record_video(seconds=seconds, note=note)
    if not result.get("ok"):
        return f"I could not record a clip: {result.get('message') or result.get('error')}"
    container = str(result.get("container") or "").lstrip(".")
    detail = f"Saved {result.get('seconds')}s of video to {result.get('path')}"
    if container and container != "mp4":
        detail += f" (written as {container}, because this OpenCV build has no MP4 encoder)"
    return detail + "."


def set_away_lock(parameters: dict, player=None) -> str:
    from core.security_perimeter import AWAY_MODES

    mode = str((parameters or {}).get("mode") or "").strip().lower()
    if mode not in AWAY_MODES:
        return "Choose one of: off, announce, or lock."
    _perimeter().update({"away_lock": mode})
    if mode == "off":
        return "Empty-desk warnings are off."
    if mode == "announce":
        return ("If the desk stays empty while the session is unlocked, I will say so "
                "but leave the machine alone.")
    return ("If the desk stays empty while the session is unlocked, the workstation "
            "will lock. That needs arming first, and never happens while the camera "
            "is faulty.")


def review_away_activity(parameters: dict, player=None) -> str:
    from core import security_alerts

    params = parameters or {}
    try:
        minutes = float(params.get("minutes") or 60.0)
    except (TypeError, ValueError):
        minutes = 60.0
    minutes = max(1.0, min(minutes, 24 * 60.0))
    report = security_alerts.away_report(minutes)
    return str(report.get("message") or "There is nothing to report.")


def set_perimeter_passphrase(parameters: dict, player=None) -> str:
    """Put the passphrase prompt on screen. The model never sees the answer."""
    if _ui is None or not hasattr(_ui, "ask_passphrase"):
        return "The interface is not available, so the passphrase cannot be set safely."

    def _done(text):
        # This callback is the only place the typed secret exists, so it is also
        # the only place the passphrase is actually stored. Showing the prompt and
        # logging "set" without calling set_passphrase left the vault empty, which
        # is why arming kept reporting no passphrase despite a successful-looking
        # setup. The dialog has already run its confirm/match checks.
        try:
            if not text:
                _ui.write_log("SYS: Perimeter passphrase was not set.")
                return
            result = passphrase_vault.set_passphrase(text)
            if result.get("ok"):
                _ui.write_log("SYS: Perimeter passphrase set.")
            else:
                reason = result.get("message") or result.get("error") or "unknown reason"
                _ui.write_log(f"SYS: Passphrase was not saved - {reason}")
        except Exception as exc:
            try:
                _ui.write_log(f"SYS: Could not save the passphrase: {exc}")
            except Exception:
                pass

    try:
        _ui.ask_passphrase(
            "Choose the passphrase that unlocks the security perimeter when the "
            "camera cannot confirm who is at the desk.",
            _done,
            mode="new",
        )
    except Exception as exc:
        return f"I could not open the passphrase prompt: {exc}"
    return ("The passphrase prompt is on your screen. Type it there - I will not see "
            "it. Once it is set, I can arm the perimeter.")


def passphrase_status(parameters: dict, player=None) -> str:
    state = passphrase_vault.status()
    if not state.get("configured"):
        return ("No perimeter passphrase is set. Without one, a camera fault would "
                "leave no way in, so the perimeter will not arm.")
    remaining = int(state.get("bypass_minutes") or 0)
    if remaining:
        return (f"A passphrase is set. A bypass is active for another {remaining} "
                "minutes.")
    return "A passphrase is set and no bypass is active."
