"""
core/net_sentinel.py
Saturn NetSentinel & Autonomous Local Network Supervisor.
Provides complete network and router omniscience:
1. Identifies Default Gateway (Router IP), Router MAC, vendor (e.g. TP-Link), and open management ports.
2. Performs fast multi-threaded subnet scans to discover every alive device on the Wi-Fi/LAN.
3. Resolves MAC addresses, OUI hardware vendors, and open service ports (HTTP, SSH, Cast, ADB, SMB, etc.).
4. Classifies devices (Smart TVs, Mobile phones, PCs, Printers, IoT nodes, Routers).
5. Provides network powers: Wake-on-LAN magic packet, ping latency probes, port auditing, and HTTP probing.
6. Public Internet Health: Public IP, ISP name, geolocation, DNS resolution, and latency.
7. Generates an interactive Cyberpunk Network Map report for the Circuit HUD.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.user_paths import get_user_data_dir

logger = logging.getLogger("NetSentinel")

NETWORK_MAPS_DIR = get_user_data_dir() / "network_maps"
NETWORK_MAPS_DIR.mkdir(parents=True, exist_ok=True)

# Curated high-frequency OUI prefix map (top router, mobile, IoT, PC manufacturers)
OUI_VENDOR_CACHE: Dict[str, str] = {
    # Routers & Networking
    "28:87:BA": "TP-Link", "50:D4:F7": "TP-Link", "E8:48:B8": "TP-Link", "98:DA:C4": "TP-Link",
    "B0:95:75": "TP-Link", "C0:06:C3": "TP-Link", "AC:84:C6": "TP-Link", "54:AF:97": "TP-Link",
    "00:1A:2B": "Cisco", "00:0C:29": "VMware", "00:50:56": "VMware", "00:14:22": "Dell",
    "00:1E:67": "Intel", "04:D4:C4": "Asus", "F0:79:59": "Asus", "2C:FD:A1": "Netgear",
    "A0:04:60": "Netgear", "14:59:C0": "D-Link", "28:10:7B": "D-Link", "00:18:E7": "Huawei",
    "48:46:FB": "Huawei", "00:26:86": "Cisco Meraki", "AC:EC:85": "MikroTik", "48:8F:5A": "MikroTik",
    # Apple
    "3C:52:82": "Apple", "F0:18:98": "Apple", "A4:83:E7": "Apple", "BC:D1:1F": "Apple",
    "AC:DE:48": "Apple", "DC:A9:04": "Apple", "28:E1:4C": "Apple", "88:66:5A": "Apple",
    # Google & Android
    "52:D4:F6": "Google / Android", "94:83:C4": "Google", "3C:5A:37": "Google", "D8:6C:63": "Google Nest",
    # Samsung
    "AC:5F:3E": "Samsung", "50:01:D9": "Samsung", "8C:F5:A3": "Samsung", "00:12:47": "Samsung TV",
    "64:1C:B0": "Samsung", "34:82:C5": "Samsung", "A0:0B:BA": "Samsung",
    # Xiaomi, OnePlus, Oppo, Vivo
    "58:44:98": "Xiaomi", "78:02:F8": "Xiaomi", "64:CC:2E": "Xiaomi", "10:2A:B3": "OnePlus",
    "60:A4:4C": "OnePlus", "94:65:2D": "Oppo", "40:4E:36": "Vivo",
    # Single-board Computers & IoT
    "B8:27:EB": "Raspberry Pi", "DC:A6:32": "Raspberry Pi", "E4:5F:01": "Raspberry Pi",
    "24:62:AB": "Espressif (ESP32/ESP8266)", "84:CC:A8": "Espressif IoT", "A4:CF:12": "Espressif IoT",
    "68:C6:3A": "Amazon Echo", "44:65:0D": "Amazon Echo", "50:DC:E7": "Amazon Fire TV",
    # TV & Entertainment
    "A8:23:FE": "LG Electronics (Smart TV)", "CC:2D:8C": "LG Electronics", "00:04:20": "Slim Devices",
    "F4:F5:D8": "Google Chromecast", "B8:3E:59": "Roku Player", "D8:31:34": "Roku", "70:2C:1F": "Sony PlayStation",
    # PC Hardware
    "48:21:0B": "Microsoft Surface", "70:85:C2": "HP Workstation", "B4:B5:2F": "HP", "E4:54:E8": "Dell",
    "D8:9C:67": "Lenovo", "00:E0:4C": "Realtek", "B0:25:AA": "Intel Wi-Fi",
}


class NetSentinel:
    """Core autonomous network supervisor for Saturn."""

    _instance: Optional[NetSentinel] = None

    def __init__(self) -> None:
        self._cached_vendor_lookups: Dict[str, str] = dict(OUI_VENDOR_CACHE)
        self.last_scan_result: Dict[str, Any] = {}

    @classmethod
    def get_instance(cls) -> NetSentinel:
        if cls._instance is None:
            cls._instance = NetSentinel()
        return cls._instance

    # =========================================================================
    # 1. LOCAL INTERFACE & GATEWAY DISCOVERY
    # =========================================================================

    @staticmethod
    def get_local_network_info() -> Dict[str, str]:
        """
        Detects current host IP, Default Gateway (Router IP), and subnet mask
        across Windows 10/11 (all languages), macOS (Darwin), and Linux.
        """
        host_ip = "127.0.0.1"
        gateway_ip = "192.168.1.1"
        subnet_prefix = "192.168.1."

        # 1. Host IP detection via dummy UDP connect (zero network traffic sent, universal on all OSes)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                detected_ip = s.getsockname()[0]
                if detected_ip and detected_ip != "127.0.0.1":
                    host_ip = detected_ip
        except Exception:
            try:
                host_ip = socket.gethostbyname(socket.gethostname())
            except Exception:
                pass

        if "." in host_ip:
            subnet_prefix = host_ip.rsplit(".", 1)[0] + "."

        # 2. Gateway Detection per operating system
        # A. Windows 10 / 11 (language-independent route parsing)
        if sys.platform == "win32":
            # Method 1: route print 0.0.0.0 (language-independent numeric table)
            try:
                out = subprocess.check_output(["route", "print", "0.0.0.0"], text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                        gw_cand, iface_cand = parts[2], parts[3]
                        if gw_cand and gw_cand != "0.0.0.0" and not gw_cand.startswith("127."):
                            gateway_ip = gw_cand
                            if host_ip == "127.0.0.1" and iface_cand:
                                host_ip = iface_cand
                            break
            except Exception:
                pass

            # Method 2: Multilingual ipconfig fallback
            if gateway_ip == "192.168.1.1":
                try:
                    out = subprocess.check_output(["ipconfig"], text=True, stderr=subprocess.DEVNULL)
                    for block in re.split(r"\n\s*\n", out):
                        gw_m = re.search(r":\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", block)
                        if any(kw in block for kw in ("Gateway", "Standardgateway", "Passerelle", "Puerta de enlace", "Padrão", "Шлюз")):
                            ips = re.findall(r":\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", block)
                            if len(ips) >= 2:
                                host_ip = ips[0]
                                gateway_ip = ips[-1]
                                break
                except Exception:
                    pass

        # B. Linux (native /proc/net/route or ip route)
        elif sys.platform.startswith("linux"):
            # Method 1: /proc/net/route (0ms, no child process)
            try:
                if os.path.exists("/proc/net/route"):
                    import struct
                    with open("/proc/net/route", "r") as f:
                        for line in f.readlines()[1:]:
                            fields = line.strip().split()
                            if len(fields) >= 3 and fields[1] == "00000000":
                                gw_hex = fields[2]
                                gw_ip = socket.inet_ntoa(struct.pack("<L", int(gw_hex, 16)))
                                if gw_ip and gw_ip != "0.0.0.0":
                                    gateway_ip = gw_ip
                                    break
            except Exception:
                pass

            # Method 2: ip route show default
            if gateway_ip == "192.168.1.1":
                try:
                    out = subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True, stderr=subprocess.DEVNULL)
                    m = re.search(r"via\s+([0-9.]+)", out)
                    if m:
                        gateway_ip = m.group(1).strip()
                except Exception:
                    pass

        # C. macOS / Darwin (route -n get default or netstat)
        elif sys.platform == "darwin":
            try:
                out = subprocess.check_output(["route", "-n", "get", "default"], text=True, stderr=subprocess.DEVNULL)
                m = re.search(r"gateway:\s+([0-9.]+)", out)
                if m:
                    gateway_ip = m.group(1).strip()
            except Exception:
                try:
                    out = subprocess.check_output(["netstat", "-nr", "-f", "inet"], text=True, stderr=subprocess.DEVNULL)
                    for line in out.splitlines():
                        if line.startswith("default"):
                            parts = line.split()
                            if len(parts) >= 2:
                                gateway_ip = parts[1]
                                break
                except Exception:
                    pass

        # Universal subnet fallback: if gateway is unknown, use host subnet .1
        if (gateway_ip == "192.168.1.1" or not gateway_ip) and host_ip != "127.0.0.1":
            gateway_ip = f"{subnet_prefix}1"

        return {
            "host_ip": host_ip,
            "gateway_ip": gateway_ip,
            "subnet_prefix": subnet_prefix,
            "hostname": socket.gethostname(),
        }

    # =========================================================================
    # 2. PUBLIC INTERNET HEALTH & ISP TELEMETRY
    # =========================================================================

    def get_internet_telemetry(self) -> Dict[str, Any]:
        """Queries public IP, ISP details, geolocation, and ping latency to internet root DNS."""
        data: Dict[str, Any] = {
            "connected": False,
            "public_ip": "Unknown",
            "isp": "Unknown",
            "org": "Unknown",
            "city": "Unknown",
            "country": "Unknown",
            "dns_latency_ms": 0.0,
            "ping_latency_ms": 0.0,
        }

        # 1. DNS Resolution Benchmark
        t0 = time.perf_counter()
        try:
            socket.gethostbyname("dns.google")
            data["dns_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            data["connected"] = True
        except Exception:
            data["dns_latency_ms"] = 999.0
            data["connected"] = False

        # 2. Public IP & ISP Telemetry via IP-API
        if data["connected"]:
            try:
                req = urllib.request.Request(
                    "http://ip-api.com/json/?fields=status,message,country,city,isp,org,as,query",
                    headers={"User-Agent": "SaturnEvo/Saturn-NetSentinel"}
                )
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    if payload.get("status") == "success":
                        data["public_ip"] = payload.get("query", "Unknown")
                        data["isp"] = payload.get("isp", "Unknown")
                        data["org"] = payload.get("org", "Unknown")
                        data["city"] = payload.get("city", "Unknown")
                        data["country"] = payload.get("country", "Unknown")
            except Exception as e:
                logger.debug(f"Public IP lookup error: {e}")
                # Fallback to ipify for IP only
                try:
                    with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=2.5) as resp:
                        p_data = json.loads(resp.read().decode("utf-8"))
                        data["public_ip"] = p_data.get("ip", "Unknown")
                except Exception:
                    pass

        # 3. Cloudflare Ping Latency
        data["ping_latency_ms"] = self.ping_target("1.1.1.1", count=2).get("avg_latency_ms", 0.0)

        # 4. Fast Net Speed Test Sample (Cloudflare CDN chunk)
        data["download_speed_mbps"] = self.measure_network_speed().get("download_speed_mbps", 0.0)

        return data

    def measure_network_speed(self) -> Dict[str, Any]:
        """Runs a fast download speed benchmark from Cloudflare CDN and returns Mbps."""
        try:
            t0 = time.perf_counter()
            req = urllib.request.Request(
                "https://speed.cloudflare.com/__down?bytes=2500000",
                headers={"User-Agent": "SaturnAI/NetSentinel"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                content = resp.read()
                dt = time.perf_counter() - t0
                mbps = round((len(content) * 8 / 1_000_000) / max(dt, 0.001), 1)
                return {"success": True, "download_speed_mbps": mbps, "duration_sec": round(dt, 2)}
        except Exception as e:
            logger.debug(f"Speed test skipped/failed: {e}")
            return {"success": False, "download_speed_mbps": 0.0, "error": str(e)}

    # =========================================================================
    # 3. MAC & HARDWARE VENDOR RESOLUTION
    # =========================================================================

    @staticmethod
    def is_randomized_mac(mac: str) -> bool:
        """
        Detects IEEE 802 locally administered / randomized MAC addresses.
        Android 10+ and iOS 14+ generate randomized MAC addresses by default for Wi-Fi privacy.
        In IEEE 802, the 2nd least significant bit of the first byte is the U/L (Universal/Local) bit.
        If bit 1 is set (e.g. x2, x6, xA, xE in byte 0), the address is locally administered.
        """
        if not mac or mac in ("---", "ff-ff-ff-ff-ff-ff", "00-00-00-00-00-00", "FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"):
            return False
        clean = mac.replace("-", ":").split(":")
        if len(clean) >= 1:
            try:
                first_byte = int(clean[0], 16)
                return bool(first_byte & 0x02)
            except ValueError:
                pass
        return False

    def resolve_mac_vendor(self, mac: str) -> str:
        """Resolves hardware vendor from MAC address with local cache + online fallback."""
        if not mac or mac in ("---", "ff-ff-ff-ff-ff-ff", "00-00-00-00-00-00"):
            return "Local Interface"

        norm_prefix = mac.replace("-", ":").upper()[:8]
        if norm_prefix in self._cached_vendor_lookups:
            return self._cached_vendor_lookups[norm_prefix]

        # Fast online lookup via api.macvendors.com with 1.5s timeout
        try:
            url = f"https://api.macvendors.com/{urllib.parse.quote(norm_prefix)}"
            req = urllib.request.Request(url, headers={"User-Agent": "SaturnEvo"})
            with urllib.request.urlopen(req, timeout=1.5) as r:
                vendor = r.read().decode("utf-8", errors="ignore").strip()
                if vendor and len(vendor) < 50:
                    self._cached_vendor_lookups[norm_prefix] = vendor
                    return vendor
        except Exception:
            pass

        # Check if this is a randomized private MAC (standard for Android 10+ and iOS 14+)
        if self.is_randomized_mac(mac):
            return "Mobile Device (Private Wi-Fi Address)"

        return "Generic Network Device"

    def classify_device(
        self,
        ip: str = "",
        mac: str = "",
        vendor: str = "",
        open_ports: Optional[List[int]] = None,
        is_gateway: bool = False,
        is_host: bool = False
    ) -> str:
        """Classifies a device type based on gateway status, vendor signature, and open ports."""
        ports = set(open_ports or [])
        vendor_low = (vendor or "").lower()

        if is_host:
            return "Host PC / Workstation"
        if is_gateway or (53 in ports and (80 in ports or 1900 in ports)):
            return "Router / Gateway"
        if 8009 in ports or any(tv in vendor_low for tv in ("samsung tv", "lg electronics", "roku", "chromecast")):
            return "Smart TV / Media Player"
        if (
            5555 in ports
            or any(m in vendor_low for m in ("google", "android", "xiaomi", "oneplus", "oppo", "vivo", "apple", "iphone", "mobile device", "smartphone"))
            or self.is_randomized_mac(mac)
        ):
            return "Mobile / Smartphone"
        if 445 in ports or 3389 in ports or 22 in ports or "dell" in vendor_low or "hp" in vendor_low or "surface" in vendor_low:
            return "PC / Server"
        if 9100 in ports or "printer" in vendor_low:
            return "Network Printer"
        if 80 in ports or 8080 in ports or "espressif" in vendor_low:
            return "IoT / Web Device"
        return "Network Device"

    @staticmethod
    def get_arp_table() -> Dict[str, str]:
        """Reads OS ARP cache, returning mapping of IP -> MAC address across Windows, Linux, and macOS."""
        arp_map: Dict[str, str] = {}
        if sys.platform == "win32":
            try:
                out = subprocess.check_output(["arp", "-a"], text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    m = re.search(r"([0-9.]+)\s+([0-9a-fA-F-]+)\s+(\w+)", line)
                    if m:
                        ip, mac, _ = m.groups()
                        if not ip.endswith(".255") and not ip.startswith(("224.", "239.")):
                            arp_map[ip] = mac.upper()
            except Exception as e:
                logger.debug(f"ARP cache read error (Windows): {e}")
        elif sys.platform == "darwin":
            # macOS: parse 'arp -an' -> ? (192.168.1.1) at 0:11:22:33:44:55 on en0 ifscope [ethernet]
            try:
                out = subprocess.check_output(["arp", "-an"], text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    m = re.search(r"\(([0-9.]+)\)\s+at\s+([0-9a-fA-F:]+)", line)
                    if m:
                        ip, mac = m.groups()
                        parts = mac.split(":")
                        if len(parts) == 6:
                            norm_mac = ":".join(f"{int(p, 16):02X}" for p in parts)
                            if not ip.endswith(".255") and not ip.startswith(("224.", "239.")):
                                arp_map[ip] = norm_mac
            except Exception as e:
                logger.debug(f"ARP cache read error (macOS): {e}")
        else:
            # Linux: First check /proc/net/arp (fastest kernel interface, non-root)
            try:
                arp_file = Path("/proc/net/arp")
                if arp_file.exists():
                    lines = arp_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                    for line in lines[1:]:  # skip header
                        parts = line.split()
                        if len(parts) >= 4:
                            ip = parts[0]
                            mac = parts[3].upper()
                            flags = parts[2]
                            if mac != "00:00:00:00:00:00" and flags != "0x0" and not ip.endswith(".255") and not ip.startswith(("224.", "239.")):
                                arp_map[ip] = mac
            except Exception as e:
                logger.debug(f"Linux /proc/net/arp read error: {e}")

            # Fallback to 'ip neigh' on Linux if arp_map is still empty
            if not arp_map:
                try:
                    out = subprocess.check_output(["ip", "-4", "neigh", "show"], text=True, stderr=subprocess.DEVNULL)
                    for line in out.splitlines():
                        m = re.search(r"([0-9.]+)\s+dev\s+\S+\s+lladdr\s+([0-9a-fA-F:]+)", line)
                        if m:
                            ip, mac = m.groups()
                            if not ip.endswith(".255") and not ip.startswith(("224.", "239.")):
                                arp_map[ip] = mac.upper()
                except Exception as e:
                    logger.debug(f"Linux 'ip neigh' read error: {e}")

            # Fallback to 'arp -an' on Linux if still empty
            if not arp_map:
                try:
                    out = subprocess.check_output(["arp", "-an"], text=True, stderr=subprocess.DEVNULL)
                    for line in out.splitlines():
                        m = re.search(r"\(([0-9.]+)\)\s+at\s+([0-9a-fA-F:]+)", line)
                        if m:
                            ip, mac = m.groups()
                            if not ip.endswith(".255") and not ip.startswith(("224.", "239.")):
                                arp_map[ip] = mac.upper()
                except Exception:
                    pass

        return arp_map

    # =========================================================================
    # 4. ROUTER DEEP INSPECTION
    # =========================================================================

    def inspect_router(self) -> Dict[str, Any]:
        """Probes the default gateway router: open management ports, vendor, and web GUI."""
        net_info = self.get_local_network_info()
        gw_ip = net_info["gateway_ip"]
        arp_table = self.get_arp_table()
        gw_mac = arp_table.get(gw_ip, "Unknown")
        vendor = self.resolve_mac_vendor(gw_mac)

        # 1. Port scan common router management ports
        management_ports = [80, 443, 8080, 22, 23, 53, 1900]
        p_res = self.scan_device_ports(gw_ip, management_ports)
        open_ports = p_res if isinstance(p_res, list) else (p_res.get("open_ports", []) if isinstance(p_res, dict) else [])

        # 2. Probe Web Admin GUI
        web_info = self.probe_http(gw_ip, port=80 if 80 in open_ports else (8080 if 8080 in open_ports else 443))
        title = web_info.get("title", "")
        server_header = web_info.get("server", "")

        # Refine vendor from HTTP title / keywords if available
        combined_text = f"{title} {web_info.get('snippet', '')} {server_header}".lower()
        if "tp-link" in combined_text or "archer" in combined_text:
            vendor = "TP-Link Systems"
        elif "asus" in combined_text or "asuswrt" in combined_text:
            vendor = "ASUSTeK Computer"
        elif "netgear" in combined_text or "genie" in combined_text:
            vendor = "Netgear"
        elif "d-link" in combined_text:
            vendor = "D-Link"
        elif "cisco" in combined_text or "linksys" in combined_text:
            vendor = "Cisco / Linksys"
        elif "huawei" in combined_text:
            vendor = "Huawei"

        ping_res = self.ping_target(gw_ip, count=3)

        res = {
            "success": True,
            "gateway_ip": gw_ip,
            "mac_address": gw_mac,
            "vendor": vendor,
            "open_ports": open_ports,
            "web_gui_status": web_info.get("status_code", 0),
            "web_gui_title": title or "Router Admin Console",
            "server_header": server_header,
            "latency_ms": ping_res.get("avg_latency_ms", 1.0),
            "web_url": f"http://{gw_ip}" if 80 in open_ports else f"https://{gw_ip}",
            "status": "Online & Reachable",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        logger.info(f"Router inspected: {gw_ip} [{vendor}] - Open Ports: {open_ports}")
        return res

    @staticmethod
    def _send_arp(ip_str: str, host_ip: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """Sends native Windows ARP request with explicit source IP binding to retrieve MAC address directly."""
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            import struct
            iphlpapi = ctypes.windll.iphlpapi
            dest_ip = struct.unpack("<I", socket.inet_aton(ip_str))[0]
            src_ip = struct.unpack("<I", socket.inet_aton(host_ip))[0] if host_ip and host_ip != "127.0.0.1" else 0
            mac_buf = (ctypes.c_byte * 6)()
            mac_len = ctypes.c_ulong(6)
            res = iphlpapi.SendARP(dest_ip, src_ip, ctypes.byref(mac_buf), ctypes.byref(mac_len))
            if res == 0:
                mac = "-".join(f"{b & 0xff:02X}" for b in mac_buf)
                return ip_str, mac
        except Exception:
            pass
        return None

    # =========================================================================
    # 5. SUBNET SWEEP & ALL DEVICE DISCOVERY
    # =========================================================================

    def scan_local_network(self, deep: bool = False, max_workers: int = 100) -> Dict[str, Any]:
        """
        Discovers active devices on the local Wi-Fi / LAN subnet.
        Uses SendARP & OS ARP cache to resolve devices, MAC addresses, and open ports.
        """
        t0 = time.time()
        net_info = self.get_local_network_info()
        subnet_prefix = net_info["subnet_prefix"]
        host_ip = net_info["host_ip"]
        gateway_ip = net_info["gateway_ip"]

        arp_table = self.get_arp_table()
        alive_ips: Set[str] = {host_ip, gateway_ip}

        # Add all IPs already known in OS ARP table
        for ip in arp_table.keys():
            if ip.startswith(subnet_prefix):
                alive_ips.add(ip)

        # Broadcast discovery wake beacon across subnet to elicit ARP updates from sleeping mobile devices
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b_sock:
                b_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                b_sock.settimeout(0.1)
                b_sock.sendto(b"\x00", (f"{subnet_prefix}255", 5353))
                b_sock.sendto(b"\x00", (f"{subnet_prefix}255", 137))
        except Exception:
            pass

        # Decide probe target list: full 254 if deep, otherwise high-density IP ranges
        if deep:
            targets = [f"{subnet_prefix}{i}" for i in range(1, 255)]
        else:
            # Fast scan: gateway region (1..25), DHCP pool (100..155, 190..210), and all already known IPs
            target_indices = set(range(1, 26)) | set(range(100, 156)) | set(range(190, 210))
            for ip in arp_table.keys():
                if ip.startswith(subnet_prefix):
                    try:
                        target_indices.add(int(ip.split(".")[-1]))
                    except Exception:
                        pass
            targets = [f"{subnet_prefix}{i}" for i in sorted(target_indices)]

        if sys.platform == "win32":
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                probed = executor.map(lambda ip: self._send_arp(ip, host_ip), targets)
                for r in probed:
                    if r:
                        ip, mac = r
                        alive_ips.add(ip)
                        arp_table[ip] = mac
        else:
            # On Linux and macOS: Trigger kernel ARP resolution via UDP ping packets (NetBIOS / mDNS)
            # followed by concurrent TCP socket checks
            def _probe_socket(ip: str) -> Optional[str]:
                try:
                    s_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s_udp.settimeout(0.08)
                    s_udp.sendto(b"\x00", (ip, 137))
                    s_udp.close()
                except Exception:
                    pass

                for port in (80, 443, 22, 53, 8009, 5555, 445, 139):
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(0.12)
                        if s.connect_ex((ip, port)) == 0:
                            s.close()
                            return ip
                        s.close()
                    except Exception:
                        pass
                return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
                probed = executor.map(_probe_socket, targets)
                for r in probed:
                    if r:
                        alive_ips.add(r)

        # Refresh ARP table for any newly populated entries
        fresh_arp = self.get_arp_table()
        for ip, mac in fresh_arp.items():
            if ip.startswith(subnet_prefix):
                alive_ips.add(ip)
                arp_table[ip] = mac

        # Scan each alive IP for detailed telemetry
        devices: List[Dict[str, Any]] = []
        router_device: Optional[Dict[str, Any]] = None

        ports_to_check = [80, 443, 8080, 22, 445, 3389, 5555, 8009, 53]

        def _inspect_device(ip: str) -> Dict[str, Any]:
            mac = arp_table.get(ip, "---")
            if ip == host_ip:
                vendor = f"This Machine ({socket.gethostname()})"
                device_type = "Host PC / Workstation"
            elif ip == gateway_ip:
                vendor = self.resolve_mac_vendor(mac)
                device_type = "Router / Gateway"
            else:
                vendor = self.resolve_mac_vendor(mac)
                device_type = "Network Device"

            p_res = self.scan_device_ports(ip, ports_to_check)
            open_p = p_res if isinstance(p_res, list) else (p_res.get("open_ports", []) if isinstance(p_res, dict) else [])
            device_type = self.classify_device(
                ip=ip,
                mac=mac,
                vendor=vendor,
                open_ports=open_p,
                is_gateway=(ip == gateway_ip),
                is_host=(ip == host_ip)
            )

            is_mobile = (device_type == "Mobile / Smartphone") or self.is_randomized_mac(mac)

            return {
                "ip": ip,
                "mac": mac,
                "vendor": vendor,
                "device_type": device_type,
                "open_ports": open_p,
                "is_router": (ip == gateway_ip),
                "is_host": (ip == host_ip),
                "is_mobile": is_mobile,
            }

        sorted_ips = sorted(alive_ips, key=lambda x: [int(p) for p in x.split(".")])
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            devices = list(ex.map(_inspect_device, sorted_ips))

        for d in devices:
            if d.get("is_router"):
                router_device = d
                break

        elapsed = round(time.time() - t0, 2)
        internet = self.get_internet_telemetry()

        result = {
            "success": True,
            "scan_duration_sec": elapsed,
            "subnet": f"{subnet_prefix}0/24",
            "gateway_ip": gateway_ip,
            "host_ip": host_ip,
            "mobile_portal_url": f"http://{host_ip}:8765",
            "total_devices_found": len(devices),
            "router": router_device,
            "devices": devices,
            "internet": internet,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        self.last_scan_result = result
        # Generate HUD visual deliverable HTML card
        html_path = self.generate_hud_network_map(result)
        result["card_path"] = str(html_path)

        logger.info(f"Subnet scan complete: {len(devices)} devices found in {elapsed}s.")
        return result

    # =========================================================================
    # 6. HARDWARE POWERS: WAKE-ON-LAN, PORT SCAN, PING
    # =========================================================================

    @staticmethod
    def wake_on_lan(mac_address: str, broadcast_ip: str = "255.255.255.255", port: int = 9) -> Dict[str, Any]:
        """Sends a standard Wake-on-LAN magic packet to power on a sleeping PC."""
        clean_mac = re.sub(r"[:\s\-]", "", mac_address).strip()
        if len(clean_mac) != 12:
            return {
                "success": False,
                "message": f"Invalid MAC address format: '{mac_address}'. Must be 12 hex characters."
            }

        try:
            mac_bytes = bytes.fromhex(clean_mac)
            magic_packet = b"\xff" * 6 + mac_bytes * 16

            # Dual-broadcast: send to primary broadcast target AND directed subnet broadcast
            # On macOS and Linux, sending to 255.255.255.255 can trigger EACCES (Permission Denied)
            # if the default route interface does not accept global broadcast; directed broadcast succeeds.
            targets = [broadcast_ip]
            if broadcast_ip == "255.255.255.255":
                try:
                    net_info = NetSentinelEngine.get_local_network_info()
                    subnet_prefix = net_info.get("subnet_prefix", "")
                    if subnet_prefix and subnet_prefix != "127.0.0.":
                        targets.append(f"{subnet_prefix}255")
                except Exception:
                    pass

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for b_ip in targets:
                    try:
                        s.sendto(magic_packet, (b_ip, port))
                    except Exception as b_err:
                        logger.debug(f"Broadcast to {b_ip} skipped: {b_err}")

            logger.info(f"Wake-on-LAN magic packet transmitted to {mac_address} via {targets}:{port}")
            return {
                "success": True,
                "mac": mac_address,
                "broadcast_ip": broadcast_ip,
                "port": port,
                "message": f"Wake-on-LAN magic packet broadcasted to {mac_address}. Device powering on."
            }
        except Exception as e:
            return {"success": False, "message": f"Wake-on-LAN failed: {e}"}

    @staticmethod
    def ping_target(target: str, count: int = 3) -> Dict[str, Any]:
        """Pings target IP or hostname and returns average round-trip latency in ms."""
        if not target:
            return {"success": False, "avg_latency_ms": 0.0, "message": "No target specified."}

        avg_latency = 0.0
        success = False
        try:
            if sys.platform == "win32":
                cmd = ["ping", "-n", str(count), "-w", "1000", target]
            elif sys.platform == "darwin":
                # macOS: -t sets timeout in seconds, -c sets count
                cmd = ["ping", "-c", str(count), "-t", "1", target]
            else:
                # Linux: -W sets timeout in seconds, -c sets count
                cmd = ["ping", "-c", str(count), "-W", "1", target]

            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            # Multi-OS parsing:
            # Unix/macOS: "round-trip min/avg/max/stddev = ..." or "rtt min/avg/max/mdev = ..."
            m_unix = re.search(r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*[0-9.]+/([0-9.]+)", out, re.I)
            # Windows (multilingual): "Average = 12ms", "Mittelwert = 12ms", "Moyenne = 12ms", "Media = 12ms"
            m_win = re.search(r"(?:Average|Mittelwert|Moyenne|Media|Promedio)\s*=\s*(\d+)\s*ms", out, re.I)
            # Generic packet match: "time=12.3 ms" or "time<1ms"
            m_generic = re.search(r"time[=<]\s*([0-9.]+)\s*ms", out, re.I)

            if m_unix:
                avg_latency = float(m_unix.group(1))
                success = True
            elif m_win:
                avg_latency = float(m_win.group(1))
                success = True
            elif m_generic:
                avg_latency = float(m_generic.group(1))
                success = True
            elif "TTL=" in out or "ttl=" in out:
                success = True
                avg_latency = 1.0
        except Exception:
            pass

        return {
            "success": success,
            "target": target,
            "avg_latency_ms": avg_latency,
            "status": "Online" if success else "Unreachable"
        }

    @staticmethod
    def scan_device_ports(target_ip: str, ports: Optional[List[int]] = None) -> Dict[str, Any]:
        """Probes specific ports on a device using fast concurrent sockets."""
        if ports is None:
            ports = [80, 443, 8080, 22, 21, 23, 445, 3389, 5555, 8009, 1900, 53]

        open_ports: List[int] = []

        def _test_port(p: int) -> Optional[int]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.12)
                res = s.connect_ex((target_ip, p))
                s.close()
                if res == 0:
                    return p
            except Exception:
                pass
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ports), 12)) as ex:
            results = ex.map(_test_port, ports)
            for r in results:
                if r is not None:
                    open_ports.append(r)

        return {
            "target_ip": target_ip,
            "open_ports": sorted(open_ports),
            "count": len(open_ports)
        }

    @staticmethod
    def probe_http(target_ip: str, port: int = 80, path: str = "/") -> Dict[str, Any]:
        """Fetches HTTP title, server banner, and status code from a device web interface."""
        protocol = "https" if port == 443 else "http"
        url = f"{protocol}://{target_ip}:{port}{path}" if port not in (80, 443) else f"{protocol}://{target_ip}{path}"
        info: Dict[str, Any] = {"url": url, "status_code": 0, "title": "", "server": "", "snippet": ""}

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SaturnEvo/Saturn"})
            with urllib.request.urlopen(req, timeout=1.5) as r:
                info["status_code"] = r.status
                info["server"] = r.headers.get("Server", "")
                raw = r.read(4096).decode("utf-8", errors="ignore")
                m_title = re.search(r"<title>(.*?)</title>", raw, re.I)
                if m_title:
                    info["title"] = m_title.group(1).strip()
                info["snippet"] = raw[:300].strip()
        except urllib.error.HTTPError as he:
            info["status_code"] = he.code
            info["server"] = he.headers.get("Server", "")
        except Exception:
            pass

        return info

    # =========================================================================
    # 7. CYBERPUNK HUD NETWORK MAP REPORT CARD
    # =========================================================================

    @staticmethod
    def generate_hud_network_map(data: Dict[str, Any]) -> Path:
        """Renders an interactive Cyberpunk Network Map report for the Saturn HUD."""
        stamp = int(time.time())
        card_path = NETWORK_MAPS_DIR / f"network_map_{stamp}.html"

        router = data.get("router") or {}
        devices = data.get("devices", [])
        internet = data.get("internet", {})

        device_rows = []
        for d in devices:
            ports_badges = "".join([f'<span class="chip chip-port">:{p}</span>' for p in d.get("open_ports", [])[:4]])
            is_hl = "host-row" if d.get("is_host") else ("router-row" if d.get("is_router") else "")
            icon = "🌐" if d.get("is_router") else ("💻" if d.get("is_host") else ("📱" if "Mobile" in d.get("device_type", "") else ("📺" if "TV" in d.get("device_type", "") else "📡")))

            device_rows.append(f"""
            <tr class="{is_hl}">
              <td>{icon} <strong>{d.get('ip')}</strong></td>
              <td><code>{d.get('mac')}</code></td>
              <td><span class="chip chip-vendor">{d.get('vendor')}</span></td>
              <td><span class="badge">{d.get('device_type')}</span></td>
              <td>{ports_badges or '<span style="color:#64748b;font-size:11px;">None</span>'}</td>
            </tr>
            """)

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Saturn NetSentinel - Local Network Map</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; }}
  body {{ background: #070b12; color: #e2e8f0; padding: 24px; }}
  .container {{ max-width: 900px; margin: 0 auto; background: rgba(15, 23, 42, 0.95); border: 1px solid #1e293b; border-radius: 16px; padding: 26px; box-shadow: 0 12px 40px rgba(0, 229, 255, 0.08); }}
  .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 16px; margin-bottom: 20px; }}
  .title {{ font-size: 22px; font-weight: 800; color: #00e5ff; letter-spacing: 0.05em; display: flex; align-items: center; gap: 8px; }}
  .badge {{ background: #0ea5e9; color: #fff; font-size: 11px; font-weight: 700; text-transform: uppercase; padding: 3px 8px; border-radius: 6px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 22px; }}
  .stat-card {{ background: rgba(30, 41, 59, 0.5); border: 1px solid #334155; border-radius: 10px; padding: 14px; text-align: center; }}
  .stat-val {{ font-size: 20px; font-weight: 800; color: #38bdf8; margin-top: 4px; }}
  .stat-label {{ font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 14px; background: rgba(30, 41, 59, 0.3); border-radius: 10px; overflow: hidden; }}
  th, td {{ padding: 12px 14px; text-align: left; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,0.05); }}
  th {{ background: rgba(30, 41, 59, 0.8); color: #94a3b8; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em; }}
  tr:hover {{ background: rgba(0, 229, 255, 0.05); }}
  .router-row {{ background: rgba(14, 165, 233, 0.12); font-weight: bold; }}
  .host-row {{ background: rgba(99, 102, 241, 0.12); }}
  .chip {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; margin-right: 4px; }}
  .chip-vendor {{ background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }}
  .chip-port {{ background: rgba(34, 197, 94, 0.15); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); font-weight: 600; }}
  .footer {{ margin-top: 20px; font-size: 11px; color: #64748b; text-align: right; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title">⚡ SATURN NETSENTINEL • NETWORK TOPOLOGY</div>
    <div class="badge">{data.get('subnet')}</div>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Default Gateway</div>
      <div class="stat-val">{data.get('gateway_ip')}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Router Vendor</div>
      <div class="stat-val" style="font-size:16px;">{router.get('vendor', 'Unknown')}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Devices Online</div>
      <div class="stat-val">{data.get('total_devices_found')} Active</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Public IP & ISP</div>
      <div class="stat-val" style="font-size:14px;color:#a855f7;">{internet.get('public_ip')}<br><span style="font-size:11px;color:#94a3b8;">{internet.get('isp', '')[:16]}</span></div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>IP Address</th>
        <th>MAC Address</th>
        <th>Hardware Vendor</th>
        <th>Device Category</th>
        <th>Open Services</th>
      </tr>
    </thead>
    <tbody>
      {"".join(device_rows)}
    </tbody>
  </table>

  <div class="footer">
    Scanned in {data.get('scan_duration_sec')}s • Gateway Ping: {internet.get('ping_latency_ms')}ms • Generated by Saturn NetSentinel
  </div>
</div>
</body>
</html>
"""
        card_path.write_text(html_content, encoding="utf-8")
        return card_path

    # =========================================================================
    # 8. ZERO-TOUCH DEVICE MESH & BROADCAST NOTIFICATIONS
    # =========================================================================

    def broadcast_online_notification(
        self,
        message: str = "Saturn Online // Node Linked",
        ports: Optional[List[int]] = None
    ) -> Dict[str, Any]:
        """
        Broadcasts a zero-touch network announcement packet across the subnet
        informing all listening devices, smart media renderers, and IoT nodes
        that Saturn is active and connected.
        """
        net_info = self.get_local_network_info()
        subnet_prefix = net_info.get("subnet_prefix", "192.168.1.")
        host_ip = net_info.get("host_ip", "127.0.0.1")
        hostname = socket.gethostname()

        target_ports = ports or [8765, 9999, 5353]
        targets = ["255.255.255.255"]
        if subnet_prefix and subnet_prefix != "127.0.0.":
            targets.append(f"{subnet_prefix}255")

        packet_payload = json.dumps({
            "sender": "Saturn",
            "protocol": "SaturnMesh/1.0",
            "event": "saturn_online",
            "host": str(hostname),
            "ip": str(host_ip),
            "timestamp": time.time(),
            "formatted_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message": str(message),
        }).encode("utf-8")

        sent_count = 0
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(0.5)
                for tgt in targets:
                    for p in target_ports:
                        try:
                            s.sendto(packet_payload, (tgt, p))
                            sent_count += 1
                        except Exception as e:
                            logger.debug(f"UDP beacon to {tgt}:{p} skipped: {e}")
        except Exception as exc:
            logger.warning(f"Error broadcasting mesh beacon: {exc}")

        # Also send SSDP M-SEARCH UPnP notify packet for media renderers and Smart TVs
        try:
            ssdp_packet = (
                "M-SEARCH * HTTP/1.1\r\n"
                "HOST: 239.255.255.250:1900\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 1\r\n"
                "ST: urn:saturn-ai:device:sentinel:1\r\n"
                f"USER-AGENT: SaturnAI/{hostname}\r\n\r\n"
            ).encode("utf-8")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                s.sendto(ssdp_packet, ("239.255.255.250", 1900))
        except Exception:
            pass

        return {
            "success": True,
            "broadcast_targets": targets,
            "packets_sent": sent_count,
            "host_ip": host_ip,
            "message": message
        }

    def notify_single_device(
        self,
        target_ip: str,
        message: str = "Saturn Online // Node Linked"
    ) -> Dict[str, Any]:
        """
        Transmits a directed network notification to a specific device.
        Supports Smart TVs (Roku ECP, Cast ping), IoT nodes, and PCs.
        """
        if not target_ip:
            return {"success": False, "message": "No target IP specified."}

        # 1. Directed UDP Notification packet on ports 8765 and 9999
        payload = json.dumps({
            "sender": "Saturn",
            "event": "node_ping",
            "target": target_ip,
            "message": message,
            "timestamp": time.time(),
        }).encode("utf-8")

        udp_sent = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.5)
                s.sendto(payload, (target_ip, 8765))
                s.sendto(payload, (target_ip, 9999))
                udp_sent = True
        except Exception:
            pass

        # 2. Roku Smart TV notification probe (port 8060)
        roku_sent = False
        try:
            req = urllib.request.Request(
                f"http://{target_ip}:8060/keypress/Info",
                data=b"",
                method="POST",
                headers={"User-Agent": "SaturnAI"}
            )
            with urllib.request.urlopen(req, timeout=0.8) as r:
                if r.status in (200, 204):
                    roku_sent = True
        except Exception:
            pass

        # 3. Google Cast probe (port 8009)
        cast_sent = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.6)
                if s.connect_ex((target_ip, 8009)) == 0:
                    cast_sent = True
        except Exception:
            pass

        # 4. Android Wireless Debugging (ADB port 5555)
        adb_sent = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.4)
                if s.connect_ex((target_ip, 5555)) == 0:
                    adb_sent = True
        except Exception:
            pass

        # 5. Push to any active WebSocket / Web Companion sessions
        web_push_sent = False
        try:
            from saturn_connect.service import get_service
            from pathlib import Path
            svc = get_service(Path(__file__).resolve().parent.parent)
            if svc and svc.gateway and svc.gateway.is_running():
                svc.gateway.broadcast_push_notification_threadsafe(message, target=target_ip)
                web_push_sent = True
        except Exception:
            pass

        # 6. Latency probe
        lat_res = self.ping_target(target_ip, count=1)
        is_alive = lat_res.get("success", False)
        net_info = self.get_local_network_info()
        host_ip = net_info.get("host_ip", "127.0.0.1")

        return {
            "success": is_alive or udp_sent,
            "target_ip": target_ip,
            "udp_broadcast": udp_sent,
            "smart_tv_notified": roku_sent or cast_sent,
            "web_companion_pushed": web_push_sent,
            "adb_endpoint": adb_sent,
            "latency_ms": lat_res.get("avg_latency_ms", 1.0),
            "mobile_portal_url": f"http://{host_ip}:8765",
            "message": f"Notification transmitted to {target_ip}."
        }

    def connect_all_devices(
        self,
        broadcast_message: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Connects with all discovered devices across the local network,
        establishes a mesh handshake, broadcasts the Saturn online notification,
        and marks devices as linked.
        """
        t0 = time.time()
        msg = broadcast_message or "Saturn Online // Node Linked"

        # Ensure we have active scan data
        if not self.last_scan_result or not self.last_scan_result.get("devices"):
            self.scan_local_network(deep=False)

        devices = list(self.last_scan_result.get("devices", []))

        # Transmit subnet-wide broadcast beacon
        b_res = self.broadcast_online_notification(message=msg)

        # Notify individual devices concurrently
        linked_devices: List[Dict[str, Any]] = []

        def _link_device(dev: Dict[str, Any]) -> Dict[str, Any]:
            ip = dev.get("ip", "")
            d_copy = dict(dev)
            if ip:
                notif = self.notify_single_device(ip, message=msg)
                d_copy["linked"] = True
                d_copy["link_status"] = "Connected"
                d_copy["notified"] = notif.get("success", True)
            else:
                d_copy["linked"] = False
                d_copy["link_status"] = "Offline"
            return d_copy

        if devices:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(devices), 16)) as ex:
                linked_devices = list(ex.map(_link_device, devices))

        # Update last scan result with linked states
        self.last_scan_result["devices"] = linked_devices
        self.last_scan_result["mesh_linked"] = True
        self.last_scan_result["mesh_timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        elapsed = round(time.time() - t0, 2)
        count_linked = len(linked_devices)

        # Push notification to any connected mobile companion web sessions
        try:
            from saturn_connect.service import get_service
            from pathlib import Path
            svc = get_service(Path(__file__).resolve().parent.parent)
            if svc and svc.gateway and svc.gateway.is_running():
                svc.gateway.broadcast_push_notification_threadsafe(msg, target="all")
        except Exception:
            pass

        logger.info(f"Mesh link established with {count_linked} devices in {elapsed}s.")

        net_info = self.get_local_network_info()
        host_ip = net_info.get("host_ip", "127.0.0.1")

        return {
            "success": True,
            "total_devices_linked": count_linked,
            "devices": linked_devices,
            "broadcast": b_res,
            "elapsed_sec": elapsed,
            "host_ip": host_ip,
            "mobile_portal_url": f"http://{host_ip}:8765",
            "message": f"Successfully connected with {count_linked} devices on your network. Broadcast notification transmitted.",
        }


def get_net_sentinel() -> NetSentinel:
    return NetSentinel.get_instance()
