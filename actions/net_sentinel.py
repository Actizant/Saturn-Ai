"""
actions/net_sentinel.py
Saturn NetSentinel & Network Bridge Action.
Provides local network omniscience, router inspection, connected device discovery,
Wake-on-LAN power control, port scanning, and public internet diagnostics.
"""

from typing import Any, Dict
from core.net_sentinel import get_net_sentinel

PLUGIN = {
    "name": "net_sentinel",
    "description": (
        "Saturn NetSentinel: Autonomous Local Network & Router Supervisor. "
        "Allows Saturn full awareness of connected devices, router management ports, "
        "Wake-on-LAN power commands, device port scanning, and public internet telemetry. "
        "Supports actions: 'scan_network' (detect all connected devices on Wi-Fi/LAN), "
        "'inspect_router' (deep router audit), 'internet_status' (public IP & ISP health), "
        "'wake_device' (Wake-on-LAN magic packet), 'scan_ports' (port scan device), 'ping', "
        "'connect_all_devices' (mesh link all network devices and broadcast online beacon)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'scan_network', 'inspect_router', 'internet_status', 'wake_device', 'scan_ports', 'ping', 'connect_all_devices'."
            },
            "target": {
                "type": "STRING",
                "description": "Target IP, MAC address, or hostname (for ping, wake_device, or scan_ports)."
            },
            "deep": {
                "type": "BOOLEAN",
                "description": "Whether to perform a deep sweep across all 254 subnet addresses."
            }
        },
        "required": ["action"]
    }
}


def run(parameters: Dict[str, Any], player: Any = None, session_memory: Any = None) -> str:
    """Executes NetSentinel network actions."""
    sentinel = get_net_sentinel()
    action = str(parameters.get("action", "scan_network")).lower().strip()
    target = str(parameters.get("target") or "").strip()
    deep = bool(parameters.get("deep", False))

    if action in ("scan_network", "scan_wifi", "scan_lan", "devices", "list_devices"):
        res = sentinel.scan_local_network(deep=deep)
        router = res.get("router", {})
        devices = res.get("devices", [])
        gw_ip = res.get("gateway_ip", "Unknown")
        router_vendor = router.get("vendor", "Router")

        lines = [
            f"⚡ Saturn NetSentinel Scan Complete ({res.get('scan_duration_sec')}s):",
            f"• Gateway: {gw_ip} [{router_vendor}]",
            f"• Subnet: {res.get('subnet')} • Connected Devices: {len(devices)} active\n"
        ]
        for d in devices:
            ports_str = f" Ports: {d.get('open_ports')}" if d.get("open_ports") else ""
            lines.append(f"  [{d.get('device_type')}] {d.get('ip')} ({d.get('mac')}) — {d.get('vendor')}{ports_str}")

        if res.get("card_path"):
            lines.append(f"\nInteractive HUD Network Map generated at: {res.get('card_path')}")

        # Launch Tactical Radar HUD overlay
        try:
            if player and hasattr(player, "show_radar_hud"):
                player.show_radar_hud(res)
            elif player and hasattr(player, "_win") and hasattr(player._win, "_radar_hud_sig"):
                player._win._radar_hud_sig.emit(res)
            elif player:
                from core.radar_hud import show_radar_hud
                show_radar_hud(res)
        except Exception:
            pass

        return "\n".join(lines)

    elif action in ("inspect_router", "router", "check_router", "router_info"):
        res = sentinel.inspect_router()
        open_p = res.get("open_ports", [])
        return (
            f"Router Audit: Gateway {res.get('gateway_ip')} [{res.get('vendor')}] | "
            f"MAC: {res.get('mac_address')} | Status: {res.get('status')} ({res.get('latency_ms')}ms) | "
            f"Management Ports: {open_p} | Web Admin: '{res.get('web_gui_title')}' ({res.get('web_url')})"
        )

    elif action in ("internet_status", "internet", "public_ip", "isp"):
        tele = sentinel.get_internet_telemetry()
        status_str = "ONLINE" if tele.get("connected") else "OFFLINE"
        return (
            f"Internet Uplink: {status_str} | Public IP: {tele.get('public_ip')} | "
            f"ISP: {tele.get('isp')} ({tele.get('city')}, {tele.get('country')}) | "
            f"Ping Latency: {tele.get('ping_latency_ms')}ms | DNS Lookup: {tele.get('dns_latency_ms')}ms"
        )

    elif action in ("wake_device", "wake_on_lan", "wol"):
        if not target:
            return "Error: Target MAC address required for Wake-on-LAN."
        res = sentinel.wake_on_lan(target)
        return res.get("message", "Wake-on-LAN executed.")

    elif action in ("scan_ports", "port_scan"):
        if not target:
            return "Error: Target IP required for port scan."
        res = sentinel.scan_device_ports(target)
        return f"Port Scan on {target}: Open Ports: {res.get('open_ports')} ({res.get('count')} open services)"

    elif action in ("ping", "ping_device"):
        if not target:
            target = "1.1.1.1"
        res = sentinel.ping_target(target)
        return f"Ping {target}: {res.get('status')} | Average Latency: {res.get('avg_latency_ms')}ms"

    elif action in ("connect_all_devices", "connect_all", "link_devices", "mesh_connect", "broadcast_online"):
        res = sentinel.connect_all_devices()
        linked = res.get("total_devices_linked", 0)
        # Refresh Radar HUD overlay if active
        try:
            if player and hasattr(player, "show_radar_hud"):
                player.show_radar_hud(sentinel.last_scan_result or {})
            elif player and hasattr(player, "_win") and hasattr(player._win, "_radar_hud_sig"):
                player._win._radar_hud_sig.emit(sentinel.last_scan_result or {})
            elif player:
                from core.radar_hud import show_radar_hud
                show_radar_hud(sentinel.last_scan_result or {})
        except Exception:
            pass
        return (
            f"⚡ Zero-Touch Device Mesh Connected: Linked {linked} devices on your network. "
            f"Subnet announcement packet broadcasted. Radar HUD synchronized."
        )

    return f"Unknown NetSentinel action '{action}'."
