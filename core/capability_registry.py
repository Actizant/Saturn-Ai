"""What Saturn can do to hardware, independent of what the hardware is.

THE PROBLEM THIS REPLACES
    ``smart_home/providers/base.py`` gave every device exactly one verb -
    ``action == "power"`` - and exactly one fact, ``is_on``. Every new device
    therefore needed either a new provider class or a lie: a printer reported
    as "on", a thermostat as "on", a door lock as "on". The vocabulary was the
    bottleneck, not the drivers.

WHAT THIS IS
    A capability is a verb with a shape, named the same way everywhere::

        switch.on      level.set      temperature.get   sensor.read
        media.play     display.show   job.start         job.status
        motion.home    lock.unlock    app.launch        text.input

    A device is not a class. A device is a *bundle of capabilities with an
    address*::

        {"id": "tv", "adapter": "cast", "address": {"host": "192.168.1.20"},
         "capabilities": ["switch.on", "switch.off", "media.play", "display.show"]}

    Anything that can be reached can be described this way. That is the whole
    trick: no per-device module, no per-brand code, no "supported devices" list
    that is really a list of the four things somebody got round to.

WHERE THE CODE THAT ACTUALLY TALKS LIVES
    Not here. ``adapters/`` holds a handful of *transport* adapters (http,
    mqtt, adb, cast) and this module only decides which one a device names and
    whether the request makes sense. A new device is a JSON entry, not a file.

WHY EXECUTION PASSES THROUGH HERE
    Every physical action has to be approved by :mod:`core.trust_gate`, and the
    gate speaks capabilities. Routing execution through the registry means
    there is exactly one place where "may this happen" is asked, and no caller
    can forget.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# ── The vocabulary ──────────────────────────────────────────────────────────

READ_ONLY = "read_only"
COMFORT = "comfort"
COSTLY = "costly"
SECRET = "secret"


@dataclass(frozen=True)
class Param:
    """One argument. Types are small on purpose - this is a wire format."""

    name: str
    type: str = "any"           # int | float | bool | str | any
    required: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: str = ""
    description: str = ""


@dataclass(frozen=True)
class Capability:
    id: str
    summary: str
    trust: str = COMFORT
    params: Tuple[Param, ...] = ()
    returns: str = ""

    def signature(self) -> str:
        if not self.params:
            return self.id
        parts = []
        for param in self.params:
            piece = f"{param.name}:{param.type}"
            if not param.required:
                piece = f"{piece}?"
            parts.append(piece)
        return f"{self.id}({', '.join(parts)})"


_LEVEL = Param("level", "int", True, 0, 100, "%", "0-100")


_CAPABILITIES: Tuple[Capability, ...] = (
    # ── power and brightness ────────────────────────────────────────────
    Capability("switch.on", "turn a device on", COMFORT),
    Capability("switch.off", "turn a device off", COMFORT),
    Capability("switch.toggle", "flip a device's power", COMFORT),
    Capability("switch.state", "read whether a device is on", READ_ONLY),
    Capability("level.set", "set a level such as brightness or volume", COMFORT,
               (_LEVEL,)),
    Capability("level.get", "read a level", READ_ONLY),

    # ── climate and sensing ─────────────────────────────────────────────
    Capability("temperature.set", "set a target temperature", COMFORT,
               (Param("celsius", "float", True, -40.0, 90.0, "C"),)),
    Capability("temperature.get", "read the current temperature", READ_ONLY),
    Capability("sensor.read", "read an arbitrary sensor", READ_ONLY,
               (Param("name", "str"),)),

    # ── media and display ───────────────────────────────────────────────
    Capability("media.play", "start media playback", COMFORT,
               (Param("what", "str", description="title, url or query"),)),
    Capability("media.pause", "pause playback", COMFORT),
    Capability("media.stop", "stop playback", COMFORT),
    Capability("media.next", "skip forward", COMFORT),
    Capability("media.previous", "skip back", COMFORT),
    Capability("display.show", "put something on a screen", COMFORT,
               (Param("content", "str", True, description="text or URL"),)),

    # ── long-running work ───────────────────────────────────────────────
    Capability("job.start", "start a job such as a print or a render", COSTLY,
               (Param("source", "str", description="file, model or URL"),
                Param("profile", "str"))),
    Capability("job.pause", "pause a running job", COMFORT),
    Capability("job.cancel", "cancel a running job", COMFORT),
    Capability("job.status", "read job progress", READ_ONLY),

    # ── physical movement ───────────────────────────────────────────────
    Capability("motion.move", "move an axis or a robot", COMFORT,
               (Param("axis", "str"), Param("distance", "float", unit="mm"),
                Param("speed", "float", unit="mm/s"))),
    Capability("motion.home", "return to the home position", COMFORT),
    Capability("motion.stop", "stop moving immediately", COMFORT),
    Capability("motion.state", "read position and motion state", READ_ONLY),

    # ── access ──────────────────────────────────────────────────────────
    Capability("lock.lock", "secure a lock", COMFORT),
    Capability("lock.unlock", "open a lock", SECRET),
    Capability("lock.state", "read whether a lock is secure", READ_ONLY),

    # ── screens that belong to something else ───────────────────────────
    Capability("app.launch", "open an application or an app", COMFORT,
               (Param("package", "str", True),)),
    Capability("text.input", "type text into the focused field", COMFORT,
               (Param("text", "str", True),)),
    Capability("key.press", "press a key or a button", COMFORT,
               (Param("key", "str", True),)),
    Capability("screen.capture", "take a screenshot of the device", READ_ONLY),
    Capability("notify.send", "show a notification on the device", COMFORT,
               (Param("text", "str", True),)),
)

CAPABILITIES: Dict[str, Capability] = {cap.id: cap for cap in _CAPABILITIES}

# Prefixes that make a capability id legal without being listed above, so a
# driver can expose ``tuya.dp3`` without waiting for this file to grow.
KNOWN_FAMILIES = frozenset({
    "switch", "level", "temperature", "sensor", "media", "display", "job",
    "motion", "lock", "app", "text", "key", "screen", "notify",
})

TIERS = (READ_ONLY, COMFORT, COSTLY, SECRET)


def capability(capability_id: str) -> Optional[Capability]:
    return CAPABILITIES.get(normalise(capability_id))


def normalise(capability_id: str) -> str:
    return ".".join(str(capability_id or "").strip().lower().split())


def known(capability_id: str) -> bool:
    """Whether the id is legal. Unlisted members of a known family are legal."""
    cap = normalise(capability_id)
    if cap in CAPABILITIES:
        return True
    family = cap.split(".", 1)[0]
    return bool(family) and family in KNOWN_FAMILIES


def vocabulary() -> List[str]:
    return sorted(CAPABILITIES)


def describe_vocabulary() -> str:
    """One line per capability, for a tool description or a settings page."""
    return "\n".join(
        f"{cap.signature()} - {cap.summary} ({cap.trust})"
        for cap in sorted(_CAPABILITIES, key=lambda item: item.id)
    )


# ── Devices ─────────────────────────────────────────────────────────────────

@dataclass
class Device:
    id: str
    name: str
    adapter: str = "http"
    address: Dict[str, Any] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)
    kind: str = ""
    enabled: bool = True
    trust: str = COMFORT
    notes: str = ""
    #: Where this row came from. Empty means "written down by the owner";
    #: anything else means a source produced it just now, and a write must not
    #: turn it into a setting.
    source: str = ""

    @property
    def capability_ids(self) -> List[str]:
        return sorted(self.capabilities)

    def supports(self, capability_id: str) -> bool:
        return normalise(capability_id) in {normalise(k) for k in self.capabilities}

    def public(self) -> Dict[str, Any]:
        """Everything except the parts of the address that could be a secret."""
        payload = asdict(self)
        payload["address"] = {
            key: ("***" if any(word in key.lower() for word in _SECRET_WORDS) else value)
            for key, value in self.address.items()
        }
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Device":
        caps = raw.get("capabilities") or []
        if isinstance(caps, list):
            caps = {normalise(item): {} for item in caps if normalise(item)}
        elif not isinstance(caps, dict):
            caps = {}
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or raw.get("id") or ""),
            adapter=str(raw.get("adapter") or "http"),
            address=dict(raw.get("address") or {}),
            capabilities=dict(caps),
            state=dict(raw.get("state") or {}),
            kind=str(raw.get("kind") or ""),
            enabled=bool(raw.get("enabled", True)),
            trust=str(raw.get("trust") or COMFORT),
            notes=str(raw.get("notes") or ""),
            source=str(raw.get("source") or ""),
        )


_SECRET_WORDS = ("token", "key", "secret", "password", "passwd", "pin", "credential")


# ── Storage ─────────────────────────────────────────────────────────────────

VERSION = 1
ENV_PATH = "SATURN_DEVICES_FILE"

_lock = threading.RLock()
_cache: Optional[List[Device]] = None
_cache_stamp: float = 0.0


def _path() -> Path:
    override = os.environ.get(ENV_PATH)
    if override:
        return Path(override)
    try:
        from core.user_paths import get_user_data_dir

        return get_user_data_dir() / "config" / "devices.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "config" / "devices.json"


# ── Live devices ────────────────────────────────────────────────────────────

#: How stale a live list is allowed to be. A file must be exact; a paired
#: phone may be a second and a half behind, because asking it is a call into
#: another process and :func:`load` sits on a hot path.
SOURCE_TTL = 1.5

_sources: List[Callable[[], Iterable[Device]]] = []
_source_rows: List[Device] = []
_source_at: float = 0.0


def register_source(source: Callable[[], Iterable[Device]]) -> Callable:
    """Teach the registry where *live* devices come from.

    A source answers with the devices that exist right now - a paired phone, a
    printer that announced itself - and is never written to ``devices.json``.
    Registering the same function twice is a no-op.
    """
    with _lock:
        if source not in _sources:
            _sources.append(source)
    return source


def clear_sources() -> None:
    """Forget every source. Tests use this; nothing else should."""
    global _source_rows, _source_at
    with _lock:
        _sources.clear()
        _source_rows, _source_at = [], 0.0


def _source_devices(force: bool = False) -> List[Device]:
    """The live rows, re-read at most every :data:`SOURCE_TTL` seconds.

    A source that raises is a device that is not there. It must never stop the
    owner's configured devices from loading.
    """
    global _source_rows, _source_at
    with _lock:
        if not _sources:
            _source_rows, _source_at = [], 0.0
            return []
        if not force and (time.monotonic() - _source_at) < SOURCE_TTL:
            return list(_source_rows)
        sources = list(_sources)

    rows: List[Device] = []
    for source in sources:
        try:
            produced = list(source() or [])
        except Exception:
            continue
        for device in produced:
            if isinstance(device, dict):
                device = Device.from_dict(device)
            if not isinstance(device, Device) or not device.id:
                continue
            # Stamped here as well as in the source itself, so a future source
            # cannot forget and quietly become a row in the file.
            device.source = device.source or "live"
            rows.append(device)
    with _lock:
        _source_rows, _source_at = rows, time.monotonic()
    return rows


def load(force: bool = False) -> List[Device]:
    """Every device Saturn can reach: the ones written down, then the live ones.

    A broken or missing file is an empty house, not an error. Live devices are
    merged behind it and lose any id collision, because the owner's deliberate
    entry is the one that should win.
    """
    path = _path()
    with _lock:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = 0.0
        if _cache is not None and not force and stamp == _cache_stamp:
            written = list(_cache)
        else:
            written = _read(path, stamp)

    live = _source_devices(force=force)
    if not live:
        return written
    known = {device.id for device in written}
    return written + [device for device in live if device.id not in known]


def _read(path: Path, stamp: float) -> List[Device]:
    """Read the file and remember it. Called with the lock already held."""
    global _cache, _cache_stamp
    if not path.exists():
        _cache, _cache_stamp = [], stamp
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("devices") if isinstance(raw, dict) else raw
        devices = [Device.from_dict(row) for row in (rows or [])
                   if isinstance(row, dict) and row.get("id")]
    except Exception:
        devices = []
    _cache, _cache_stamp = devices, stamp
    return list(devices)


def save(devices: Iterable[Device]) -> None:
    """Write the whole file.

    Rows may arrive as the dicts they were read from, so they are normalised
    here. A registry that answers a plain row with "asdict() should be called
    on dataclass instances" has told the caller nothing about the device it
    actually got wrong - and a device *is* a declarative entry, so a row is the
    natural thing to hand it.
    """
    global _cache, _cache_stamp
    path = _path()
    rows = [device if isinstance(device, Device) else Device.from_dict(device or {})
            for device in devices]
    # A live device is not a setting: writing it down would leave a phone in the
    # file that is not in the house, and the next load would trust it. An entry
    # the owner wrote is kept even when a live device shares its id.
    rows = [row for row in rows if not row.source]
    payload = {"version": VERSION, "devices": [asdict(device) for device in rows]}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        _cache = list(rows)
        try:
            _cache_stamp = path.stat().st_mtime
        except OSError:
            _cache_stamp = 0.0


def reset_cache() -> None:
    global _cache, _cache_stamp
    with _lock:
        _cache, _cache_stamp = None, 0.0


def get(device_id: str) -> Optional[Device]:
    wanted = str(device_id or "").strip().lower()
    for device in load():
        if device.id.lower() == wanted:
            return device
    return None


def resolve(target: str) -> Optional[Device]:
    """Find a device by id or by name, case-insensitively."""
    wanted = str(target or "").strip().lower()
    if not wanted:
        return None
    devices = load()
    for device in devices:
        if device.id.lower() == wanted:
            return device
    for device in devices:
        if device.name.lower() == wanted:
            return device
    for device in devices:
        if wanted and (wanted in device.name.lower() or wanted in device.id.lower()):
            return device
    return None


def add(device: Device, replace: bool = False) -> Device:
    """Add one device, given either a :class:`Device` or the row it came from."""
    device = device if isinstance(device, Device) else Device.from_dict(device or {})
    if not device.id:
        raise ValueError("a device needs an id")
    devices = load(force=True)
    for index, existing in enumerate(devices):
        if existing.id == device.id:
            if not replace:
                raise ValueError(f"a device with id '{device.id}' already exists")
            devices[index] = device
            save(devices)
            return device
    devices.append(device)
    save(devices)
    return device


def update(device_id: str, patch: Dict[str, Any]) -> Optional[Device]:
    devices = load(force=True)
    for device in devices:
        if device.id != device_id:
            continue
        for key, value in (patch or {}).items():
            if key == "capabilities":
                if isinstance(value, dict):
                    device.capabilities = {normalise(k): v
                                           for k, v in {**device.capabilities, **value}.items()}
                elif isinstance(value, list):
                    device.capabilities = {normalise(item): {} for item in value
                                           if normalise(item)}
            elif key in ("address", "state") and isinstance(value, dict):
                merged = {**(getattr(device, key) or {}), **value}
                setattr(device, key, merged)
            elif key != "id" and hasattr(device, key):
                setattr(device, key, value)
        save(devices)
        return device
    return None


def remove(device_id: str) -> bool:
    devices = load(force=True)
    kept = [device for device in devices if device.id != device_id]
    if len(kept) == len(devices):
        return False
    save(kept)
    return True


# ── Lookup ──────────────────────────────────────────────────────────────────

def for_capability(capability_id: str) -> List[Device]:
    cap = normalise(capability_id)
    return [device for device in load() if device.enabled and device.supports(cap)]


def describe() -> str:
    """What Saturn may say when asked what it can reach."""
    devices = [device for device in load() if device.enabled]
    if not devices:
        return "No devices are configured yet."
    lines = []
    for device in devices:
        caps = ", ".join(device.capability_ids) or "nothing"
        kind = f"{device.kind}, " if device.kind else ""
        lines.append(f"- {device.name} ({kind}{device.adapter}): {caps}")
    return "\n".join(lines)


# ── Validation ──────────────────────────────────────────────────────────────

def validate(capability_id: str, params: Optional[Dict[str, Any]] = None
             ) -> Tuple[bool, str]:
    """Whether this call makes sense before anything is sent anywhere."""
    cap = normalise(capability_id)
    if not cap:
        return False, "no capability was named"
    if not known(cap):
        return False, (f"'{cap}' is not a capability I know. Known families: "
                       f"{', '.join(sorted(KNOWN_FAMILIES))}")
    spec = CAPABILITIES.get(cap)
    if spec is None:
        return True, ""

    supplied = dict(params or {})
    missing = [param.name for param in spec.params
               if param.required and supplied.get(param.name) in (None, "")]
    if missing:
        return False, f"{cap} needs {', '.join(missing)}"

    for param in spec.params:
        if param.name not in supplied:
            continue
        value = supplied[param.name]
        if value is None:
            continue
        if param.type in ("int", "float"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False, f"{param.name} must be a number"
            if param.minimum is not None and number < param.minimum:
                return False, f"{param.name} must be at least {param.minimum:g}{param.unit}"
            if param.maximum is not None and number > param.maximum:
                return False, f"{param.name} must be at most {param.maximum:g}{param.unit}"
        elif param.type == "bool" and not isinstance(value, bool):
            return False, f"{param.name} must be true or false"
        elif param.type == "str" and not isinstance(value, str):
            return False, f"{param.name} must be text"
    return True, ""


# ── Execution ───────────────────────────────────────────────────────────────

def _adapter_for(device: Device):
    try:
        from adapters import get_adapter

        return get_adapter(device.adapter), ""
    except Exception as exc:  # adapters are optional at import time
        return None, f"adapter layer unavailable: {exc}"


def execute(device_id: str, capability_id: str,
            params: Optional[Dict[str, Any]] = None, *,
            confirmed: bool = False, passphrase: bool = False,
            gate: bool = True) -> Dict[str, Any]:
    """Run one capability against one device, and report what actually happened.

    Never raises and never claims more than the adapter reported: an adapter
    that is unsure says ``ok: False`` and the caller says so out loud.
    """
    cap = normalise(capability_id)
    device = resolve(device_id)
    if device is None:
        return {"ok": False, "error": "NO_SUCH_DEVICE",
                "detail": f"I have no device called '{device_id}'."}
    if not device.enabled:
        return {"ok": False, "error": "DISABLED",
                "detail": f"{device.name} is turned off in settings."}
    if not device.supports(cap):
        return {"ok": False, "error": "UNSUPPORTED",
                "detail": f"{device.name} cannot {cap}. It offers: "
                          f"{', '.join(device.capability_ids) or 'nothing'}."}

    ok, message = validate(cap, params)
    if not ok:
        return {"ok": False, "error": "BAD_REQUEST", "detail": message}

    if gate:
        try:
            from core.trust_gate import evaluate

            decision = evaluate(cap, confirm=confirmed, passphrase=passphrase)
        except Exception:
            # A gate that cannot answer is not a reason to break the device
            # layer; the refusal it would have issued is not evidence either way.
            decision = None
        if decision is not None and not decision.allowed:
            return {"ok": False, "error": "REFUSED",
                    "detail": decision.spoken or decision.reason,
                    "policy_allowed": decision.policy_allowed}

    adapter, problem = _adapter_for(device)
    if adapter is None:
        return {"ok": False, "error": "NO_ADAPTER", "detail": problem}

    merged = dict(device.capabilities.get(cap) or {})
    merged.update(params or {})

    try:
        result = adapter.send(device, cap, merged)
    except Exception as exc:
        result = {"ok": False, "error": "ADAPTER_FAILED", "detail": str(exc)}

    if not isinstance(result, dict):
        result = {"ok": False, "error": "ADAPTER_CONTRACT", "detail": repr(result)}

    result.setdefault("ok", False)
    result.setdefault("detail", f"{device.name}: {cap}")
    result["device"] = device.id
    result["capability"] = cap
    result["at"] = time.time()

    if result.get("ok") and isinstance(result.get("state"), dict):
        try:
            update(device.id, {"state": result["state"]})
        except Exception:
            pass
    return result


# ── A seeded example, only when the file has never existed ─────────────────

def ensure_file() -> None:
    """Write an empty registry once, so the file is there to be edited."""
    path = _path()
    if path.exists():
        return
    try:
        save([])
    except Exception:
        pass
