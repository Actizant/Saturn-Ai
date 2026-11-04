"""Saturn Connect subsystem.

This package adds the local gateway, device registry, pairing flow, and
protocol definitions used by Saturn to reach companion devices.
"""

from .service import SaturnConnectService, get_service

__all__ = ["SaturnConnectService", "get_service"]
