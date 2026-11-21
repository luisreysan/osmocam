"""Station mode helpers: have the camera join a shared Wi-Fi network (cafe Wi-Fi or phone hotspot)
and discover its DHCP address on the local subnet via TCP 7001 + DUML 07/07 identity check.

See docs/PROTOCOL.md section 4.1.
"""

from __future__ import annotations

import getpass
import ipaddress
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import datalink, wifi
from .protocol import ADDR_WIFI, DATALINK_PORT, POKE_PORT
from .registry import CameraRegistry
from .util import log


def probe_tcp(host: str, port: int = POKE_PORT, timeout: float = 0.5) -> bool:
    """True when `host:port` accepts a TCP connection within `timeout` seconds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_ifconfig_ipv4(text: str) -> tuple[str, str] | None:
    """First non-loopback, non-link-local (ip, netmask) from an `ifconfig` block."""
    for line in text.splitlines():
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", line)
        if not m:
            continue
        ip = m.group(1)
        if ip.startswith(("127.", "169.254.")):
            continue
        hex_mask = re.search(r"netmask\s+0x([0-9a-fA-F]{8})", line)
        if hex_mask:
            return ip, str(ipaddress.IPv4Address(int(hex_mask.group(1), 16)))
        dec_mask = re.search(r"netmask\s+(\d+\.\d+\.\d+\.\d+)", line)
        return ip, (dec_mask.group(1) if dec_mask else "24")
    return None


def _wifi_interface_network(interface: str | None = None) -> tuple[str, ipaddress.IPv4Network] | None:
    """Prefer the Wi-Fi adapter's IPv4 subnet directly (stays accurate even when a VPN/Tailscale exit node is active)."""
    if sys.platform == "win32":
        return None
    device = wifi.adapter_name(interface)
    if not device:
        return None
    try:
        out = subprocess.run(["ifconfig", device], capture_output=True, text=True, errors="replace", timeout=5).stdout
        parsed = _parse_ifconfig_ipv4(out)
        if parsed:
            return parsed[0], ipaddress.IPv4Network(f"{parsed[0]}/{parsed[1]}", strict=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        out = subprocess.run(["ip", "-4", "addr", "show", device], capture_output=True, text=True, errors="replace", timeout=5).stdout
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", out)
        if m and not m.group(1).startswith(("127.", "169.254.")):
            return m.group(1), ipaddress.IPv4Network(f"{m.group(1)}/{m.group(2)}", strict=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _outbound_ip() -> str:
    """Local IPv4 address of the interface currently routing to the internet (no packets sent)."""
    for target in ("8.8.8.8", "1.1.1.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith(("127.", "169.254.")):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    return ""


def _netmask_for(ip: str) -> str:
    """IPv4 netmask (dotted or prefix length) for the interface holding `ip`, or '24' as fallback."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, errors="replace", timeout=5).stdout
            lines = out.splitlines()
            for i, line in enumerate(lines):
                if ip in line:
                    for nxt in lines[i + 1:i + 5]:
                        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", nxt)
                        if m and ("mask" in nxt.lower() or m.group(1).startswith("255.")):
                            return m.group(1)
        else:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, errors="replace", timeout=5).stdout
            for line in out.splitlines():
                if f"inet {ip} " in line or line.strip().startswith(f"inet {ip}"):
                    parsed = _parse_ifconfig_ipv4(line)
                    if parsed:
                        return parsed[1]
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return "24"


def local_network(interface: str | None = None) -> tuple[str, ipaddress.IPv4Network] | None:
    """(local_ip, IPv4Network) for the computer's active Wi-Fi/local network, or None when offline."""
    direct = _wifi_interface_network(interface)
    if direct is not None:
        return direct
    ip = _outbound_ip()
    if not ip:
        return None
    mask = _netmask_for(ip)
    try:
        net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
    except ValueError:
        net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
    return ip, net


def _arp_hosts(network: ipaddress.IPv4Network) -> list[str]:
    """IPv4 addresses inside `network` currently present in the OS ARP cache."""
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, errors="replace", timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[str] = []
    for raw in re.findall(r"\b(\d+\.\d+\.\d+\.\d+)\b", out):
        try:
            addr = ipaddress.IPv4Address(raw)
        except ValueError:
            continue
        if addr in network and raw not in found:
            found.append(raw)
    return found


def _candidate_hosts(local_ip: str, network: ipaddress.IPv4Network, exclude: set[str]) -> list[str]:
    """Hosts to probe for TCP 7001. On subnets larger than /24, scan the computer's /24 plus ARP entries."""
    skip = set(exclude) | {local_ip}
    if network.prefixlen >= 24:
        return [str(h) for h in network.hosts() if str(h) not in skip]
    narrow = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
    hosts = [str(h) for h in narrow.hosts() if str(h) not in skip]
    seen = set(hosts) | skip
    for ip in _arp_hosts(network):
        if ip not in seen:
            hosts.append(ip)
            seen.add(ip)
    return hosts


def identify_camera(host: str, port: int = DATALINK_PORT, timeout: float = 3.0) -> tuple[bool, str | None]:
    """Ask a candidate host for its camera AP name via datalink DUML 07/07 (receiver 0x07).
    Returns (handshake_ok, camera_ssid_or_None)."""
    if port == DATALINK_PORT:
        datalink.poke(host)
    got = threading.Event()
    camera_ssid: list[str | None] = [None]

    def on_message(m) -> None:
        if m.command == (0x07, 0x07) and m.is_reply and len(m.payload) >= 2:
            length = m.payload[1]
            if length and 2 + length <= len(m.payload):
                camera_ssid[0] = m.payload[2:2 + length].decode("ascii", "replace")
                got.set()

    session = datalink.Datalink(port=port, host=host, on_message=on_message, start_live_view=False)
    worker = threading.Thread(target=_run_quiet, args=(session,), daemon=True)
    worker.start()
    handshook = False
    try:
        if session.ready.wait(timeout=timeout):
            handshook = True
            session.send(0x07, 0x07, receiver=ADDR_WIFI)
            got.wait(timeout=2.0)
    finally:
        session.stop()
        worker.join(timeout=1.5)
    return handshook, camera_ssid[0]


def _run_quiet(session: datalink.Datalink) -> None:
    try:
        session.run()
    except Exception:  # noqa: BLE001 - failed probe simply doesn't set session.ready
        pass


def find_camera(expected_ssid: str | None, port: int = DATALINK_PORT,
                preferred_host: str | None = None, exclude_hosts: set[str] | None = None,
                timeout: float = 25.0, interface: str | None = None) -> str | None:
    """Locate the camera on the current local network. Checks `preferred_host` first, then scans TCP 7001
    across the subnet and confirms identity via DUML 07/07."""
    net_info = local_network(interface)
    if net_info is None:
        log("Station mode: this computer has no active IPv4 network connection")
        return None
    local_ip, network = net_info
    exclude = set(exclude_hosts or ())
    if preferred_host and preferred_host not in exclude:
        try:
            if ipaddress.IPv4Address(preferred_host) in network and probe_tcp(preferred_host, POKE_PORT, 0.6):
                log(f"Camera reachable at saved address {preferred_host}")
                return preferred_host
        except ValueError:
            pass

    candidates = _candidate_hosts(local_ip, network, exclude)
    log(f"Searching for {expected_ssid or 'camera'} on {network} ({len(candidates)} addresses, local {local_ip})...")
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        with ThreadPoolExecutor(max_workers=min(64, max(1, len(candidates)))) as pool:
            open_hosts = [h for h, ok in zip(candidates, pool.map(lambda ip: probe_tcp(ip, POKE_PORT, 0.45), candidates)) if ok]
        fallback: str | None = None
        for host in open_hosts:
            ok, ssid = identify_camera(host, port)
            if ok and expected_ssid and ssid == expected_ssid:
                log(f"Found {ssid} on shared network at {host}")
                time.sleep(0.8)  # let the camera close the probe session before the live session connects
                return host
            if ok and (not expected_ssid or ssid is None) and fallback is None:
                fallback = host
        if fallback is not None:
            log(f"Found DJI Osmo camera on shared network at {fallback}")
            time.sleep(0.8)
            return fallback
        if time.monotonic() >= deadline:
            break
        time.sleep(2.0)
    log(f"No camera answered on {network} (TCP {POKE_PORT}) after {attempt} scan(s).")
    log("Tip: cafe/public Wi-Fi with Client Isolation blocks device-to-device video; try your iPhone Personal "
        "Hotspot (with Maximize Compatibility ON) instead.")
    return None


def resolve_station_network(registry: CameraRegistry, station_arg: str, password_arg: str | None,
                            last_network: str | None = None) -> tuple[str, str]:
    """Choose (ssid, password) for Station Mode from CLI args, saved cameras.json networks, or interactive prompt."""
    ssid = station_arg.strip()
    if not ssid:
        saved = registry.network_names()
        if saved:
            default = last_network if last_network in saved else saved[0]
            print("\nSaved Wi-Fi networks for Station Mode:")
            for idx, name in enumerate(saved, 1):
                mark = " (default)" if name == default else ""
                print(f"  {idx}) {name}{mark}")
            print(f"  {len(saved) + 1}) Enter a new Wi-Fi network")
            choice = input(f"Select [1-{len(saved) + 1}] or press Enter for '{default}': ").strip()
            if not choice:
                ssid = default
            elif choice.isdigit() and 1 <= int(choice) <= len(saved):
                ssid = saved[int(choice) - 1]
            elif choice.isdigit() and int(choice) == len(saved) + 1:
                ssid = input("Wi-Fi name (SSID): ").strip()
            else:
                ssid = choice
        else:
            ssid = input("Wi-Fi name (SSID) for the camera to join (cafe Wi-Fi or iPhone Hotspot): ").strip()
    if not ssid:
        raise ValueError("No Wi-Fi network name provided for Station Mode")

    password = password_arg if password_arg is not None else registry.network_password(ssid)
    if password is None:
        password = getpass.getpass(f"Wi-Fi password for '{ssid}' (hidden while typing): ")
    return ssid, password
