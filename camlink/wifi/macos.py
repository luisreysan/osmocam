"""macOS backend of camlink.wifi: networksetup.

Joining is one networksetup command. Going back can't name the network: networksetup does not use the
password the Mac saved for it, so a direct rejoin fails (error -3924) after up to ~40 s. Instead the
camera's network is removed from the known networks (or macOS might rejoin it) and Wi-Fi is switched off
and on: macOS rejoins its usual network by itself. The current network's name is only read for messages
(recent macOS versions hide it from programs without Location Services access).
"""

from __future__ import annotations

import json
import subprocess
import time

from ..util import log

WIFI_PORTS = ("Wi-Fi", "AirPort")
HIDDEN = ("", "<redacted>")


def _run(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, errors="replace", timeout=timeout)


def _said(result: subprocess.CompletedProcess) -> str:
    return (result.stdout.strip() or result.stderr.strip()).replace("\n", " ")


def _ports(listing: str) -> list[tuple[str, str]]:
    """(hardware port, device) pairs from `networksetup -listallhardwareports`, e.g. ("Wi-Fi", "en0")."""
    ports, port = [], None
    for line in listing.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port is not None:
            ports.append((port, line.split(":", 1)[1].strip()))
            port = None
    return ports


def _is_wifi(device: str) -> bool:
    # "Wi-Fi Power (en0): On" for a Wi-Fi device, "en1 is not a Wi-Fi interface." for any other.
    said = _said(_run("networksetup", "-getairportpower", device))
    return "Power" in said and "not a Wi-Fi" not in said


def _wifi_devices() -> list[str]:
    ports = _ports(_run("networksetup", "-listallhardwareports").stdout)
    named = [device for port, device in ports if port in WIFI_PORTS]
    return named or [device for _, device in ports if device.startswith("en") and _is_wifi(device)]


def adapter_name(interface: str | None = None) -> str:
    devices = _wifi_devices()
    if interface:
        return interface if interface in devices else ""
    return devices[0] if devices else ""


def _from_networksetup(text: str) -> str:
    # macOS 14.3 and older: "Current Wi-Fi Network: Home". Newer: "You are not associated with...".
    prefix = "Current Wi-Fi Network:"
    return text.strip()[len(prefix):].strip() if text.strip().startswith(prefix) else ""


def _from_ipconfig(text: str) -> str:
    for line in text.splitlines():
        key, _, value = line.strip().partition(" : ")
        if key == "SSID" and value.strip() not in HIDDEN:
            return value.strip()
    return ""


def _from_system_profiler(text: str, device: str) -> str:
    try:
        sections = json.loads(text).get("SPAirPortDataType", [])
    except ValueError:
        return ""
    for section in sections:
        for interface in section.get("spairport_airport_interfaces", []):
            if interface.get("_name") == device:
                name = interface.get("spairport_current_network_information", {}).get("_name", "")
                return "" if name in HIDDEN else name
    return ""


def current_network(interface: str | None = None) -> str:
    device = adapter_name(interface)
    if not device:
        return ""
    return (_from_networksetup(_run("networksetup", "-getairportnetwork", device).stdout)
            or _from_ipconfig(_run("ipconfig", "getsummary", device).stdout)
            or _from_system_profiler(_run("system_profiler", "-json", "SPAirPortDataType", timeout=60).stdout,
                                     device))


def prepare(_ssid: str, _password: str, device: str) -> None:
    if _said(_run("networksetup", "-getairportpower", device)).endswith("Off"):
        log("Turning Wi-Fi on")
        _run("networksetup", "-setairportpower", device, "on")
        time.sleep(2)


def connect(ssid: str, password: str, device: str) -> str:
    """One attempt to join the camera's network; returns what networksetup said (nothing on success)."""
    try:
        return _said(_run("networksetup", "-setairportnetwork", device, ssid, password, timeout=40)) or "requested"
    except subprocess.TimeoutExpired:
        return "no answer from networksetup"


def restore(profile: str, interface: str | None = None, camera: str = "") -> None:
    """Forget the camera's network, then switch Wi-Fi off and on so macOS rejoins its usual network."""
    device = adapter_name(interface)
    if not device:
        return
    if camera:
        try:
            said = _said(_run("networksetup", "-removepreferredwirelessnetwork", device, camera, timeout=15))
            log(f"Forgetting {camera}: {said[:100] or 'done'}")
        except subprocess.TimeoutExpired:
            log(f"Could not forget {camera} (no answer from networksetup)")
    log("Switching Wi-Fi off and on: the Mac rejoins its usual network" + (f" ({profile})" if profile else ""))
    _run("networksetup", "-setairportpower", device, "off")
    time.sleep(1)
    _run("networksetup", "-setairportpower", device, "on")
