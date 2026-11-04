"""
tests/test_net_sentinel.py
Unit tests for Saturn NetSentinel & Autonomous Local Network Supervisor.
Tests router inspection, network scanning, device classification, Wake-on-LAN,
ping probing, internet health telemetry, action plugins, dynamic features, and Tier 1 intent reflexes.
"""

from unittest.mock import MagicMock, patch
from pathlib import Path
import json
import socket
import pytest

from core.net_sentinel import NetSentinel, get_net_sentinel
from core.intent_dispatcher import IntentDispatcher
from actions import net_sentinel as net_sentinel_action
from features import net_sentinel as net_sentinel_feature


def test_net_sentinel_singleton():
    """Verifies that NetSentinel returns a singleton instance."""
    s1 = get_net_sentinel()
    s2 = NetSentinel.get_instance()
    assert s1 is s2
    assert isinstance(s1, NetSentinel)


def test_mac_vendor_resolution():
    """Verifies OUI resolution for known, local, and unknown hardware vendors."""
    sentinel = get_net_sentinel()

    # Known vendors from curated dictionary
    assert sentinel.resolve_mac_vendor("28:87:BA:D0:EC:BA") == "TP-Link"
    assert sentinel.resolve_mac_vendor("28-87-ba-d0-ec-ba") == "TP-Link"
    assert "Raspberry Pi" in sentinel.resolve_mac_vendor("b8:27:eb:12:34:56")
    assert "Apple" in sentinel.resolve_mac_vendor("3C:52:82:AA:BB:CC")
    assert "Samsung" in sentinel.resolve_mac_vendor("AC:5F:3E:11:22:33")
    assert "Espressif" in sentinel.resolve_mac_vendor("24:62:AB:77:88:99")

    # Local / special interface
    assert sentinel.resolve_mac_vendor("---") == "Local Interface"

    # Unknown vendor fallback when offline or unregistered (universal MAC)
    with patch("urllib.request.urlopen", side_effect=Exception("Offline")):
        assert sentinel.resolve_mac_vendor("04:00:00:11:22:33") == "Generic Network Device"
        # Locally administered / private randomized MAC (Android / iOS)
        assert "Mobile Device" in sentinel.resolve_mac_vendor("86:58:FD:3D:FF:CF")
        assert "Mobile Device" in sentinel.resolve_mac_vendor("02:00:00:11:22:33")


def test_device_classification():
    """Verifies heuristic device categorization based on gateway status, vendor, and ports."""
    sentinel = get_net_sentinel()

    # Host Machine
    assert sentinel.classify_device(is_host=True) == "Host PC / Workstation"

    # Router
    assert sentinel.classify_device(is_gateway=True, vendor="TP-Link") == "Router / Gateway"
    assert sentinel.classify_device(open_ports=[53, 80]) == "Router / Gateway"

    # Smart TV / Media Player via Google Cast 8009 or TV vendor
    assert sentinel.classify_device(open_ports=[8009]) == "Smart TV / Media Player"
    assert sentinel.classify_device(vendor="LG Electronics") == "Smart TV / Media Player"

    # Smart Home / IoT
    assert sentinel.classify_device(vendor="Espressif IoT") == "IoT / Web Device"
    assert sentinel.classify_device(open_ports=[8080]) == "IoT / Web Device"

    # PC / Server
    assert sentinel.classify_device(open_ports=[3389]) == "PC / Server"
    assert sentinel.classify_device(open_ports=[445]) == "PC / Server"

    # Mobile
    assert sentinel.classify_device(vendor="Apple") == "Mobile / Smartphone"
    assert sentinel.classify_device(open_ports=[5555]) == "Mobile / Smartphone"


def test_wake_on_lan():
    """Verifies Wake-on-LAN magic packet generation and UDP broadcast."""
    sentinel = get_net_sentinel()

    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value.__enter__.return_value = mock_sock

        # Valid MAC colon separated
        res = sentinel.wake_on_lan("28:87:BA:D0:EC:BA")
        assert res["success"] is True
        assert res["mac"] == "28:87:BA:D0:EC:BA"
        mock_sock.sendto.assert_called_once()
        sent_data = mock_sock.sendto.call_args[0][0]
        # Magic packet: 6 bytes 0xFF followed by 16 repetitions of 6-byte MAC (total 102 bytes)
        assert len(sent_data) == 102
        assert sent_data[:6] == b"\xff" * 6

        # Valid MAC hyphen separated
        res_hyphen = sentinel.wake_on_lan("28-87-ba-d0-ec-ba")
        assert res_hyphen["success"] is True

        # Invalid MAC
        res_invalid = sentinel.wake_on_lan("invalid-mac-address")
        assert res_invalid["success"] is False
        assert "Invalid MAC address" in res_invalid["message"]


def test_ping_target():
    """Verifies host ping latency measurement and status parsing."""
    sentinel = get_net_sentinel()

    with patch("subprocess.check_output") as mock_run:
        mock_run.return_value = "Reply from 192.168.0.1: bytes=32 time=2ms TTL=64\nAverage = 2ms\n"
        res = sentinel.ping_target("192.168.0.1", count=1)
        assert res["success"] is True
        assert res["target"] == "192.168.0.1"
        assert res["avg_latency_ms"] == 2.0
        assert res["status"] == "Online"

    # Empty target
    empty_res = sentinel.ping_target("")
    assert empty_res["success"] is False


def test_scan_device_ports():
    """Verifies multi-port scanning probe."""
    sentinel = get_net_sentinel()

    def fake_connect(addr):
        ip, port = addr
        if port in (80, 22):
            return 0
        return 10061

    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock.connect_ex.side_effect = fake_connect
        mock_sock_cls.return_value = mock_sock

        res = sentinel.scan_device_ports("192.168.0.1", ports=[80, 22, 443])
        assert res["target_ip"] == "192.168.0.1"
        assert res["open_ports"] == [22, 80]
        assert res["count"] == 2


def test_get_local_network_info():
    """Verifies gateway and subnet identification."""
    info = NetSentinel.get_local_network_info()
    assert "host_ip" in info
    assert "gateway_ip" in info
    assert "subnet_prefix" in info
    assert "hostname" in info


def test_inspect_router_structure():
    """Verifies router inspection returns valid schema."""
    sentinel = get_net_sentinel()

    with patch.object(NetSentinel, "get_local_network_info", return_value={
        "host_ip": "192.168.0.112",
        "gateway_ip": "192.168.0.1",
        "subnet_prefix": "192.168.0.",
        "hostname": "TEST-PC"
    }):
        with patch.object(sentinel, "scan_device_ports", return_value={"open_ports": [80, 22, 53]}):
            with patch.object(sentinel, "ping_target", return_value={"success": True, "avg_latency_ms": 1.5}):
                with patch("subprocess.check_output", return_value="192.168.0.1  28-87-ba-d0-ec-ba  dynamic"):
                    res = sentinel.inspect_router()
                    assert res["success"] is True
                    assert res["gateway_ip"] == "192.168.0.1"
                    assert res["mac_address"] == "28-87-BA-D0-EC-BA"
                    assert "TP-Link" in res["vendor"]
                    assert 80 in res["open_ports"]
                    assert "Online" in res["status"]


def test_internet_telemetry_structure():
    """Verifies public internet health telemetry schema."""
    sentinel = get_net_sentinel()

    mock_payload = json.dumps({
        "status": "success",
        "query": "139.5.47.129",
        "isp": "AS133612 Zess Networks",
        "org": "Zess Networks",
        "city": "Dombivli",
        "country": "India"
    }).encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = mock_payload
    mock_resp.__enter__.return_value = mock_resp

    with patch("socket.gethostbyname", return_value="8.8.8.8"):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(sentinel, "ping_target", return_value={"avg_latency_ms": 18.0}):
                res = sentinel.get_internet_telemetry()
                assert res["connected"] is True
                assert res["public_ip"] == "139.5.47.129"
                assert "Zess Networks" in res["isp"]
                assert res["country"] == "India"
                assert res["ping_latency_ms"] == 18.0


def test_hud_network_map_generation():
    """Verifies Cyberpunk HTML deliverable map is written."""
    sentinel = get_net_sentinel()
    data = {
        "gateway_ip": "192.168.0.1",
        "subnet": "192.168.0.0/24",
        "total_devices_found": 2,
        "router": {"vendor": "TP-Link Systems"},
        "internet": {"public_ip": "139.5.47.129", "isp": "Zess Networks"},
        "devices": [
            {
                "ip": "192.168.0.1",
                "mac": "28-87-BA-D0-EC-BA",
                "vendor": "TP-Link Systems",
                "device_type": "Router / Gateway",
                "open_ports": [80, 22],
                "is_router": True,
                "is_host": False
            },
            {
                "ip": "192.168.0.112",
                "mac": "---",
                "vendor": "This Machine",
                "device_type": "Host PC / Workstation",
                "open_ports": [],
                "is_router": False,
                "is_host": True
            }
        ]
    }
    card_path = sentinel.generate_hud_network_map(data)
    assert card_path is not None
    assert str(card_path).endswith(".html")
    assert Path(card_path).exists()


def test_action_plugin():
    """Verifies actions/net_sentinel.py dispatches commands correctly and returns summaries."""
    with patch.object(NetSentinel, "inspect_router", return_value={
        "success": True, "gateway_ip": "192.168.0.1", "vendor": "TP-Link", "open_ports": [80, 22], "latency_ms": 1.2, "web_url": "http://192.168.0.1", "status": "Online"
    }):
        res = net_sentinel_action.run({"action": "inspect_router"})
        assert isinstance(res, str)
        assert "TP-Link" in res
        assert "192.168.0.1" in res

    with patch.object(NetSentinel, "wake_on_lan", return_value={
        "success": True, "mac": "AA:BB:CC:DD:EE:FF", "message": "Wake-on-LAN magic packet broadcasted."
    }):
        res_wol = net_sentinel_action.run({"action": "wake_device", "target": "AA:BB:CC:DD:EE:FF"})
        assert "Wake-on-LAN" in res_wol

    with patch.object(NetSentinel, "get_internet_telemetry", return_value={
        "connected": True, "public_ip": "1.2.3.4", "isp": "Test ISP", "city": "Mumbai", "country": "India", "ping_latency_ms": 10.0, "dns_latency_ms": 8.0
    }):
        res_inet = net_sentinel_action.run({"action": "internet_status"})
        assert "1.2.3.4" in res_inet
        assert "Test ISP" in res_inet


def test_dynamic_feature():
    """Verifies features/net_sentinel.py execution."""
    with patch.object(NetSentinel, "inspect_router", return_value={
        "success": True, "gateway_ip": "192.168.0.1", "vendor": "TP-Link", "open_ports": [80]
    }):
        out = net_sentinel_feature.execute(action="inspect_router")
        assert out["success"] is True
        assert out["action"] == "inspect_router"

    with patch.object(NetSentinel, "get_internet_telemetry", return_value={
        "connected": True, "public_ip": "1.2.3.4", "isp": "Test ISP"
    }):
        out_inet = net_sentinel_feature.execute(action="internet_status")
        assert out_inet["success"] is True
        assert out_inet["action"] == "internet_status"


def test_intent_dispatcher_net_sentinel_reflexes():
    """Verifies Tier 1 IntentDispatcher regex routing for network and router voice commands."""
    dispatcher = IntentDispatcher()

    # Scan network queries
    r1 = dispatcher.match_instant_reflex("scan network")
    assert r1 is not None
    assert r1.intent == "net_sentinel"
    assert r1.action == "scan_network"

    r2 = dispatcher.match_instant_reflex("who is on my wifi")
    assert r2 is not None
    assert r2.intent == "net_sentinel"
    assert r2.action == "scan_network"

    r3 = dispatcher.match_instant_reflex("devices on my network")
    assert r3 is not None
    assert r3.intent == "net_sentinel"
    assert r3.action == "scan_network"

    # Router inspection queries
    r4 = dispatcher.match_instant_reflex("inspect router")
    assert r4 is not None
    assert r4.intent == "net_sentinel"
    assert r4.action == "inspect_router"

    r5 = dispatcher.match_instant_reflex("check router")
    assert r5 is not None
    assert r5.intent == "net_sentinel"
    assert r5.action == "inspect_router"

    # Internet / IP telemetry queries
    r6 = dispatcher.match_instant_reflex("internet status")
    assert r6 is not None
    assert r6.intent == "net_sentinel"
    assert r6.action == "internet_status"

    r7 = dispatcher.match_instant_reflex("what is my public ip")
    assert r7 is not None
    assert r7.intent == "net_sentinel"
    assert r7.action == "internet_status"

    # Wake on LAN queries
    r8 = dispatcher.match_instant_reflex("wake on lan 28:87:BA:D0:EC:BA")
    assert r8 is not None
    assert r8.intent == "net_sentinel"
    assert r8.action == "wake_device"
    assert r8.parameters["target"] == "28:87:BA:D0:EC:BA"

    # Ping queries
    r9 = dispatcher.match_instant_reflex("ping 192.168.0.1")
    assert r9 is not None
    assert r9.intent == "net_sentinel"
    assert r9.action == "ping"
    assert r9.parameters["target"] == "192.168.0.1"

    # Radar queries
    r10 = dispatcher.match_instant_reflex("network radar")
    assert r10 is not None
    assert r10.intent == "net_sentinel"
    assert r10.action == "scan_network"

    r11 = dispatcher.match_instant_reflex("open radar")
    assert r11 is not None
    assert r11.intent == "net_sentinel"
    assert r11.action == "scan_network"


def test_radar_hud_assets_and_bridge():
    """Verifies Radar HUD HTML asset exists and RadarBridge works correctly."""
    from core.radar_hud import RADAR_HTML_FILE, RadarBridge, show_radar_hud

    assert RADAR_HTML_FILE.exists()
    content = RADAR_HTML_FILE.read_text(encoding="utf-8")
    assert "radarCanvas" in content
    assert "updateRadarData" in content
    assert "setScanningState" in content
    assert "triggerRescan" in content

    # Mock overlay and bridge
    mock_overlay = MagicMock()
    bridge = RadarBridge(mock_overlay)

    # Test closeRadar
    close_called = []
    bridge.close_requested.connect(lambda: close_called.append(True))
    bridge.closeRadar()
    assert len(close_called) == 1

    # Test pingDevice
    with patch.object(bridge._sentinel, "ping_target", return_value={"avg_latency_ms": 2.5}):
        bridge._run_ping_worker("192.168.0.1")
        mock_overlay.set_latency_result.assert_called_with("192.168.0.1", 2.5)

    # Test wakeDevice
    with patch.object(bridge._sentinel, "wake_on_lan", return_value={"success": True}):
        bridge.wakeDevice("AA:BB:CC:DD:EE:FF")
        bridge._sentinel.wake_on_lan.assert_called_with("AA:BB:CC:DD:EE:FF")


def test_cross_platform_ping_parsing():
    """Verifies ping response parsing for macOS, Linux, and international Windows locales."""
    sentinel = get_net_sentinel()

    # macOS format: round-trip min/avg/max/stddev = ...
    macos_out = """PING 1.1.1.1 (1.1.1.1): 56 data bytes
64 bytes from 1.1.1.1: icmp_seq=0 ttl=58 time=12.345 ms
--- 1.1.1.1 ping statistics ---
1 packets transmitted, 1 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 12.345/14.567/16.789/1.234 ms
"""
    with patch("subprocess.check_output", return_value=macos_out):
        res = sentinel.ping_target("1.1.1.1", count=1)
        assert res["success"] is True
        assert res["avg_latency_ms"] == 14.567

    # Linux format: rtt min/avg/max/mdev = ...
    linux_out = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=9.87 ms
--- 8.8.8.8 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
rtt min/avg/max/mdev = 9.870/11.230/12.500/0.890 ms
"""
    with patch("subprocess.check_output", return_value=linux_out):
        res = sentinel.ping_target("8.8.8.8", count=1)
        assert res["success"] is True
        assert res["avg_latency_ms"] == 11.230

    # Windows German locale: Mittelwert = 8ms
    win_de_out = """Ping wird ausgeführt für 192.168.1.1 mit 32 Bytes Daten:
Antwort von 192.168.1.1: Bytes=32 Zeit=8ms TTL=64
Ping-Statistik für 192.168.1.1:
    Pakete: Gesendet = 1, Empfangen = 1, Verloren = 0 (0% Verlust),
Ca. Zeitangaben in Millisek.:
    Minimum = 8ms, Maximum = 8ms, Mittelwert = 8ms
"""
    with patch("subprocess.check_output", return_value=win_de_out):
        res = sentinel.ping_target("192.168.1.1", count=1)
        assert res["success"] is True
        assert res["avg_latency_ms"] == 8.0

    # Windows French locale: Moyenne = 7ms
    win_fr_out = """Statistiques Ping pour 192.168.1.1:
    Paquets : envoyés = 1, reçus = 1, perdus = 0 (perte 0%),
Durée approximative des boucles en millisecondes :
    Minimum = 7ms, Maximum = 7ms, Moyenne = 7ms
"""
    with patch("subprocess.check_output", return_value=win_fr_out):
        res = sentinel.ping_target("192.168.1.1", count=1)
        assert res["success"] is True
        assert res["avg_latency_ms"] == 7.0


def test_cross_platform_arp_table_parsing():
    """Verifies ARP cache extraction on macOS (arp -an) and Linux (/proc/net/arp)."""
    # 1. macOS Darwin arp -an output
    macos_arp = """? (192.168.1.1) at 0:11:22:33:44:55 on en0 ifscope [ethernet]
? (192.168.1.105) at a:b:c:d:e:f on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
"""
    with patch("sys.platform", "darwin"), patch("os.name", "posix"):
        with patch("subprocess.check_output", return_value=macos_arp):
            table = NetSentinel.get_arp_table()
            assert "192.168.1.1" in table
            assert table["192.168.1.1"] == "00:11:22:33:44:55"
            assert "192.168.1.105" in table
            assert table["192.168.1.105"] == "0A:0B:0C:0D:0E:0F"
            # Multicast filtered out
            assert "224.0.0.251" not in table

    # 2. Linux /proc/net/arp parsing
    linux_proc_arp = """IP address       HW type     Flags       HW address            Mask     Device
192.168.1.1      0x1         0x2         00:50:56:c0:00:08     *        eth0
192.168.1.50     0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0
192.168.1.200    0x1         0x0         00:00:00:00:00:00     *        eth0
"""
    with patch("sys.platform", "linux"), patch("os.name", "posix"):
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=linux_proc_arp):
                table = NetSentinel.get_arp_table()
                assert "192.168.1.1" in table
                assert table["192.168.1.1"] == "00:50:56:C0:00:08"
                assert "192.168.1.50" in table
                assert table["192.168.1.50"] == "AA:BB:CC:DD:EE:FF"
                # Incomplete flag 0x0 filtered out
                assert "192.168.1.200" not in table


def test_cross_platform_gateway_discovery():
    """Verifies gateway parsing on Linux (/proc/net/route) and macOS (route -n get default)."""
    # 1. Linux /proc/net/route hex parsing (0101A8C0 in little-endian hex = 192.168.1.1)
    linux_route = """Iface	Destination	Gateway 	Flags	RefCnt	Use	Metric	Mask		MTU	Window	IRTT
eth0	00000000	0101A8C0	0003	0	0	100	00000000	0	0	0
"""
    with patch("sys.platform", "linux"):
        with patch("socket.socket") as mock_sock:
            mock_s = MagicMock()
            mock_s.getsockname.return_value = ("192.168.1.120", 0)
            mock_sock.return_value.__enter__.return_value = mock_s
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value=linux_route):
                    info = NetSentinel.get_local_network_info()
                    assert info["gateway_ip"] == "192.168.1.1"
                    assert info["host_ip"] == "192.168.1.120"
                    assert info["subnet_prefix"] == "192.168.1."

    # 2. macOS route -n get default
    macos_route_out = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.254
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
    with patch("sys.platform", "darwin"):
        with patch("socket.socket") as mock_sock:
            mock_s = MagicMock()
            mock_s.getsockname.return_value = ("192.168.1.45", 0)
            mock_sock.return_value.__enter__.return_value = mock_s
            with patch("subprocess.check_output", return_value=macos_route_out):
                info = NetSentinel.get_local_network_info()
                assert info["gateway_ip"] == "192.168.1.254"
                assert info["host_ip"] == "192.168.1.45"


def test_game_sentinel_cross_platform():
    """Verifies game detection and priority elevation without .exe extensions on POSIX."""
    from core.game_sentinel import GameSentinelManager, KNOWN_GAME_DIRS

    gsm = GameSentinelManager.get_instance()

    # Verify cross-platform directory paths exist
    assert "steamapps/common" in KNOWN_GAME_DIRS
    assert "epic games/" in KNOWN_GAME_DIRS

    # Verify priority elevation on POSIX calls proc.nice(-10)
    mock_proc = MagicMock()
    with patch("psutil.Process", return_value=mock_proc):
        # Emulate non-Windows where HIGH_PRIORITY_CLASS does not exist on psutil
        with patch("core.game_sentinel.hasattr", return_value=False):
            elevated = gsm.elevate_process_priority(1234)
            assert elevated is True
            mock_proc.nice.assert_called_with(-10)


def test_broadcast_online_notification():
    """Verifies UDP discovery beacon and SSDP M-SEARCH broadcast generation."""
    sentinel = get_net_sentinel()

    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ("192.168.1.100", 0)
        mock_sock_cls.return_value.__enter__.return_value = mock_sock

        res = sentinel.broadcast_online_notification(message="Saturn Test Beacon")
        assert res["success"] is True
        assert res["packets_sent"] > 0
        assert "255.255.255.255" in res["broadcast_targets"]
        assert res["message"] == "Saturn Test Beacon"
        assert mock_sock.sendto.call_count >= 1


def test_notify_single_device():
    """Verifies targeted unicast notification packet transmission and Smart TV probes."""
    sentinel = get_net_sentinel()

    with patch("socket.socket") as mock_sock_cls, \
         patch("urllib.request.urlopen") as mock_urlopen, \
         patch.object(sentinel, "ping_target", return_value={"success": True, "avg_latency_ms": 3.2}):

        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0  # Cast port open
        mock_sock_cls.return_value.__enter__.return_value = mock_sock

        # Mock Roku 200 OK
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = sentinel.notify_single_device("192.168.1.88", message="Hello Node")
        assert res["success"] is True
        assert res["target_ip"] == "192.168.1.88"
        assert res["udp_broadcast"] is True
        assert res["smart_tv_notified"] is True
        assert res["latency_ms"] == 3.2


def test_connect_all_devices_and_mesh_state():
    """Verifies connect_all_devices establishes mesh state and marks all devices as linked."""
    sentinel = get_net_sentinel()

    fake_devices = [
        {"ip": "192.168.1.1", "mac": "00:11:22:33:44:55", "vendor": "TP-Link", "device_type": "Router / Gateway"},
        {"ip": "192.168.1.75", "mac": "AA:BB:CC:DD:EE:FF", "vendor": "LG Electronics", "device_type": "Smart TV / Media Player"},
        {"ip": "192.168.1.120", "mac": "11:22:33:44:55:66", "vendor": "Apple", "device_type": "Mobile / Smartphone"},
    ]
    sentinel.last_scan_result = {
        "subnet": "192.168.1.0/24",
        "gateway_ip": "192.168.1.1",
        "devices": list(fake_devices),
    }

    with patch.object(sentinel, "broadcast_online_notification", return_value={"success": True, "broadcasts_sent": 4, "ssdp_msearch": True}), \
         patch.object(sentinel, "notify_single_device", return_value={"success": True, "target_ip": "192.168.1.1"}):

        res = sentinel.connect_all_devices(broadcast_message="Mesh Sync")
        assert res["success"] is True
        assert res["total_devices_linked"] == 3
        assert len(res["devices"]) == 3
        for dev in res["devices"]:
            assert dev["linked"] is True
            assert dev["link_status"] == "Connected"

        # Verify sentinel last scan result has mesh_linked flag
        assert sentinel.last_scan_result["mesh_linked"] is True
        assert "mesh_timestamp" in sentinel.last_scan_result


def test_intent_dispatcher_connect_all_reflexes():
    """Verifies instant reflex matching for zero-touch device mesh connection commands."""
    dispatcher = IntentDispatcher()

    queries = [
        "connect all devices",
        "connect to all my devices",
        "link network devices",
        "connect devices via network",
        "broadcast to all devices",
    ]

    for q in queries:
        r = dispatcher.match_instant_reflex(q)
        assert r is not None, f"Failed reflex match for query: '{q}'"
        assert r.intent == "net_sentinel"
        assert r.action == "connect_all_devices"


def test_radar_bridge_connect_all_and_notify():
    """Verifies RadarBridge slots trigger sentinel methods and push updates to overlay."""
    from core.radar_hud import RadarBridge

    mock_overlay = MagicMock()
    bridge = RadarBridge(mock_overlay)

    # Test connectAllDevices worker
    fake_connect_res = {"success": True, "total_devices_linked": 2}
    with patch.object(bridge._sentinel, "connect_all_devices", return_value=fake_connect_res):
        bridge._run_connect_all_worker()
        mock_overlay.set_mesh_connected.assert_called_with(fake_connect_res)

    # Test notifyDevice worker
    fake_notify_res = {"success": True, "target_ip": "192.168.1.99"}
    with patch.object(bridge._sentinel, "notify_single_device", return_value=fake_notify_res):
        bridge._run_notify_device_worker("192.168.1.99")
        mock_overlay.set_device_notified.assert_called_with("192.168.1.99", fake_notify_res)


def test_action_and_feature_connect_all_integration():
    """Verifies actions/net_sentinel.py and features/net_sentinel.py execute connect_all_devices."""
    sentinel = get_net_sentinel()

    fake_res = {
        "success": True,
        "total_devices_linked": 4,
        "devices": [{"ip": "192.168.1.1", "linked": True}],
        "message": "Connected with 4 devices.",
    }

    with patch.object(sentinel, "connect_all_devices", return_value=fake_res):
        # Test action plugin
        action_out = net_sentinel_action.run({"action": "connect_all_devices"})
        assert "Zero-Touch Device Mesh Connected" in action_out
        assert "4 devices" in action_out

        # Test dynamic feature
        feature_out = net_sentinel_feature.execute(action="connect_all_devices")
        assert feature_out["success"] is True
        assert feature_out["action"] == "connect_all_devices"


def test_mobile_companion_portal():
    """Verifies that the zero-install mobile companion portal HTML is generated with correct endpoints."""
    from saturn_connect.gateway.mobile_portal import get_mobile_portal_html
    html = get_mobile_portal_html(host_ip="192.168.0.112", port=8765)
    assert "SATURN" in html
    assert "Mobile Companion Node" in html
    assert "192.168.0.112:8765" in html
    assert "Notification.requestPermission" in html
    assert "connectWebSocket" in html




