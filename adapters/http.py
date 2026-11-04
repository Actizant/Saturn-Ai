"""Any REST API, described entirely in the device entry.

There is no code here for a TV, a printer or a lamp. A device says where it
lives and, per capability, what to send::

    "address": {"base_url": "http://192.168.1.20:8060"},
    "capabilities": {
        "media.play":  {"method": "POST", "path": "/keypress/Play"},
        "display.show": {"method": "POST", "path": "/input", "body": {"url": "{content}"}}
    }

That is Roku's ECP, OctoPrint's REST API, a Home Assistant webhook and an
ESPHome endpoint alike - one adapter, four devices, no new files.
"""

from __future__ import annotations

from typing import Any, Dict

from adapters import Adapter, failure, register, render

try:  # pragma: no cover - exercised by availability, not by logic
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]

DEFAULT_TIMEOUT = 5.0
_READ_VERBS = ("get", "read", "status", "state")


class HttpAdapter(Adapter):
    name = "http"
    summary = "any REST API described by a per-capability mapping"
    hint = "the 'requests' package is not installed"

    @property
    def available(self) -> bool:
        return requests is not None

    def send(self, device: Any, capability: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if requests is None:
            return failure("ADAPTER_UNAVAILABLE",
                           "The HTTP adapter needs the requests package.",
                           hint=self.hint)

        address = dict(getattr(device, "address", None) or {})
        mapping = dict((getattr(device, "capabilities", None) or {}).get(capability) or {})

        base = str(address.get("base_url") or address.get("url") or "").rstrip("/")
        if not base:
            return failure("NO_ADDRESS", f"{device.name} has no base_url in its address.")

        context = {**address, **(params or {}),
                   "device": device.id, "name": device.name, "capability": capability}

        path = str(render(str(mapping.get("path") or ""), context))
        url = f"{base}/{path.lstrip('/')}" if path else base

        verb = str(capability).rsplit(".", 1)[-1]
        method = str(mapping.get("method")
                     or ("GET" if verb in _READ_VERBS else "POST")).upper()

        headers = render({**(address.get("headers") or {}),
                          **(mapping.get("headers") or {})}, context)
        body = render(mapping.get("body"), context)
        timeout = float(address.get("timeout") or DEFAULT_TIMEOUT)

        try:
            response = requests.request(
                method, url,
                headers=headers or None,
                json=body if body is not None else None,
                timeout=timeout,
                # Verification is on unless a device explicitly says otherwise.
                verify=bool(address.get("verify_tls", True)),
            )
        except Exception as exc:
            return failure("UNREACHABLE", f"{device.name} did not answer: {exc}",
                           url=url, method=method)

        status = int(getattr(response, "status_code", 0) or 0)
        ok = 200 <= status < 300
        result: Dict[str, Any] = {
            "ok": ok,
            "detail": f"{device.name} {capability}: HTTP {status}",
            "status": status,
            "method": method,
            "url": url,
            "body": str(getattr(response, "text", ""))[:400],
        }
        if not ok:
            result["error"] = "HTTP_STATUS"

        if mapping.get("track_state"):
            try:
                payload = response.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                result["state"] = {
                    key: value for key, value in payload.items()
                    if isinstance(value, (bool, int, float, str))
                }
        return result

    def probe(self, device: Any) -> Dict[str, Any]:
        if requests is None:
            return {"ok": False, "detail": self.hint}
        address = dict(getattr(device, "address", None) or {})
        base = str(address.get("base_url") or address.get("url") or "")
        if not base:
            return {"ok": False, "detail": "no base_url"}
        try:
            response = requests.get(base, timeout=3.0,
                                    verify=bool(address.get("verify_tls", True)))
            return {"ok": True, "detail": f"HTTP {response.status_code}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}


register(HttpAdapter())
