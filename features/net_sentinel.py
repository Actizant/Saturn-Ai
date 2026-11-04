"""
features/net_sentinel.py
Hot-loaded dynamic feature for Saturn: NetSentinel & Network Bridge.
"""

from typing import Any, Dict
from core.net_sentinel import get_net_sentinel

FEATURE_METADATA = {
    "name": "net_sentinel",
    "description": (
        "Saturn NetSentinel: Autonomous Local Network & Router Supervisor. "
        "Allows Saturn full awareness of connected devices, router management ports, "
        "Wake-on-LAN power commands, device port scanning, and public internet telemetry."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'scan_network', 'inspect_router', 'internet_status', 'wake_device', 'scan_ports', 'ping'."
            },
            "target": {
                "type": "STRING",
                "description": "Target IP, MAC address, or hostname."
            },
            "deep": {
                "type": "BOOLEAN",
                "description": "Whether to perform deep sweep across all 254 subnet addresses."
            }
        },
        "required": ["action"]
    },
    "version": "1.0.0",
    "active": True
}


def execute(**kwargs: Any) -> Dict[str, Any]:
    sentinel = get_net_sentinel()
    action = str(kwargs.get("action", "scan_network")).lower().strip()
    target = str(kwargs.get("target") or "").strip()
    deep = bool(kwargs.get("deep", False))

    if action in ("scan_network", "scan_wifi", "scan_lan", "devices"):
        res = sentinel.scan_local_network(deep=deep)
        return {
            "success": True,
            "action": "scan_network",
            "devices_count": res.get("total_devices_found"),
            "data": res,
            "summary": f"Discovered {res.get('total_devices_found')} devices on local network."
        }

    elif action in ("inspect_router", "router"):
        res = sentinel.inspect_router()
        return {
            "success": True,
            "action": "inspect_router",
            "data": res,
            "summary": f"Router at {res.get('gateway_ip')} [{res.get('vendor')}] is online."
        }

    elif action in ("internet_status", "internet", "public_ip"):
        tele = sentinel.get_internet_telemetry()
        return {
            "success": True,
            "action": "internet_status",
            "data": tele,
            "summary": f"Internet Online: Public IP {tele.get('public_ip')} ({tele.get('isp')})"
        }

    elif action in ("wake_device", "wake_on_lan"):
        res = sentinel.wake_on_lan(target)
        return {
            "success": res.get("success", False),
            "action": "wake_device",
            "summary": res.get("message")
        }

    elif action in ("scan_ports", "port_scan"):
        res = sentinel.scan_device_ports(target)
        return {
            "success": True,
            "action": "scan_ports",
            "data": res,
            "summary": f"Target {target} open ports: {res.get('open_ports')}"
        }

    elif action == "ping":
        res = sentinel.ping_target(target or "1.1.1.1")
        return {
            "success": res.get("success", False),
            "action": "ping",
            "data": res,
            "summary": f"Ping {target}: {res.get('status')} ({res.get('avg_latency_ms')}ms)"
        }

    elif action in ("connect_all_devices", "connect_all", "link_devices", "mesh_connect", "broadcast_online"):
        res = sentinel.connect_all_devices()
        return {
            "success": True,
            "action": "connect_all_devices",
            "data": res,
            "summary": res.get("message", "Connected all devices on local network.")
        }

    return {"success": False, "error": f"Unknown action '{action}'"}
