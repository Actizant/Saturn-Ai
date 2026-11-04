"""
agent/actions/__init__.py
=========================
Handler packages for :mod:`agent.action_router`.

Every module here registers itself with ``@register("intent")`` at import
time. One broken module must not take the rest down, so imports are guarded
per module: an intent whose module failed to load simply falls through to the
legacy rule chain in ``main.py``.
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger("action_router")

_MODULES = (
    "media",
    "apps",
    "commerce",
    "productivity",
    "creative",
    "system",
    "diagnostics",
    "self_heal",
    "learning",
    "network",
    "office",
)

FAILED_MODULES = []

for _name in _MODULES:
    try:
        importlib.import_module(f"{__name__}.{_name}")
    except Exception as _exc:  # pragma: no cover - defensive
        logger.warning("Action handlers '%s' unavailable: %s", _name, _exc)
        FAILED_MODULES.append(_name)

__all__ = ["FAILED_MODULES"]
