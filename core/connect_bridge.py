"""A paired phone, described as a device Saturn already knows how to reach.

WHY A PROJECTION AND NOT A SECOND LIST
    ``saturn_connect`` has its own device store, its own pairing, its own
    capability words. None of that is wrong - it is how a phone gets paired in
    the first place. What was wrong is that the result lived *outside* the
    device registry, so "turn on the lamp" and "turn on the phone's flashlight"
    were answered by two different code paths, and only one of them asked the
    trust gate.

    This module is the whole fix on the registry side: a function that reads
    the gateway's device list and hands it back as ordinary
    :class:`~core.capability_registry.Device` rows. It is registered as a
    *source*, so a phone appears in ``load()`` without ever being written into
    ``devices.json``. A phone that is unpaired or offline stops appearing, and
    nothing stale is left behind.

WHY THE ID IS PREFIXED
    A phone's id comes from its own agent and could collide with a name the
    owner wrote down. ``connect:`` keeps the namespaces apart and makes the
    origin obvious in a listing, while ``resolve()`` still matches the phone's
    own name, so a person can say "my phone" and be understood.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import capability_registry as registry
from core.capability_registry import COMFORT, Device
from core.connect_vocabulary import capabilities as phone_capabilities

#: How a live phone is told apart from a device that was written down.
PREFIX = "connect:"


def service() -> Optional[Any]:
    """The phone gateway service, if this process has one.

    Never starts one: a device list is not a reason to open a port.
    """
    try:
        from saturn_connect.service import current_service

        return current_service()
    except Exception:
        return None


def address_for(device_id: str) -> Dict[str, str]:
    """The address a row carries, so the adapter knows which phone it meant."""
    return {"target": str(device_id)}


def target_of(device: Any) -> str:
    """The gateway's own id for a device row, whichever road it came down.

    A row built here carries it in ``address``; a row the model named by hand
    may carry it only in the id. Both end up as the same string.
    """
    address = getattr(device, "address", None) or {}
    target = str(address.get("target") or "").strip()
    if target:
        return target
    own = str(getattr(device, "id", "") or "")
    return own[len(PREFIX):] if own.startswith(PREFIX) else own


def devices() -> List[Device]:
    """Every paired phone, right now, as registry rows.

    A gateway that is not there, or that cannot answer, is a phone that is not
    in the house - not an exception thrown into ``load()``.
    """
    gateway = service()
    if gateway is None:
        return []
    try:
        records = list(gateway.list_devices() or [])
    except Exception:
        return []

    capabilities = {capability: {} for capability in phone_capabilities()}
    rows: List[Device] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        device_id = str(record.get("device_id") or "").strip()
        if not device_id or record.get("revoked"):
            continue
        platform = str(record.get("platform") or "phone")
        rows.append(Device(
            id=f"{PREFIX}{device_id}",
            name=str(record.get("name") or device_id),
            adapter="connect",
            address=address_for(device_id),
            capabilities=dict(capabilities),
            state={
                "online": bool(record.get("online")),
                "battery": record.get("battery"),
                "platform": platform,
                # What the phone itself says it will do. Kept for the listing;
                # the gateway enforces it, so Saturn does not second-guess it.
                "granted": list(record.get("capabilities") or []),
            },
            kind=platform,
            enabled=True,
            trust=COMFORT,
            notes="paired through Saturn Connect",
            source=PREFIX.rstrip(":"),
        ))
    return rows


def install() -> None:
    """Join this module's list to the registry. Importing it does the same."""
    registry.register_source(devices)


install()
