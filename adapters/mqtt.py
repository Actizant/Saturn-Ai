"""Any MQTT topic.

This is the shortest path to a house full of hardware: ESP32 boards, Tasmota
plugs, Zigbee bridges and Home Assistant itself all reduce to "publish this
string to that topic". A device entry carries the broker in ``address`` and the
per-capability topic in the mapping::

    "address": {"broker": "192.168.1.9", "port": 1883,
                "username": "saturn", "password": "..."},
    "capabilities": {
        "switch.on":  {"topic": "home/lamp/set", "payload": "ON"},
        "level.set":  {"topic": "home/lamp/set", "payload": "{\\"brightness\\": {level}}"}
    }

``paho-mqtt`` is an optional dependency. If it is missing, every call reports
that plainly instead of pretending the lamp is on.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from adapters import Adapter, failure, register, render

try:  # pragma: no cover - availability, not logic
    from paho.mqtt import publish as _publish
except Exception:  # pragma: no cover
    _publish = None  # type: ignore[assignment]

DEFAULT_PORT = 1883


class MqttAdapter(Adapter):
    name = "mqtt"
    summary = "any MQTT topic (ESP32, Tasmota, Zigbee bridges, Home Assistant)"
    hint = "the 'paho-mqtt' package is not installed"

    @property
    def available(self) -> bool:
        return _publish is not None

    def send(self, device: Any, capability: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if _publish is None:
            return failure("ADAPTER_UNAVAILABLE",
                           "The MQTT adapter needs the paho-mqtt package.",
                           hint=self.hint)

        address = dict(getattr(device, "address", None) or {})
        mapping = dict((getattr(device, "capabilities", None) or {}).get(capability) or {})

        broker = str(address.get("broker") or address.get("host") or "")
        if not broker:
            return failure("NO_ADDRESS", f"{device.name} has no broker in its address.")

        context = {**address, **(params or {}),
                   "device": device.id, "name": device.name, "capability": capability}

        topic = str(render(str(mapping.get("topic") or address.get("topic") or ""), context))
        if not topic:
            return failure("NO_TOPIC", f"{device.name} has no topic for {capability}.")

        payload = self._payload(mapping, params, context)

        auth = None
        if address.get("username"):
            auth = {"username": str(address["username"]),
                    "password": str(address.get("password") or "")}

        try:
            _publish.single(
                topic,
                payload=payload,
                qos=int(address.get("qos", 0)),
                retain=bool(address.get("retain", False)),
                hostname=broker,
                port=int(address.get("port", DEFAULT_PORT)),
                keepalive=10,
                auth=auth,
            )
        except Exception as exc:
            return failure("UNREACHABLE", f"{device.name} could not reach {broker}: {exc}",
                           topic=topic)

        return {"ok": True, "detail": f"{device.name} {capability}: published to {topic}",
                "topic": topic, "payload": payload}

    @staticmethod
    def _payload(mapping: Dict[str, Any], params: Dict[str, Any],
                 context: Dict[str, Any]) -> str:
        """What actually goes on the wire.

        A mapping without a payload means "send the argument", which is what
        nearly every simple broker expects.
        """
        payload = mapping.get("payload")
        if payload is None:
            if not params:
                return ""
            if "value" in params:
                return str(params["value"])
            if len(params) == 1:
                return str(next(iter(params.values())))
            payload = params
        if isinstance(payload, (dict, list)):
            return json.dumps(render(payload, context), ensure_ascii=False)
        return str(render(str(payload), context))


register(MqttAdapter())
