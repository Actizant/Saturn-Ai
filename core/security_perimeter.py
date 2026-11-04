"""The policy layer: the only thing in Saturn allowed to ask for a lock.

EVERYTHING HERE IS A DELIBERATE REFUSAL
    The spec says "a face was detected but it is not the owner, so lock". That
    would fire on a colleague leaning over your shoulder, on a photograph on the
    wall, on your own face at a new angle, and on the reflection in a window. A
    security system that interrupts you is one you turn off.

    So suspicion needs five independent facts, and the absence of one is enough
    to stop it:

      1. an arrival - not "a face is on screen", but "someone appeared after the
         desk was empty". That is the only moment the answer changes.
      2. two unverified readings at least six seconds apart. Never one frame.
      3. no owner sighting inside the grace window. If you were recognised ten
         minutes ago you are still the person at this desk.
      4. three consecutive healthy camera readings. A camera that cannot see is
         a fault, and a fault must never be mistaken for a stranger.
      5. no active bypass and no lock inside the cooldown. Otherwise a failing
         detector becomes a machine that locks every ten minutes.

    Even then, the lock needs all of: armed, not in dry-run, identity models
    present, an owner enrolled, and no fault. Evidence is captured on suspicion
    whether or not the lock is armed, because "it measured and logged" is
    useful; "it locked me out" is not.

WHY IT CANNOT LOCK WITHOUT YOU
    With no model files the sensor reports PRESENT_UNVERIFIED with reason
    IDENTITY_UNAVAILABLE, which blocks the lock. That is the whole reason this
    subsystem can ship before a single download: presence-only mode is honest
    about what it cannot see.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from typing import Callable, Optional

from core import passphrase_vault
from core.presence_watch import (
    ABSENT, FAULT, LOCKED, PRESENT_KNOWN, PRESENT_OWNER, PRESENT_UNVERIFIED,
    STARTING,
)

SETTINGS_KEY = "security_perimeter"

# Two readings must be this far apart before they count as evidence.
SAMPLE_GAP_SECONDS = 6.0

# How many consecutive healthy camera readings a lock decision requires.
HEALTHY_SAMPLES_NEEDED = 3

# How often the away-log interval is written while you are gone.
AWAY_NOTE_SECONDS = 30.0

DEFAULTS = {
    "enabled": True,
    "armed": False,
    # Try to arm at every start, without waiting to be asked.
    #
    # This is not the same permission as ``armed``. ``armed`` is a state that
    # survives a restart; a thread does not. Without this flag an owner who
    # armed the perimeter yesterday gets a process today that believes it is
    # armed and samples nothing, which is the worst of both worlds: a green
    # light over a disconnected sensor. So the flag means "make the state true
    # again at start", and :func:`start_at_boot` is what honours it.
    "arm_on_start": True,
    "dry_run": True,
    "sample_seconds": 3.0,
    "away_lock": "announce",          # off | announce | lock
    "away_seconds": 300.0,
    "grace_minutes": 10.0,
    "lock_cooldown_seconds": 600.0,
    "evidence_seconds": 10.0,
    "retention_mb": 500.0,
    "owner_name": "",
    "camera": 0,
    "confirm_before_lock": True,
}

AWAY_MODES = ("off", "announce", "lock")


# ── Settings ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        from memory.config_manager import get_setting

        stored = get_setting(SETTINGS_KEY, {}) or {}
        if not isinstance(stored, dict):
            stored = {}
    except Exception:
        stored = {}
    config = dict(DEFAULTS)
    for key, value in stored.items():
        if key in DEFAULTS and value is not None:
            config[key] = value
    return config


def save_config(patch: dict) -> dict:
    """Merge a patch into the stored dict and write the whole thing back.

    ``memory.config_manager.save_settings`` replaces top-level keys wholesale,
    so the perimeter must always write its complete dict rather than a fragment.
    The passphrase lives under its own key for exactly that reason.
    """
    config = load_config()
    config.update({k: v for k, v in (patch or {}).items() if k in DEFAULTS})
    try:
        from memory.config_manager import set_setting

        set_setting(SETTINGS_KEY, config)
    except Exception:
        pass
    return config


# ── Platform actions ────────────────────────────────────────────────────────

def lock_workstation() -> bool:
    """Lock Windows. Returns whether the call was made, not whether it worked."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.user32.LockWorkStation())
    except Exception:
        return False


def _default_evidence(sample: dict, seconds: float) -> dict:
    """Snapshot first, then a clip: the snapshot must not wait behind the video."""
    from core import media_capture

    out = {"photo": None, "video": None}
    try:
        photo = media_capture.take_photo(note="perimeter")
        out["photo"] = photo.get("path")
    except Exception as exc:
        out["photo_error"] = str(exc)
    if seconds and seconds > 0:
        try:
            clip = media_capture.record_video(seconds=seconds, note="perimeter")
            out["video"] = clip.get("path")
        except Exception as exc:
            out["video_error"] = str(exc)
    return out


# ── The policy ──────────────────────────────────────────────────────────────

class SecurityPerimeter:
    def __init__(self, watcher=None, hub=None, config: Optional[dict] = None,
                 lock: Optional[Callable[[], bool]] = None,
                 evidence: Optional[Callable[[dict, float], dict]] = None,
                 announce: Optional[Callable[[str, dict], None]] = None,
                 ask_passphrase: Optional[Callable[[str], bool]] = None,
                 context: Optional[Callable[[], dict]] = None,
                 clock: Optional[Callable[[], float]] = None):
        self.watcher = watcher
        self.hub = hub if hub is not None else getattr(watcher, "hub", None)
        self._config = dict(DEFAULTS)
        self._config.update(config or load_config())
        self._lock_fn = lock or lock_workstation
        self._evidence_fn = evidence or _default_evidence
        self._announce_fn = announce
        self._ask_passphrase = ask_passphrase
        self._context = context
        self._clock = clock or time.time

        self._mutex = threading.Lock()
        self._episode_start: Optional[float] = None
        self._first_unverified: Optional[float] = None
        self._streak = 0
        self._healthy_streak = 0
        self._verified_at = self._clock()
        self._last_lock_at = 0.0
        self._away_since: Optional[float] = None
        self._away_announced = False
        self._away_noted_at = 0.0
        self._fault = False
        self._fault_announced = False
        self._evidence_done = False
        self._acting = False
        self._last_verdict: dict = {"action": "none", "reason": "starting"}
        self._enrolled_cache: tuple = (0.0, [])
        self._last_evidence = ""
        if self.watcher is not None:
            self.watcher.owner_name = self._owner_name()

    # ── Configuration ───────────────────────────────────────────────────

    @property
    def config(self) -> dict:
        return dict(self._config)

    def configure(self, announce=None, ask_passphrase=None, context=None) -> None:
        """Late wiring, so the perimeter can be built before the HUD exists.

        Only the side-effect seams are settable here - the policy itself is fixed
        at construction so no caller can quietly loosen it at runtime.
        """
        if announce is not None:
            self._announce_fn = announce
        if ask_passphrase is not None:
            self._ask_passphrase = ask_passphrase
        if context is not None:
            self._context = context

    def update(self, patch: dict) -> dict:
        with self._mutex:
            self._config = save_config(patch)
        if self.watcher is not None:
            self.watcher.owner_name = self._owner_name()
        return self.config

    # ── Small helpers ───────────────────────────────────────────────────

    def _now(self) -> float:
        return float(self._clock())

    def _g(self, key: str, default=None):
        return self._config.get(key, DEFAULTS.get(key, default))

    def _enrolled(self) -> list:
        stamp, cached = self._enrolled_cache
        if self._now() - stamp < 60.0:
            return cached
        try:
            from core import face_id

            people = face_id.people()
        except Exception:
            people = []
        self._enrolled_cache = (self._now(), people)
        return people

    def _owner_name(self) -> str:
        people = self._enrolled()
        name = str(self._g("owner_name") or "").strip()
        if name and name.lower() in {person.lower() for person in people}:
            return name
        # A configured name that is not enrolled cannot ever be recognised, so
        # it is not the owner no matter what the settings say. A leftover
        # placeholder here used to block arming outright and, worse, made the
        # real owner read as merely "known" - so the perimeter never counted
        # them as present. Fall back to the only enrolled person if there is one.
        return people[0] if len(people) == 1 else ""

    def _reset_episode(self) -> None:
        self._episode_start = None
        self._first_unverified = None
        self._streak = 0
        self._evidence_done = False

    def _announce(self, kind: str, payload: Optional[dict] = None) -> None:
        if self._announce_fn is None:
            return
        try:
            self._announce_fn(kind, payload or {})
        except Exception:
            pass

    # ── The decision ────────────────────────────────────────────────────

    def on_sample(self, sample: dict) -> dict:
        """Judge one reading. Returns the verdict and performs any action."""
        verdict = {"action": "none", "reason": "", "state": str((sample or {}).get("state") or ""),
                   "at": self._now()}
        if not self._g("enabled", True):
            verdict["reason"] = "disabled"
            return self._remember(verdict)

        state = verdict["state"]
        if not sample or sample.get("skipped") or state in (STARTING, LOCKED, ""):
            with self._mutex:
                self._reset_episode()
                if state == LOCKED:
                    self._away_since = None
                    self._away_announced = False
                    self._fault = False
                    self._fault_announced = False
            verdict["reason"] = "not evaluated"
            return self._remember(verdict)

        healthy = bool((sample.get("hub") or {}).get("ok")) and not sample.get("faulted")
        with self._mutex:
            self._healthy_streak = self._healthy_streak + 1 if healthy else 0
            self._fault = not healthy

        if sample.get("faulted") or state == FAULT:
            return self._remember(self._on_fault(sample))

        with self._mutex:
            self._fault_announced = False

        if state == ABSENT:
            return self._remember(self._on_absent(sample, verdict))
        return self._remember(self._on_present(sample, verdict))

    def _remember(self, verdict: dict) -> dict:
        self._last_verdict = dict(verdict)
        return verdict

    def _on_fault(self, sample: dict) -> dict:
        # The sample's own reason is what actually went wrong (NO_FRAME,
        # CAPTURE_FAILED, LENS_COVERED). The hub's health reason is usually just
        # "idle" - the state of a camera nobody is holding - and leading with it
        # produced alerts reading "the camera stopped working: idle", which tells
        # the owner nothing about the fault they need to fix.
        reason = str(sample.get("reason")
                     or (sample.get("hub") or {}).get("reason")
                     or "CAMERA_FAULT")
        verdict = {"action": "none", "reason": f"FAULT: {reason}", "state": FAULT,
                   "at": self._now(), "fault": True}
        with self._mutex:
            self._reset_episode()
            first = not self._fault_announced
            self._fault_announced = True
        if first:
            # This is the whole fail-safe in one line: a camera that cannot see
            # can never cause a lock. It can only tell you it is blind.
            verdict["action"] = "announce"
            self._announce("camera_fault", {"reason": reason})
            self._alert("camera_fault", f"The camera stopped working: {reason}", None,
                        verdict)
        return verdict

    def _on_absent(self, sample: dict, verdict: dict) -> dict:
        now = self._now()
        with self._mutex:
            self._reset_episode()
            if self._away_since is None:
                self._away_since = now
                self._away_announced = False
            away_for = now - self._away_since
            threshold = float(self._g("away_seconds", 300.0) or 0)
            mode = str(self._g("away_lock", "announce") or "off").lower()
            should_note = (now - self._away_noted_at) >= AWAY_NOTE_SECONDS
            if should_note:
                self._away_noted_at = now
            first_announce = not self._away_announced and away_for >= threshold
            if first_announce:
                self._away_announced = True

        if should_note:
            self._note_away(sample, away_for)

        verdict["away_seconds"] = round(away_for, 1)
        if away_for < threshold or mode == "off":
            return verdict

        if mode == "lock":
            allowed, blockers = self._can_lock(now, need_identity=False)
            if allowed:
                self._perform_lock("away_lock", sample, verdict)
            else:
                verdict["reason"] = "away: " + ", ".join(blockers)
                if first_announce:
                    verdict["action"] = "announce"
                    self._announce("away_unlocked", {"minutes": int(away_for // 60)})
        elif first_announce:
            verdict["action"] = "announce"
            verdict["reason"] = "away"
            self._announce("away_unlocked", {"minutes": int(away_for // 60)})
        return verdict

    def _on_present(self, sample: dict, verdict: dict) -> dict:
        now = self._now()
        state = verdict["state"]
        with self._mutex:
            self._away_since = None
            self._away_announced = False

        if state == PRESENT_OWNER:
            with self._mutex:
                self._verified_at = now
                self._reset_episode()
            verdict["reason"] = "owner seen"
            return verdict

        if state == PRESENT_KNOWN:
            # A person you enrolled is not an intruder, and neither is anyone
            # standing behind you while you are recognised.
            with self._mutex:
                self._reset_episode()
            verdict["reason"] = "known person"
            return verdict

        if state != PRESENT_UNVERIFIED:
            return verdict

        arrival = bool(sample.get("arrival"))
        with self._mutex:
            if arrival:
                self._episode_start = now
                self._first_unverified = now
                self._streak = 1
                self._evidence_done = False
            elif self._episode_start is not None:
                self._streak += 1
            else:
                verdict["reason"] = "no arrival"
                return verdict
            streak = self._streak
            since_first = now - (self._first_unverified or now)
            verified_ago = now - self._verified_at
            healthy = self._healthy_streak

        identity = sample.get("identity") or {}
        identity_reason = str(identity.get("reason") or "")
        identity_available = bool(sample.get("identity_available"))

        suspicion_blockers = []
        if streak < 2:
            suspicion_blockers.append("one reading")
        if since_first < SAMPLE_GAP_SECONDS:
            suspicion_blockers.append("too soon")
        if verified_ago < float(self._g("grace_minutes", 10.0)) * 60.0:
            suspicion_blockers.append("owner seen recently")
        if healthy < HEALTHY_SAMPLES_NEEDED:
            suspicion_blockers.append("camera unproven")

        if suspicion_blockers:
            verdict["reason"] = "unverified: " + ", ".join(suspicion_blockers)
            verdict["unverified"] = True
            return verdict

        # Suspicion is established. Evidence first, decision second: a lock that
        # happens before the camera is recording leaves nothing behind.
        verdict["unverified"] = True
        verdict["reason"] = identity_reason or "unverified arrival"
        with self._mutex:
            already = self._evidence_done
            self._evidence_done = True
        if not already:
            self._capture_evidence(sample, verdict)

        allowed, blockers = self._can_lock(now, need_identity=True,
                                           identity_available=identity_available)
        verdict["blocked_by"] = blockers

        if allowed and self._g("confirm_before_lock", True) and not self._challenge_available():
            # The valve that makes an unrecognised arrival safe to act on is
            # missing: the interface never handed us a prompt, or no passphrase
            # is stored for it to check. Falling through to the lock here would
            # be the silent degradation this design exists to prevent, so hold
            # instead - and say why, rather than looking merely unarmed.
            verdict["reason"] = "confirmation_unavailable"
            verdict["blocked_by"] = list(blockers) + ["no way to ask for a passphrase"]
            self._announce("confirmation_unavailable",
                           {"blocked_by": verdict["blocked_by"]})
            return verdict

        if identity_reason == "INFERENCE_TIMEOUT":
            # The machine is stuck, not the face. A human can still answer.
            if self._challenge("The camera check is taking too long."):
                verdict["action"] = "bypass"
                return verdict
            verdict["reason"] = "INFERENCE_TIMEOUT"
            if allowed:
                self._perform_lock("inference_timeout", sample, verdict)
            return verdict

        if allowed and self._g("confirm_before_lock", True):
            # A face that cannot be matched is genuinely ambiguous: "not the
            # owner" and "the owner in worse light" are the same answer to this
            # comparison. Locking on that guess costs the owner their session,
            # so ask first. The evidence above is already written either way,
            # so a stranger is photographed before they ever see the prompt.
            if self._challenge("I do not recognise the person at this desk."):
                verdict["action"] = "bypass"
                return verdict

        if allowed:
            self._perform_lock("unverified_arrival", sample, verdict)
        else:
            verdict["action"] = "evidence" if not already else "none"
            if not blockers:
                blockers = ["not permitted"]
            self._announce("unverified_person",
                           {"reason": identity_reason, "blocked": blockers})
        return verdict

    # ── Permission to lock ──────────────────────────────────────────────

    def _can_lock(self, now: float, need_identity: bool = True,
                  identity_available: Optional[bool] = None) -> tuple:
        blockers: list[str] = []
        if self._fault:
            blockers.append("camera fault")
        if not self._g("armed", False):
            blockers.append("not armed")
        if self._g("dry_run", True):
            blockers.append("dry run")
        if passphrase_vault.bypass_active():
            blockers.append("bypass active")
        cooldown = float(self._g("lock_cooldown_seconds", 600.0) or 0)
        if self._last_lock_at and (now - self._last_lock_at) < cooldown:
            blockers.append("cooldown")
        if not self._enrolled():
            blockers.append("nobody enrolled")
        if need_identity:
            if identity_available is None:
                identity_available = False
            if not identity_available:
                blockers.append("identity unavailable")
            elif not self._owner_name():
                blockers.append("no owner configured")
        return (not blockers), blockers

    def _perform_lock(self, reason: str, sample: dict, verdict: dict) -> None:
        # A dry run must mean no lock, checked here at the point of no return
        # rather than only in the callers: this is the last function before an
        # action the user cannot undo from inside the app.
        if self._g("dry_run", True):
            verdict["action"] = "none"
            verdict["reason"] = "dry run"
            return
        # The heartbeat check before any severe action, once more, at the last
        # moment: the camera can fail between the reading and the decision.
        health = (sample.get("hub") or {}) if sample else {}
        if health and not health.get("ok", True):
            verdict["action"] = "none"
            verdict["reason"] = "camera failed before lock"
            self._alert("lock_aborted", "The camera failed just before locking; "
                                        "the workstation was left unlocked.", None,
                        verdict)
            return
        if passphrase_vault.bypass_active():
            verdict["action"] = "none"
            verdict["reason"] = "bypass granted"
            return
        with self._mutex:
            if self._acting:
                return
            self._acting = True
        try:
            locked = False
            try:
                locked = bool(self._lock_fn())
            except Exception as exc:
                verdict["reason"] = f"lock failed: {exc}"
            with self._mutex:
                self._last_lock_at = self._now()
                self._reset_episode()
            verdict["action"] = "lock" if locked else "announce"
            verdict["reason"] = reason
            if locked:
                self._alert("workstation_locked",
                            f"Workstation locked ({reason}) because an unverified "
                            f"person arrived while you were away.", self._pending_evidence())
                self._announce("workstation_locked", {"reason": reason})
            else:
                self._announce("lock_failed", {"reason": reason})
        finally:
            with self._mutex:
                self._acting = False

    # ── Side effects ────────────────────────────────────────────────────

    def _capture_evidence(self, sample: dict, verdict: Optional[dict] = None) -> dict:
        from core import media_capture

        seconds = float(self._g("evidence_seconds", 10.0) or 0)
        record = {}
        try:
            record = self._evidence_fn(sample, seconds) or {}
        except Exception as exc:
            record = {"error": str(exc)}
        path = record.get("photo") or record.get("video")
        self._last_evidence = path
        self._alert("evidence_captured",
                    "Captured evidence of an unverified person at the desk.",
                    path, verdict)
        try:
            media_capture.prune(float(self._g("retention_mb", 500.0) or 500.0))
        except Exception:
            pass
        return record

    def _pending_evidence(self) -> str:
        return str(getattr(self, "_last_evidence", "") or "")

    def _alert(self, kind: str, text: str, evidence: Optional[str],
               verdict: Optional[dict] = None) -> None:
        # ``self._last_verdict`` is the *previous* reading, so an alert written
        # before the new verdict is stored described an older moment - a camera
        # fault was filed with the state "owner seen" from a minute earlier.
        try:
            from core import security_alerts

            security_alerts.record(kind, text, evidence,
                                   verdict if verdict is not None else self._last_verdict)
        except Exception:
            pass

    def _note_away(self, sample: dict, away_for: float) -> None:
        # The sensor already knows the window title, the idle clock and how far
        # the pointer travelled; the presence sample does not carry those, so
        # they are merged in here rather than polled a second time.
        merged = {}
        if self._context is not None:
            try:
                merged.update(self._context() or {})
            except Exception:
                pass
        merged.update({k: v for k, v in (sample or {}).items() if v is not None})
        try:
            from core import security_alerts

            security_alerts.note_away(merged, away_for)
        except Exception:
            pass

    def _challenge_available(self) -> bool:
        """Whether a human could be asked right now.

        Kept separate from ``_challenge`` because "nobody can be asked" and "the
        answer was wrong" collapse to the same False, and they must not lead to
        the same action: one is a reason to hold, the other a reason to act.
        """
        if self._ask_passphrase is None:
            return False
        return bool(passphrase_vault.configured())

    def _challenge(self, reason: str) -> bool:
        """Ask a human for the passphrase. True only if they answered rightly."""
        if not self._challenge_available():
            return False
        try:
            ok = bool(self._ask_passphrase(reason))
        except Exception:
            ok = False
        if ok:
            passphrase_vault.grant_bypass(passphrase_vault.DEFAULT_BYPASS_HOURS,
                                           reason="verified by passphrase")
            self._alert("bypass_granted",
                        "Passphrase accepted; the perimeter is paused for 24 hours.", None)
            self._announce("bypass_granted",
                           {"hours": passphrase_vault.DEFAULT_BYPASS_HOURS})
        return ok

    # ── Arming ──────────────────────────────────────────────────────────

    def arm(self, owner: str = "", confirm_selftest: bool = True) -> dict:
        """Turn on real verdicts, but only once everything needed is present."""
        problems: list[str] = []
        try:
            from core import face_id
        except Exception as exc:
            return {"ok": False, "error": "FACE_MODULE_UNAVAILABLE", "message": str(exc)}

        info = face_id.available()
        people = face_id.people()
        wanted = str(owner or self._owner_name() or "").strip()
        if not people:
            problems.append(
                "Nobody is enrolled, so there is no owner to recognise. Use "
                "remember_face first." + (f" {info.get('hint')}" if info.get("hint") else "")
            )
        elif not info.get("identity"):
            problems.append(info.get("hint") or "The face models are missing.")
        elif not wanted:
            problems.append("More than one person is enrolled; say which one is the owner.")
        elif wanted.lower() not in {name.lower() for name in people}:
            problems.append(f"'{wanted}' is not enrolled. Enrolled: {', '.join(people)}.")

        if not passphrase_vault.configured():
            problems.append("No passphrase is set, so a camera fault would leave no way in.")

        hub = self.hub or getattr(self.watcher, "hub", None)
        if hub is not None and confirm_selftest:
            probe = hub.probe(reset=True)
            if not probe.get("healthy"):
                problems.append(f"The camera self-test failed: {probe.get('reason')}.")

        if problems:
            return {"ok": False, "error": "NOT_READY", "problems": problems,
                    "message": "I have not armed the perimeter. " + " ".join(problems)}

        self.update({"armed": True, "dry_run": False, "owner_name": wanted})
        return {"ok": True, "armed": True, "owner": wanted,
                "message": (f"Perimeter armed for {wanted}. The workstation will lock "
                            "only if an unverified person arrives after you leave, and "
                            "never while the camera is faulty.")}

    def disarm(self) -> dict:
        self.update({"armed": False})
        with self._mutex:
            self._reset_episode()
        message = "Perimeter disarmed. It still watches and logs."
        if self._g("arm_on_start", True):
            # Saying only "disarmed" would be a half-truth: this setting brings
            # it back at the next start, and the owner needs to know that now
            # rather than discover it after a reboot.
            message += (" It is set to arm again at the next start; switch off "
                        "'arm at startup' to keep it down.")
        return {"ok": True, "armed": False, "message": message}

    # ── Reading it back ─────────────────────────────────────────────────

    def status(self) -> dict:
        hub = self.hub or getattr(self.watcher, "hub", None)
        try:
            from core import face_id

            info = face_id.available()
        except Exception:
            info = {"identity": False, "detector": None, "hint": "face module unavailable"}
        try:
            from core import security_alerts

            pending = len(security_alerts.pending())
        except Exception:
            pending = 0
        return {
            "enabled": bool(self._g("enabled", True)),
            "armed": bool(self._g("armed", False)),
            "dry_run": bool(self._g("dry_run", True)),
            "owner": self._owner_name(),
            "enrolled": self._enrolled(),
            "identity_available": bool(info.get("identity")),
            "detector": info.get("detector"),
            "hint": info.get("hint"),
            "away_lock": str(self._g("away_lock", "announce")),
            "away_seconds": float(self._g("away_seconds", 300.0)),
            "grace_minutes": float(self._g("grace_minutes", 10.0)),
            "confirm_before_lock": bool(self._g("confirm_before_lock", True)),
            "can_prompt": self._challenge_available(),
            "passphrase": passphrase_vault.status(),
            "camera": hub.health() if hub is not None else None,
            "last_verdict": dict(self._last_verdict),
            "pending_alerts": pending,
            "fault": self._fault,
        }


_perimeter: Optional[SecurityPerimeter] = None
_perimeter_lock = threading.Lock()


def get_perimeter() -> SecurityPerimeter:
    global _perimeter
    with _perimeter_lock:
        if _perimeter is None:
            from core.presence_watch import get_presence_watcher

            watcher = get_presence_watcher()
            watcher.interval = max(0.5, float(load_config().get("sample_seconds", 3.0)))
            _perimeter = SecurityPerimeter(watcher=watcher)
            watcher.set_callback(_perimeter.on_sample)
        return _perimeter


# ── Boot ────────────────────────────────────────────────────────────────────

_booted = False
_boot_lock = threading.Lock()


def start_at_boot(*, announce=None, ask_passphrase=None,
                  context=None) -> dict:
    """Bring the perimeter up for this process, once.

    WHY THIS EXISTS
        Arming is a *setting*; sampling is a *thread*. The setting was fine
        across restarts and the thread was not, because nothing ever built the
        perimeter except a tool call. So an owner who armed it yesterday ran
        today with ``armed: true`` on disk and no camera being read at all -
        the one failure mode a security feature must not have, since it looks
        exactly like a working one.

    WHAT IT WILL NOT DO
        * It does not start the sampler when nothing is armed and nothing was
          asked for. Opening the camera at every boot to police a perimeter
          nobody switched on would be a surveillance feature, not a guard.
        * It does not skip arming's preconditions. :meth:`arm` still demands an
          enrolled owner, the face models, a passphrase, and a healthy camera,
          and its refusal is reported here rather than swallowed - an arming
          that quietly did nothing is worse than one that says why.

    Runs on whatever thread calls it and never raises: this is a boot hook.
    """
    global _booted
    with _boot_lock:
        config = load_config()
        if not config.get("enabled", True):
            return {"ok": True, "started": False, "armed": False,
                    "reason": "the perimeter is switched off", "problems": []}
        if _booted:
            return {"ok": True, "started": True,
                    "armed": bool(config.get("armed")),
                    "reason": "already running", "problems": []}

        armed = bool(config.get("armed"))
        try:
            perimeter = get_perimeter()
            perimeter.configure(announce=announce, ask_passphrase=ask_passphrase,
                                context=context)
        except Exception as exc:
            return {"ok": False, "started": False, "armed": armed,
                    "reason": f"the perimeter could not be built: {exc}",
                    "problems": [str(exc)]}

        if not armed:
            if not config.get("arm_on_start", DEFAULTS["arm_on_start"]):
                return {"ok": True, "started": False, "armed": False,
                        "reason": ("not armed, and the perimeter is not set to "
                                   "arm at start"), "problems": []}
            outcome = perimeter.arm()
            armed = bool(outcome.get("armed"))
            if not outcome.get("ok") or not armed:
                problems = list(outcome.get("problems") or [])
                return {"ok": False, "started": False, "armed": False,
                        "reason": str(outcome.get("message")
                                      or "the perimeter would not arm"),
                        "problems": problems}

        watcher = getattr(perimeter, "watcher", None)
        if watcher is None or not hasattr(watcher, "start"):
            return {"ok": False, "started": False, "armed": armed,
                    "reason": "there is no presence watcher to start",
                    "problems": ["no watcher"]}
        try:
            started = bool(watcher.start())
        except Exception as exc:
            return {"ok": False, "started": False, "armed": armed,
                    "reason": f"the presence watcher would not start: {exc}",
                    "problems": [str(exc)]}
        _booted = started
        if not started:
            # A report whose ok is False must not carry a cheerful reason; this
            # is the one line the boot log shows.
            return {"ok": False, "started": False, "armed": armed,
                    "reason": "the presence watcher would not start",
                    "problems": ["watcher refused"]}
        return {"ok": True, "started": True, "armed": armed,
                "reason": "armed and watching" if armed else "watching",
                "problems": []}


def booted() -> bool:
    """Whether this process has successfully started the sampler."""
    return bool(_booted)
