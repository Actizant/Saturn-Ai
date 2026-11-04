"""The adapter that touches nothing.

Every newly added device starts here. It prints exactly what would have been
sent, records it for the settings panel, and reports success - because success
here means "the request is well formed", not "the lamp is on". A device is only
promoted to a real transport once the owner has looked at one dry run and
agreed that it is the right request.

This is also what makes a strange API safe to explore: nothing in the house
moves while the mapping is being worked out.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict

from adapters import Adapter, register

LOG: Deque[Dict[str, Any]] = deque(maxlen=100)


class DryRunAdapter(Adapter):
    name = "dry_run"
    summary = "prints what would be sent and touches nothing"

    def send(self, device: Any, capability: str, params: Dict[str, Any]) -> Dict[str, Any]:
        mapping = dict((getattr(device, "capabilities", None) or {}).get(capability) or {})
        entry = {
            "at": time.time(),
            "device": getattr(device, "id", ""),
            "name": getattr(device, "name", ""),
            "capability": capability,
            "params": dict(params or {}),
            "mapping": mapping,
        }
        LOG.append(entry)

        state = {}
        if capability == "switch.on":
            state = {"is_on": True}
        elif capability == "switch.off":
            state = {"is_on": False}
        elif capability == "level.set" and "level" in (params or {}):
            state = {"level": params["level"]}

        described = ", ".join(f"{key}={value}" for key, value in (params or {}).items())
        return {
            "ok": True,
            "dry_run": True,
            "detail": f"[dry run] {entry['name']}: {capability}"
                      + (f" ({described})" if described else ""),
            "state": state,
        }


def recent(limit: int = 10):
    return list(LOG)[-max(1, int(limit)):]


register(DryRunAdapter())
