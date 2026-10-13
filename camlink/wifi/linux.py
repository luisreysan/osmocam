"""Linux backend of camlink.wifi: NetworkManager's nmcli, which most desktop distributions use.

The camera's network gets a connection profile named after it that never connects by itself and never
takes the default route (with Ethernet plugged in, internet keeps working). Going back brings the
previous connection up again. Without NetworkManager, join the camera's network by hand: the apps notice
and carry on.
"""

from __future__ import annotations

import subprocess

from ..util import log

NO_NMCLI = ("NetworkManager (nmcli) was not found: join the camera's Wi-Fi network by hand (its name and "
            "password are in cameras.json, see the README)")
WIFI_TYPES = ("802-11-wireless", "wifi")
# The camera's network: manual only, no default route and no DNS (the camera offers neither).
CAMERA_SETTINGS = ("connection.autoconnect", "no", "ipv4.never-default", "yes", "ipv4.ignore-auto-dns", "yes")


def _nmcli(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["nmcli", *args], capture_output=True, text=True, errors="replace", timeout=timeout)


def _fields(line: str) -> list[str]:
    """One line of nmcli's terse output (-t): fields split on ':', with '\\:' and '\\\\' escaped."""
    fields, field, escaped = [], [], False
    for char in line:
        if escaped:
            field.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(field))
            field = []
        else:
            field.append(char)
    fields.append("".join(field))
    return fields


def _rows(*args: str) -> list[list[str]]:
    return [_fields(line) for line in _nmcli("-t", *args).stdout.splitlines() if line]


def _said(result: subprocess.CompletedProcess) -> str:
    return (result.stdout.strip() or result.stderr.strip()).replace("\n", " ")


def adapter_name(interface: str | None = None) -> str:
    try:
        devices = [row[0] for row in _rows("-f", "DEVICE,TYPE", "device", "status")
                   if len(row) >= 2 and row[1] == "wifi"]
    except FileNotFoundError:
        raise RuntimeError(NO_NMCLI) from None
    if interface:
        return interface if interface in devices else ""
    return devices[0] if devices else ""


def current_network(interface: str | None = None) -> str:
    try:
        rows = _rows("-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
    except FileNotFoundError:
        return ""
    for row in rows:
        if len(row) >= 3 and row[1] in WIFI_TYPES and (not interface or row[2] == interface):
            return row[0]
    return ""


def prepare(ssid: str, password: str, device: str) -> None:
    """Create (or refresh) the camera's connection profile."""
    names = {row[0] for row in _rows("-f", "NAME", "connection", "show") if row}
    security = ("wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password)
    if ssid in names:
        result = _nmcli("connection", "modify", "id", ssid, *security, *CAMERA_SETTINGS)
    else:
        result = _nmcli("connection", "add", "type", "wifi", "con-name", ssid, "ssid", ssid,
                        *security, *CAMERA_SETTINGS)
    if result.returncode != 0:
        raise RuntimeError(f"Could not add the camera's Wi-Fi profile: {_said(result)}")


def connect(ssid: str, _password: str, device: str) -> str:
    """One attempt to join the camera's network; returns what nmcli said."""
    try:
        # A camera woken a moment ago may be missing from the last scan: scan again first.
        _nmcli("device", "wifi", "list", "--rescan", "yes", "ifname", device, timeout=20)
        return _said(_nmcli("--wait", "20", "connection", "up", "id", ssid, "ifname", device, timeout=30))
    except subprocess.TimeoutExpired:
        return "no answer from NetworkManager"


def restore(profile: str, interface: str | None = None, camera: str = "") -> None:
    """Bring the previous connection back up; with none, just leave the camera's network (NetworkManager
    then connects wherever it normally would)."""
    device = adapter_name(interface)
    if profile:
        args = ["--wait", "20", "connection", "up", "id", profile] + (["ifname", device] if device else [])
        log(f"Returning Wi-Fi to {profile!r}: {_said(_nmcli(*args, timeout=30))[:100]}")
    elif camera:
        log(f"Leaving {camera}: {_said(_nmcli('connection', 'down', 'id', camera))[:100]}")
