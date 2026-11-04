"""Transports, not devices.

Every adapter here answers one question: *how do bytes reach this thing?* None
of them knows what a TV or a printer is. That split is what keeps the hardware
layer universal - a new device is an entry in ``devices.json`` naming an
adapter and a mapping, never a new Python file.

THE BUILT-INS
    ``http``     any REST API, described entirely in the device's mapping.
    ``mqtt``     any broker topic, for ESP32 and most cheap IoT.
    ``adb``      Android over USB or Wi-Fi: phones, TVs, TV boxes.
    ``connect``  a phone paired through Saturn Connect, over the local gateway.
    ``dry_run``  prints the request and touches nothing. This is the adapter a
                 newly added device starts as, so nothing moves by accident.

WHAT AN ADAPTER PROMISES
    ``send(device, capability, params) -> dict`` and never raises. The dict
    always carries ``ok``; when it is False it carries ``error`` and a
    ``detail`` that a person can read out loud. An adapter that is unsure must
    say so rather than claim success.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_TOKEN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def render(template: Any, context: Dict[str, Any]) -> Any:
    """Substitute ``{name}`` placeholders from ``context``.

    A template that is *exactly* one placeholder keeps the value's own type, so
    ``{"level": "{level}"}`` sends the number 40 and not the string "40".
    Unknown names are left alone - a typo should be visible in the request, not
    silently become an empty string.
    """
    if isinstance(template, dict):
        return {key: render(value, context) for key, value in template.items()}
    if isinstance(template, list):
        return [render(item, context) for item in template]
    if not isinstance(template, str):
        return template

    whole = _TOKEN.fullmatch(template)
    if whole and whole.group(1) in context:
        return context[whole.group(1)]

    def swap(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key)
        return match.group(0) if value is None else str(value)

    return _TOKEN.sub(swap, template)


def failure(error: str, detail: str, **extra: Any) -> Dict[str, Any]:
    payload = {"ok": False, "error": error, "detail": detail}
    payload.update(extra)
    return payload


class Adapter:
    name = "base"
    summary = ""
    hint = ""

    @property
    def available(self) -> bool:
        return True

    def send(self, device: Any, capability: str, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def probe(self, device: Any) -> Dict[str, Any]:
        """Reachability, if the transport can answer cheaply. Best effort."""
        return {"ok": True, "detail": "not checked"}

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "summary": self.summary,
                "available": self.available, "hint": self.hint}


# ── The registry ────────────────────────────────────────────────────────────

REGISTRY: Dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    REGISTRY[adapter.name] = adapter
    return adapter


def get_adapter(name: str) -> Optional[Adapter]:
    _load_builtins()
    key = str(name or "").strip().lower()
    return REGISTRY.get(key) or REGISTRY.get(key.replace("-", "_"))


def adapters() -> List[Adapter]:
    _load_builtins()
    return [REGISTRY[key] for key in sorted(REGISTRY)]


def describe() -> str:
    return "\n".join(
        f"- {adapter.name}: {adapter.summary}"
        + ("" if adapter.available else f" (unavailable: {adapter.hint})")
        for adapter in adapters()
    )


_LOADED = False


def _load_builtins() -> None:
    """Import the built-ins once. One missing library must not hide the rest."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from adapters import adb, connect, dry_run, http, mqtt  # noqa: F401  (registration)
