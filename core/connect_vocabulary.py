"""The phone speaks its own words. This is the seam that turns them into ours.

WHY A TABLE AND NOT A BRANCH
    ``saturn_connect`` was written before the capability layer existed, so its
    command vocabulary grew on its own: ``launch_app``, ``volume_set``,
    ``flashlight_on``. The registry speaks ``app.launch``, ``level.set``,
    ``switch.on``. Both are honest names for the same acts; the problem was only
    that they were *different*, so a phone command and a lamp command took
    different roads - and only the lamp's road asked the trust gate.

    Two translation branches would have kept the vocabularies apart forever.
    One table, read in both directions, means a new capability for the phone is
    a line here, and ``tests/test_connect_vocabulary.py`` refuses a line that
    names something the registry does not know, or a registry capability that
    this file silently ignores.

WHAT A PHONE ACTUALLY HAS
    A switch: its flashlight. A level: its volume. A screen: it can open a URL
    and take typed text. Sensors: battery and device info. That is the whole of
    it, and saying so is more useful than pretending the phone is a printer.

WHAT IS DELIBERATELY MISSING
    ``unlock_phone`` is not mapped, and must not be. It needs a PIN, and a PIN
    that has travelled through a language model is no longer a secret. Unlocking
    stays on the road where the owner types it, which is also the road that no
    model can call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

# ── The table ───────────────────────────────────────────────────────────────

#: registry capability -> the word the phone's command handler answers to
CAPABILITY_TO_ACTION: Dict[str, str] = {
    # ``switch.on`` on a phone is its flashlight: the only switch it has.
    "switch.on": "flashlight_on",
    "switch.off": "flashlight_off",
    # ``level`` on a phone is volume. Both sides mean percent, 0-100.
    "level.set": "volume_set",
    "level.get": "volume_get",
    "app.launch": "launch_app",
    "display.show": "open_url",
    "text.input": "ui_type",
}

#: ``sensor.read`` is one capability with many names, so it needs its own column
SENSOR_ACTIONS: Dict[str, str] = {
    "battery": "get_battery",
    "device": "get_device_info",
}

#: registry argument -> phone argument. The reverse is built from this below,
#: so the two directions cannot drift apart.
PARAM_NAMES: Dict[str, Dict[str, str]] = {
    "app.launch": {"package": "app_name"},
    "display.show": {"content": "url"},
    "level.set": {"level": "value"},
    "text.input": {"text": "text"},
}

#: phone action -> registry capability, derived rather than retyped
ACTION_TO_CAPABILITY: Dict[str, str] = {
    action: capability for capability, action in CAPABILITY_TO_ACTION.items()
}

#: phone argument -> registry argument
ACTION_PARAMS: Dict[str, Dict[str, str]] = {
    capability: {phone: own for own, phone in names.items()}
    for capability, names in PARAM_NAMES.items()
}

# ── What cannot be said, and why ────────────────────────────────────────────

#: capabilities a phone genuinely cannot express, with the reason a person
#: would accept. Anything in the registry must appear in this table or in the
#: mapping above; the drift guard in the tests holds that line.
UNEXPRESSED_CAPABILITIES: Dict[str, str] = {
    "switch.toggle": "the phone has no way to flip its flashlight blind",
    "switch.state": "the phone agent does not report flashlight state",
    "temperature.get": "a phone has no thermometer to read",
    "temperature.set": "a phone has no thermostat to set",
    "media.play": "the phone agent has no media action yet",
    "media.pause": "the phone agent has no media action yet",
    "media.stop": "the phone agent has no media action yet",
    "media.next": "the phone agent has no media action yet",
    "media.previous": "the phone agent has no media action yet",
    "job.start": "a phone does not run Saturn's long jobs",
    "job.pause": "a phone does not run Saturn's long jobs",
    "job.cancel": "a phone does not run Saturn's long jobs",
    "job.status": "a phone does not run Saturn's long jobs",
    "motion.move": "a phone moves by gesture, not by axis and distance",
    "motion.home": "a phone has no home position",
    "motion.stop": "a phone has no motion to stop",
    "motion.state": "a phone reports no motion state",
    "lock.lock": "the phone agent only unlocks, and that needs the PIN",
    "lock.unlock": "unlocking needs the PIN, which must not travel through the model",
    "lock.state": "the phone agent does not report lock state",
    "key.press": "the phone types text; it has no named key to press",
    "screen.capture": "the phone agent dumps UI nodes, not pixels",
    "notify.send": "the phone agent has no notification action yet",
}

#: phone actions that have no capability, with the reason. A row here is not
#: a failure - it is the sentence Saturn says instead of guessing.
UNEXPRESSED_ACTIONS: Dict[str, str] = {
    "unlock_phone": "unlocking needs the PIN, which must not travel through the model",
    "ui_dump": "the UI node dump is debugging output, not an action",
    "ui_tap": "a tap needs coordinates; no capability describes a point on a screen",
    "ui_swipe": "a swipe needs a path; no capability describes one",
    "file_list": "the phone's files are not exposed as a capability yet",
    "file_read": "the phone's files are not exposed as a capability yet",
    "file_write": "the phone's files are not exposed as a capability yet",
    "file_delete": "the phone's files are not exposed as a capability yet",
}


def capabilities() -> Tuple[str, ...]:
    """Every registry capability a paired phone can answer, in registry grammar."""
    return tuple(sorted(set(CAPABILITY_TO_ACTION) | {"sensor.read"}))


# ── A translation ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Translation:
    """A request said in the other vocabulary, or the reason it cannot be.

    ``target`` is an action when going towards the phone and a capability when
    coming back, because that is what a translation is: the same sentence in
    the other language.
    """

    target: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    problem: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.target) and not self.problem


def _normalise(capability_id: str) -> str:
    return ".".join(str(capability_id or "").strip().lower().split())


def _pick(params: Dict[str, Any], names: Iterable[str]) -> Any:
    """The first of ``names`` that carries a real value."""
    for name in names:
        if name in params and params[name] not in (None, ""):
            return params[name]
    return None


def _coerce(name: str, value: Any) -> Any:
    """A level that arrives as "40" is still forty."""
    if name in ("level", "celsius"):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return value
    return value


def _carry(supplied: Dict[str, Any], known: Dict[str, str]) -> Dict[str, Any]:
    """Rename what the table knows and carry the rest untouched.

    Passing the unknown keys through is what lets a phone action gain a
    parameter without this file learning about it - the alternative is a table
    that quietly drops the argument somebody just asked for.
    """
    translated: Dict[str, Any] = {}
    for own, other in known.items():
        value = _pick(supplied, (own, other))
        if value is not None:
            translated[other] = _coerce(own, value)
    spoken = set(known) | set(known.values())
    for key, value in supplied.items():
        if key not in spoken and key not in translated:
            translated[key] = value
    return translated


# ── One direction: capability -> what to send the phone ─────────────────────

def to_action(capability_id: str, params: Optional[Dict[str, Any]] = None) -> Translation:
    """Say a registry capability in the phone's words."""
    capability = _normalise(capability_id)
    supplied = dict(params or {})

    if capability == "sensor.read":
        name = str(_pick(supplied, ("name", "sensor", "what")) or "").strip().lower()
        name = "device" if name == "info" else name
        if not name:
            return Translation(problem=(
                "sensor.read on a phone needs a name: "
                f"{', '.join(sorted(SENSOR_ACTIONS))}."))
        action = SENSOR_ACTIONS.get(name)
        if action is None:
            return Translation(problem=(
                f"a phone has no '{name}' sensor. It can read: "
                f"{', '.join(sorted(SENSOR_ACTIONS))}."))
        return Translation(target=action)

    action = CAPABILITY_TO_ACTION.get(capability)
    if action is None:
        reason = UNEXPRESSED_CAPABILITIES.get(capability)
        if reason:
            return Translation(problem=f"a phone cannot {capability}: {reason}.")
        return Translation(problem=f"a phone has no way to {capability}.")

    return Translation(target=action,
                       params=_carry(supplied, PARAM_NAMES.get(capability) or {}))


# ── The other direction: the phone's action -> a capability ─────────────────

def to_capability(action: str, params: Optional[Dict[str, Any]] = None) -> Translation:
    """Say one of the phone's actions as a registry capability."""
    word = str(action or "").strip().lower()
    supplied = dict(params or {})

    for name, phone_action in SENSOR_ACTIONS.items():
        if phone_action == word:
            return Translation(target="sensor.read", params={"name": name})

    capability = ACTION_TO_CAPABILITY.get(word)
    if capability is None:
        reason = UNEXPRESSED_ACTIONS.get(word)
        if reason:
            return Translation(problem=f"'{word}' has no capability: {reason}.")
        return Translation(problem=f"I don't know an action called '{word}'.")

    return Translation(target=capability,
                       params=_carry(supplied, ACTION_PARAMS.get(capability) or {}))


def describe() -> str:
    """The mapping as two readable lists, for a tool description."""
    towards = "\n".join(
        f"- {capability} -> {action}" for capability, action in sorted(CAPABILITY_TO_ACTION.items())
    )
    sensors = "\n".join(
        f"- sensor.read(name={name}) -> {action}"
        for name, action in sorted(SENSOR_ACTIONS.items())
    )
    return f"{towards}\n{sensors}"
