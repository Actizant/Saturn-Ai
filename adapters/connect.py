"""A paired phone, reachable the same way as a lamp.

WHAT THIS IS
    The last few feet of the "one address space" work. A phone already appears
    in the device registry (see :mod:`core.connect_bridge`), so the only thing
    missing was a transport that knows how to say a capability to it. That is
    all this file is: translate, route through the gateway, and report exactly
    what came back.

WHAT IT DELIBERATELY DOES NOT DO
    It does not check the phone's permissions. The gateway already does, with
    the list the phone itself declared, and a second opinion invented here
    would only be a table that drifts. If the phone refuses, the refusal is
    passed on in the phone's own words.
"""

from __future__ import annotations

from typing import Any, Dict

from adapters import Adapter, failure, register
from core import connect_bridge
from core.connect_vocabulary import to_action


def _describe(data: Any) -> str:
    """A short, readable rendering of whatever the phone sent back."""
    if isinstance(data, dict):
        parts = [f"{key}={value}" for key, value in list(data.items())[:4]]
        return ", ".join(parts)
    if data is None or isinstance(data, bool):
        return ""
    return str(data)


def _state_for(capability: str, data: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """What the registry should remember about a phone after this call."""
    state: Dict[str, Any] = {}
    if capability == "switch.on":
        state["is_on"] = True
    elif capability == "switch.off":
        state["is_on"] = False
    if isinstance(data, dict):
        for key in ("battery", "level", "volume", "is_on", "online"):
            if key in data:
                state[key] = data[key]
        # The phone says "volume"; the vocabulary says "level". Same number.
        if "volume" in state and "level" not in state:
            state["level"] = state.pop("volume")
    # ``volume_set`` confirms nothing, but the call did establish the level it
    # was asked for. The dry-run adapter records the same thing; dropping it
    # here would throw away the only fact the round trip produced.
    if capability == "level.set" and "level" not in state:
        level = (params or {}).get("level")
        if level is not None:
            state["level"] = level
    return state


class ConnectAdapter(Adapter):
    name = "connect"
    summary = "a phone paired through the Saturn Connect gateway"
    hint = "pair one from the phone's Saturn Connect app, or let it announce itself"

    @property
    def available(self) -> bool:
        return connect_bridge.service() is not None

    def send(self, device: Any, capability: str, params: Dict[str, Any]) -> Dict[str, Any]:
        name = str(getattr(device, "name", "") or "the phone")

        gateway = connect_bridge.service()
        if gateway is None:
            return failure(
                "GATEWAY_UNAVAILABLE",
                "Saturn Connect isn't running in this window, so the phone can't be reached.",
            )

        translation = to_action(capability, params)
        if not translation.ok:
            return failure("NOT_EXPRESSIBLE", translation.problem)

        target = connect_bridge.target_of(device)
        if not target:
            return failure("NO_TARGET", f"{name} has no id the gateway would recognise.")

        try:
            result = gateway.route_command(target, translation.target, translation.params)
        except Exception as exc:
            return failure("ROUTE_FAILED", f"The phone gateway could not be asked: {exc}")

        if not isinstance(result, dict):
            return {"ok": True, "detail": f"{name}: {capability}", "state": {}, "data": result}

        if not result.get("success", False):
            return failure(
                str(result.get("error_code") or "ROUTE_FAILED"),
                str(result.get("error") or f"{name} did not answer."),
            )

        data = result.get("data", result.get("result"))
        described = _describe(data)
        detail = f"{name}: {capability} ({translation.target})"
        if described:
            detail += f" - {described}"
        return {"ok": True, "detail": detail, "state": _state_for(capability, data, params),
                "data": data, "request_id": result.get("request_id", "")}


register(ConnectAdapter())
