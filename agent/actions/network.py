"""
agent/actions/network.py
========================
NetSentinel: LAN topology sweeps, mesh broadcast, router inspection, uplink
telemetry, Wake-on-LAN and ping. Every branch drives the tactical Radar HUD
when the window exposes one.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


@register("net_sentinel")
def handle_net_sentinel(ctx: ActionContext) -> bool:
    action = str(ctx.action or "scan_network").lower()
    target = str(ctx.param("target") or "")

    ctx.speak(ctx.ack("Executing NetSentinel..."))

    def _run() -> None:
        try:
            from core.net_sentinel import get_net_sentinel

            sentinel = get_net_sentinel()

            if action in ("scan_network", "scan_wifi", "scan_lan", "devices", "radar"):
                # Pop the tactical Net-Radar immediately, in sweep mode.
                ctx.ui_call("show_radar_hud", sentinel.last_scan_result or {})
                ctx.ui_call(
                    "begin_task_workspace",
                    "NetSentinel: Subnet Topology Audit",
                    [
                        "Activate Saturn Net-Radar Scope",
                        "Sweep /24 subnet addresses via SendARP",
                        "Resolve OUI hardware vendors & open service ports",
                        "Render interactive Cyberpunk Network Map",
                    ],
                    source=ctx.source or "local",
                )
                result = sentinel.scan_local_network(deep=False)
                card_path = result.get("card_path")
                device_count = result.get("total_devices_found", 0)
                gateway_ip = result.get("gateway_ip", "192.168.0.1")
                router_vendor = (result.get("router") or {}).get("vendor", "Router")

                # Push the live discovery set to the Radar HUD.
                ctx.ui_call("show_radar_hud", result)

                summary = (
                    f"Network scan complete: Discovered {device_count} active devices "
                    f"on your LAN through gateway {gateway_ip} ({router_vendor})."
                )
                ctx.ui_call(
                    "update_task_workspace",
                    status=f"{device_count} Devices Online",
                    output=summary,
                    percent=100,
                )
                if card_path:
                    internet = result.get("internet") or {}
                    ctx.ui_call(
                        "show_hud_deliverable",
                        title=f"Network Topology: {gateway_ip}",
                        summary=(
                            f"Discovered {device_count} connected devices on "
                            f"{result.get('subnet')}"
                        ),
                        bullets=[
                            f"Gateway: {gateway_ip} ({router_vendor})",
                            f"Public IP: {internet.get('public_ip')}",
                            f"ISP: {internet.get('isp')}",
                            f"Active LAN Nodes: {device_count}",
                        ],
                        file_path=str(card_path),
                        kind="net_sentinel",
                    )
                ctx.speak(
                    f"Scan complete, sir. Discovered {device_count} devices connected "
                    f"to your {router_vendor} router. Radar scope is active."
                )

            elif action in (
                "connect_all_devices",
                "connect_all",
                "link_devices",
                "mesh_connect",
                "broadcast_online",
            ):
                ctx.ui_call("show_radar_hud", sentinel.last_scan_result or {})
                ctx.ui_call(
                    "begin_task_workspace",
                    "NetSentinel: Zero-Touch Mesh Sync",
                    [
                        "Broadcast Saturn Online beacon across subnet",
                        "Probe SSDP, Cast & UPnP services on LAN endpoints",
                        "Establish zero-install device telemetry mesh",
                        "Synchronize Radar HUD device status",
                    ],
                    source=ctx.source or "local",
                )
                mesh = sentinel.connect_all_devices("Saturn Online // Node Linked")
                linked = mesh.get("total_devices_linked", 0)

                ctx.ui_call("show_radar_hud", sentinel.last_scan_result or {})
                ctx.ui_call(
                    "update_task_workspace",
                    status=f"{linked} Devices Linked",
                    output=(
                        f"Network mesh online: Connected {linked} devices. Broadcast "
                        "notification transmitted across LAN."
                    ),
                    percent=100,
                )
                ctx.speak(
                    f"Network mesh synchronized, sir. Linked with {linked} devices "
                    "across your network. Online notification broadcasted."
                )

            elif action in ("inspect_router", "router"):
                result = sentinel.inspect_router()
                open_ports = result.get("open_ports", [])
                message = (
                    f"Router at {result.get('gateway_ip')} is a "
                    f"{result.get('vendor')} gateway. Open ports: {open_ports}. "
                    "Web administration console is active."
                )
                ctx.ui_call(
                    "update_task_workspace",
                    status=f"Router: {result.get('vendor')}",
                    output=message,
                    percent=100,
                )
                ctx.speak(message)

            elif action in ("internet_status", "internet", "public_ip"):
                telemetry = sentinel.get_internet_telemetry()
                status = "Online" if telemetry.get("connected") else "Offline"
                message = (
                    f"Internet uplink is {status}, sir. Public IP is "
                    f"{telemetry.get('public_ip')} via {telemetry.get('isp')} in "
                    f"{telemetry.get('city')}, with "
                    f"{telemetry.get('ping_latency_ms')} milliseconds latency."
                )
                ctx.ui_call(
                    "update_task_workspace",
                    status=f"Internet: {status}",
                    output=message,
                    percent=100,
                )
                ctx.speak(message)

            elif action in ("wake_device", "wake_on_lan"):
                if not target:
                    ctx.speak("Please specify the target MAC address to wake, sir.")
                    return
                result = sentinel.wake_on_lan(target)
                ctx.speak(result.get("message", "Wake-on-LAN packet transmitted."))

            elif action == "ping":
                result = sentinel.ping_target(target or "1.1.1.1")
                ctx.speak(
                    f"Host {result.get('target')} is {result.get('status')} with "
                    f"average latency of {result.get('avg_latency_ms')} milliseconds."
                )

        except Exception as exc:
            ctx.log(f"ERR: NetSentinel failed: {exc}")
            ctx.speak(f"NetSentinel encountered an issue: {exc}")

    ctx.spawn(_run, name="net-sentinel")
    return True
