"""Who is allowed to ask for what.

WHY THIS EXISTS
    Saturn is about to gain hands that reach past this keyboard: a printer, a
    door lock, someone else's phone. The moment a capability stops being "open
    an app" and becomes "start the print" or "unlock the door", the question
    *who is asking* stops being a nicety and becomes the precondition for
    building everything else. So there is exactly one place that answers it,
    and anything that moves matter asks here first.

ONE CAMERA, ONE SAMPLER, MANY CONSUMERS
    This module never opens a camera. :mod:`core.presence_watch` already takes
    one frame every three seconds and resolves identity once per arrival;
    asking it for its latest verdict is free. Two samplers fighting over one
    device is how a security system ends up blind, so the gate reads and does
    not look.

WHAT COUNTS AS EVIDENCE, AND WHAT DOES NOT
    * A recognised owner's face        -> OWNER, verified.
    * Another enrolled person          -> GUEST, verified.
    * A face nobody recognises         -> UNKNOWN. The whole point of enrolling
      the owner is that "somebody is at the desk" is not the same claim as
      "the owner is at the desk".
    * An empty frame while armed       -> UNKNOWN. Someone is talking, but the
      person the camera is pointed at is not the person talking.
    * Camera absent, unarmed, faulted  -> OWNER, *assumed*, and flagged as such.
      A single-user desktop with no camera must never lock its owner out of his
      own assistant; the assumption is recorded so a caller can raise the bar
      when the act is irreversible.

TWO KINDS OF "NO"
    ``needs_confirm``   a spoken yes finishes it. Reversible, cheap, ordinary.
    ``needs_passphrase`` the camera never saw the owner, so a secret is
                        required. This is the only path for locks, doors and
                        anything that arms or disarms protection.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Deque, Dict, List, Optional, Tuple

# ── Trust levels ────────────────────────────────────────────────────────────

class Trust(IntEnum):
    """Ordered, so "at least this much" is a comparison and not a table."""

    UNKNOWN = 0
    GUEST = 1
    OWNER = 2

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.name


# ── Policy tiers ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    min_trust: Trust
    confirm: bool = False
    # ``secret`` means: a face must have said "owner", otherwise the passphrase.
    secret: bool = False
    summary: str = ""
    note: str = ""


READ_ONLY = Rule(Trust.UNKNOWN, False, summary="read that")
COMFORT = Rule(Trust.GUEST, False, summary="do that")
COSTLY = Rule(Trust.OWNER, True, summary="do that")
SECRET = Rule(Trust.OWNER, True, secret=True, summary="do that")

# Longest prefix wins, so the order here only matters for readability.
_RULES: Tuple[Tuple[str, Rule], ...] = (
    # ── reading: harmless for anyone in the room ───────────────────────────
    # Longest prefix wins, so the dotted read forms beat their family rules.
    ("switch.state", READ_ONLY),
    ("level.get", READ_ONLY),
    ("temperature.get", READ_ONLY),
    ("motion.state", READ_ONLY),
    ("lock.state", READ_ONLY),
    ("job.status", READ_ONLY),
    ("screen.", READ_ONLY),
    ("sensor.", READ_ONLY),
    ("status.", READ_ONLY),
    ("list.", READ_ONLY),
    ("get.", READ_ONLY),
    ("read.", READ_ONLY),
    ("info.", READ_ONLY),
    ("query.", READ_ONLY),
    ("device.list", READ_ONLY),
    ("capability.list", READ_ONLY),
    ("task.list", READ_ONLY),
    # Mail is addressed to a person, so it is stricter than the status reads
    # above: a verified guest in the room may ask, an unidentified voice may
    # not. It is still a read - nothing is sent, nothing is deleted.
    ("read.mail", Rule(Trust.GUEST, False, summary="read your mail")),

    # ── reversible control: a guest may turn on a light ─────────────────────
    ("switch.", COMFORT),
    ("level.", COMFORT),
    ("media.", COMFORT),
    ("display.", COMFORT),
    ("app.", COMFORT),
    ("window.", COMFORT),
    ("browser.", COMFORT),
    ("desktop.", COMFORT),
    ("temperature.set", COMFORT),
    ("motion.", COMFORT),
    ("timer.", COMFORT),
    ("reminder.", COMFORT),
    ("text.", COMFORT),
    ("key.", COMFORT),
    ("notify.", COMFORT),
    ("task.plan", COMFORT),
    ("task.run", COMFORT),
    ("lock.lock", COMFORT),
    ("job.cancel", COMFORT),
    ("job.pause", COMFORT),

    # A learned rule changes every later turn, so it is not a free act - but it
    # is reversible ("forget the rule about X" switches it off), and a guest who
    # lives here saying "we keep the kettle on the left" is the point of a shared
    # assistant. What is not allowed is learning it silently: the handler reads
    # the stored rule back out loud, every time.
    ("rule.write", Rule(Trust.GUEST, False, summary="learn that rule")),
    ("rule.delete", Rule(Trust.GUEST, False, summary="forget that rule")),

    # ── spends money or time, moves matter, cannot always be undone ─────────
    ("job.start", Rule(Trust.OWNER, True, summary="start that job")),
    ("file.delete", Rule(Trust.OWNER, True, summary="delete files")),
    ("file.move", Rule(Trust.OWNER, True, summary="move files")),
    ("file.write", Rule(Trust.OWNER, True, summary="write files")),
    ("message.send", Rule(Trust.OWNER, True, summary="send that message")),
    ("money.", Rule(Trust.OWNER, True, summary="spend money")),
    ("purchase.", Rule(Trust.OWNER, True, summary="buy that")),
    ("system.power", Rule(Trust.OWNER, True, summary="change the system power")),
    ("system.install", Rule(Trust.OWNER, True, summary="install software")),
    # Ending a process costs whatever was unsaved inside it, and a guest in the
    # room has no way to know what that is. Owner only.
    ("system.kill", Rule(Trust.OWNER, True, summary="end that program")),
    ("app.uninstall", Rule(Trust.OWNER, True, summary="uninstall that")),

    # ── secrets: never on an assumption ────────────────────────────────────
    ("lock.unlock", Rule(Trust.OWNER, True, secret=True, summary="unlock")),
    ("security.disarm", Rule(Trust.OWNER, True, secret=True, summary="disarm security")),
    ("security.arm", Rule(Trust.OWNER, True, secret=True, summary="arm security")),
    ("door.", Rule(Trust.OWNER, True, secret=True, summary="open that")),
    ("device.pair", Rule(Trust.OWNER, True, secret=True, summary="pair a new device")),
    ("capability.grant", Rule(Trust.OWNER, True, secret=True, summary="grant a permission")),
    ("credential.", Rule(Trust.OWNER, True, secret=True, summary="touch credentials")),
)

# Sorted once, longest first: "job.cancel" must beat "job." if a bare "job."
# rule is ever added above it.
_RULES_BY_LENGTH: Tuple[Tuple[str, Rule], ...] = tuple(
    sorted(_RULES, key=lambda item: len(item[0]), reverse=True)
)

DEFAULT_RULE = Rule(Trust.GUEST, False, summary="do that", note="unlisted capability")

# An intent name is not a capability. Until the registry exists everywhere, this
# translates the router's vocabulary into the same dotted grammar so a single
# policy covers both entry points.
_INTENT_CAPABILITY: Dict[str, str] = {
    "open_app": "app.launch",
    "media_control": "media.play",
    "vision": "read.screen",
    "net_sentinel": "status.network",
    "game_sentinel": "status.game",
    "mobile_connect": "app.phone",
    "meeting_mode": "display.meeting",
    "flowstate": "timer.focus",
    "study_guard": "timer.focus",
    "omni_planner": "task.plan",
    "reverse_engineer": "read.code",
    "shopping": "purchase.retail",
    "space_cleaner": "file.delete.temp",
    "developer_agent": "file.write.repo",
    "presentation": "file.write.office",
    "spreadsheet": "file.write.office",
    "generate_image": "file.write.asset",
    "google_workspace": "read.mail",
    # Reading the machine's vitals is a read; dimming the screen is reversible
    # control a guest may ask for; killing is neither.
    "system_status": "read.system",
    "brightness_control": "display.brightness",
    "process_kill": "system.kill",
    # The patch log is a read. Applying, rolling back or provoking a patch all
    # write source code, so all three share the repo-write capability.
    "self_heal_report": "read.patches",
    "self_heal_repair": "file.write.repo",
    "learn_rule": "rule.write",
    "forget_rule": "rule.delete",
    "rule_book": "read.rules",
}


# ── Evidence and decisions ──────────────────────────────────────────────────

@dataclass(frozen=True)
class Evidence:
    """The best answer available right now to "who is at the desk"."""

    trust: Trust = Trust.OWNER
    verified: bool = False
    source: str = "assumed"          # face | assumed | perimeter_off | fault | manual
    state: str = ""                  # raw presence state, for the log
    name: str = ""
    reason: str = "no biometric signal"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "trust": str(self.trust),
            "verified": self.verified,
            "source": self.source,
            "state": self.state,
            "name": self.name,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Decision:
    allowed: bool
    capability: str
    trust: Trust
    verified: bool
    needs_confirm: bool = False
    needs_passphrase: bool = False
    reason: str = ""
    spoken: str = ""
    policy_allowed: bool = True
    enforced: bool = True
    rule: str = ""
    source: str = ""
    at: float = field(default_factory=time.time)

    @property
    def blocked(self) -> bool:
        return not self.allowed

    @property
    def denied(self) -> bool:
        """A refusal of trust, not a request for one more word.

        Callers that have no way to remember a pending question should act on
        this rather than on ``allowed``: refusing to start work because the
        person is a stranger is safe and final, while refusing because a
        confirmation is outstanding needs somewhere to store the question.
        """
        return not self.policy_allowed

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "capability": self.capability,
            "trust": str(self.trust),
            "verified": self.verified,
            "needs_confirm": self.needs_confirm,
            "needs_passphrase": self.needs_passphrase,
            "reason": self.reason,
            "spoken": self.spoken,
            "policy_allowed": self.policy_allowed,
            "enforced": self.enforced,
            "rule": self.rule,
            "source": self.source,
        }


# ── Module state ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_override: Optional[Trust] = None
_log: Deque[Decision] = deque(maxlen=50)

MODE_KEY = "trust_gate_mode"
MODE_ENFORCE = "enforce"
MODE_OBSERVE = "observe"


# ── Reading the sensor ──────────────────────────────────────────────────────

def _perimeter_armed() -> Tuple[bool, bool]:
    """(armed, enabled). Anything unreadable counts as not armed."""
    try:
        from core.security_perimeter import load_config

        config = load_config()
        return bool(config.get("armed")), bool(config.get("enabled", True))
    except Exception:
        return False, False


def presence_evidence() -> Evidence:
    """Read the latest verdict. Never raises, never opens a device."""
    with _lock:
        override = _override
    if override is not None:
        return Evidence(trust=override, verified=False, source="manual",
                        reason="manually set")

    armed, enabled = _perimeter_armed()
    if not armed:
        return Evidence(trust=Trust.OWNER, verified=False,
                        source="perimeter_off",
                        reason="perimeter not armed" if enabled else "perimeter disabled")

    try:
        from core.presence_watch import (
            ABSENT, FAULT, LOCKED, PRESENT_KNOWN, PRESENT_OWNER,
            PRESENT_UNVERIFIED, STARTING, get_presence_watcher,
        )

        snapshot = get_presence_watcher().snapshot()
    except Exception as exc:
        return Evidence(trust=Trust.OWNER, verified=False, source="fault",
                        reason=f"presence unavailable: {exc}")

    state = str(snapshot.get("state") or STARTING)
    identity = snapshot.get("identity") or {}
    name = str(identity.get("name") or "")

    if state == PRESENT_OWNER:
        return Evidence(trust=Trust.OWNER, verified=True, source="face",
                        state=state, name=name, reason="owner's face")
    if state == PRESENT_KNOWN:
        return Evidence(trust=Trust.GUEST, verified=True, source="face",
                        state=state, name=name, reason=f"known person ({name})")
    if state in (PRESENT_UNVERIFIED, ABSENT):
        return Evidence(trust=Trust.UNKNOWN, verified=True, source="face",
                        state=state, name=name,
                        reason=("unrecognised face" if state == PRESENT_UNVERIFIED
                                else "nobody in frame"))
    # FAULT / LOCKED / STARTING: the sensor cannot answer. Assume the owner so
    # the assistant stays usable, but never claim the answer was verified.
    return Evidence(trust=Trust.OWNER, verified=False, source="fault",
                    state=state, name=name,
                    reason=str(snapshot.get("reason") or state.lower()))


# ── Policy ──────────────────────────────────────────────────────────────────

def canonical(capability: str) -> str:
    """Map a router intent onto the dotted capability grammar."""
    text = str(capability or "").strip().lower()
    return _INTENT_CAPABILITY.get(text, text)


def rule_for(capability: str) -> Tuple[str, Rule]:
    """Longest matching prefix, else the permissive default."""
    cap = canonical(capability)
    for prefix, rule in _RULES_BY_LENGTH:
        if cap.startswith(prefix):
            return prefix, rule
    return "", DEFAULT_RULE


def _owner_name() -> str:
    try:
        from core.identity import identity

        name = identity.get_owner_name()
        return str(name or "").strip() or "the owner"
    except Exception:
        return "the owner"


def _summary(capability: str, rule: Rule) -> str:
    if rule.summary and rule.summary != "do that":
        return rule.summary
    return canonical(capability).replace(".", " ").strip() or "do that"


def mode() -> str:
    try:
        from memory.config_manager import get_setting

        value = str(get_setting(MODE_KEY, MODE_ENFORCE) or MODE_ENFORCE).strip().lower()
    except Exception:
        value = MODE_ENFORCE
    return value if value in (MODE_ENFORCE, MODE_OBSERVE) else MODE_ENFORCE


# ── The one public question ─────────────────────────────────────────────────

def evaluate(capability: str, *, confirm: bool = False, passphrase: bool = False,
             evidence: Optional[Evidence] = None,
             enforced: Optional[bool] = None) -> Decision:
    """Decide whether this capability may run right now, and say why.

    ``confirm`` and ``passphrase`` are the caller's account of what it has
    already collected in this turn, so a two-step flow (ask, then act) does not
    ask twice.
    """
    cap = canonical(capability) or "unknown"
    prefix, rule = rule_for(cap)
    ev = evidence or presence_evidence()

    policy_allowed = ev.trust >= rule.min_trust
    needs_passphrase = bool(rule.secret and not ev.verified and not passphrase)
    # A passphrase is a stronger credential than a spoken yes, so having given
    # it must not be followed by a second question about the same act.
    confirmed = bool(confirm or (rule.secret and passphrase))
    needs_confirm = bool(rule.confirm and not confirmed)

    allowed = policy_allowed and not needs_confirm and not needs_passphrase

    reason = ""
    spoken = ""
    summary = _summary(cap, rule)
    owner = _owner_name()

    if not policy_allowed:
        if ev.trust == Trust.UNKNOWN:
            reason = f"{cap}: unrecognised person ({ev.reason})"
            # Only claim the owner is the bar when the owner really is the
            # bar. A guest-level act is open to anyone recognised, so name the
            # owner as one of them instead of pretending it is owner-only.
            who = (f"Only {owner} can approve that."
                   if rule.min_trust >= Trust.OWNER
                   else f"Only {owner} or someone I know can approve that.")
            spoken = f"I don't recognise who's asking, so I won't {summary}. {who}"
        else:
            reason = f"{cap}: guest cannot {summary}"
            spoken = f"That's {owner}'s to approve, not mine to hand out."
    elif needs_passphrase:
        reason = f"{cap}: needs the passphrase (no verified owner face)"
        spoken = f"I didn't see {owner}'s face for that one. Say the passphrase."
    elif needs_confirm:
        reason = f"{cap}: needs a spoken yes"
        spoken = f"I can {summary}. Confirm and I'll go ahead."

    is_enforced = mode() == MODE_ENFORCE if enforced is None else bool(enforced)
    if not is_enforced:
        allowed = True

    decision = Decision(
        allowed=allowed,
        capability=cap,
        trust=ev.trust,
        verified=ev.verified,
        needs_confirm=needs_confirm,
        needs_passphrase=needs_passphrase,
        reason=reason,
        spoken=spoken,
        policy_allowed=policy_allowed,
        enforced=is_enforced,
        rule=prefix or "<default>",
        source=ev.source,
    )
    with _lock:
        _log.append(decision)
    return decision


def require(capability: str, **kwargs: Any) -> Decision:
    """Alias with the name callers read better: ``gate.require("job.start")``."""
    return evaluate(capability, **kwargs)


def check(capability: str, **kwargs: Any) -> bool:
    """Just the yes/no, for call sites that have nothing to say about a no."""
    return evaluate(capability, **kwargs).allowed


# ── Manual override (tests, and the owner's deliberate unlock) ──────────────

def set_override(trust: Optional[Trust]) -> None:
    global _override
    with _lock:
        _override = None if trust is None else Trust(trust)


def clear_override() -> None:
    set_override(None)


# ── Observability ───────────────────────────────────────────────────────────

def recent_decisions(limit: int = 10) -> List[Dict[str, Any]]:
    with _lock:
        items = list(_log)[-max(1, int(limit)):]
    return [item.as_dict() for item in items]


def status() -> Dict[str, Any]:
    """Everything a settings panel may show without leaking a secret."""
    evidence = presence_evidence()
    with _lock:
        recent = len(_log)
    return {
        "trust": str(evidence.trust),
        "verified": evidence.verified,
        "source": evidence.source,
        "state": evidence.state,
        "reason": evidence.reason,
        "mode": mode(),
        "owner": _owner_name(),
        "recent_decisions": recent,
    }
