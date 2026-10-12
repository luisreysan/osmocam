"""Windows Wi-Fi: join the camera's access point, and go back to the network the PC was on.

Joining adds a WPA2 profile for the camera's network and connects with netsh. Going back uses the Native
Wifi API (wlanapi.dll) directly: netsh refuses to connect to a saved network unless Windows location
access is enabled, WlanConnect does not. The adapter is the first Wi-Fi adapter unless one is named.
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import tempfile
import time
from ctypes import wintypes
from xml.sax.saxutils import escape

from .protocol import CAMERA_HOST, CAMERA_SUBNET
from .util import log

PROFILE_XML = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{key}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>
"""


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _InterfaceInfo(ctypes.Structure):
    _fields_ = [("guid", _GUID), ("description", ctypes.c_wchar * 256), ("state", ctypes.c_uint)]


class _InterfaceList(ctypes.Structure):
    _fields_ = [("count", wintypes.DWORD), ("index", wintypes.DWORD), ("items", _InterfaceInfo * 1)]


class _ConnectionParameters(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_uint), ("profile", wintypes.LPCWSTR), ("ssid", ctypes.c_void_p),
                ("bssids", ctypes.c_void_p), ("bss_type", ctypes.c_uint), ("flags", wintypes.DWORD)]


_CONNECTION_MODE_PROFILE = 0
_BSS_INFRASTRUCTURE = 1
_OPCODE_CURRENT_CONNECTION = 7
_STATE_CONNECTED = 1


class _Wlan:
    """Just enough of wlanapi.dll: list adapters, read the current network, connect to a saved one."""

    def __init__(self) -> None:
        self.api = ctypes.WinDLL("wlanapi.dll")
        self.handle = wintypes.HANDLE()
        version = wintypes.DWORD()
        code = self.api.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(self.handle))
        if code:
            raise OSError(code, "WlanOpenHandle failed")

    def __enter__(self) -> "_Wlan":
        return self

    def __exit__(self, *_exc) -> None:
        self.api.WlanCloseHandle(self.handle, None)

    def adapters(self) -> list[tuple[_GUID, str]]:
        pointer = ctypes.c_void_p()
        if self.api.WlanEnumInterfaces(self.handle, None, ctypes.byref(pointer)):
            return []
        try:
            count = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))[0]
            array = ctypes.cast(pointer.value + 8, ctypes.POINTER(_InterfaceInfo * count)).contents
            return [(_copy_guid(item.guid), _alias(item.guid) or item.description) for item in array]
        finally:
            self.api.WlanFreeMemory(pointer)

    def current_profile(self, guid: _GUID) -> str:
        size = wintypes.DWORD()
        pointer = ctypes.c_void_p()
        code = self.api.WlanQueryInterface(self.handle, ctypes.byref(guid), _OPCODE_CURRENT_CONNECTION, None,
                                           ctypes.byref(size), ctypes.byref(pointer), None)
        if code:
            return ""                       # not connected
        try:
            state = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint))[0]
            name = ctypes.wstring_at(pointer.value + 8)
            return name if state == _STATE_CONNECTED else ""
        finally:
            self.api.WlanFreeMemory(pointer)

    def connect(self, guid: _GUID, profile: str) -> int:
        params = _ConnectionParameters(_CONNECTION_MODE_PROFILE, profile, None, None, _BSS_INFRASTRUCTURE, 0)
        return self.api.WlanConnect(self.handle, ctypes.byref(guid), ctypes.byref(params), None)


def _copy_guid(guid: _GUID) -> _GUID:
    copy = _GUID()
    ctypes.memmove(ctypes.byref(copy), ctypes.byref(guid), ctypes.sizeof(_GUID))
    return copy


def _alias(guid: _GUID) -> str:
    """The adapter's name as Windows shows it ("Wi-Fi", "WLAN"...), which netsh expects."""
    try:
        iphlp = ctypes.WinDLL("iphlpapi.dll")
        luid = ctypes.c_uint64()
        if iphlp.ConvertInterfaceGuidToLuid(ctypes.byref(guid), ctypes.byref(luid)):
            return ""
        buffer = ctypes.create_unicode_buffer(257)
        if iphlp.ConvertInterfaceLuidToAlias(ctypes.byref(luid), buffer, ctypes.c_size_t(len(buffer))):
            return ""
        return buffer.value
    except OSError:
        return ""


def _adapter(interface: str | None) -> tuple[_GUID, str] | None:
    with _Wlan() as wlan:
        adapters = wlan.adapters()
    if interface:
        return next((a for a in adapters if a[1].lower() == interface.lower()), None)
    return adapters[0] if adapters else None


def adapter_name(interface: str | None = None) -> str:
    """Name of the Wi-Fi adapter in use ("" when the PC has none)."""
    found = _adapter(interface)
    return found[1] if found else ""


def current_network(interface: str | None = None) -> str:
    """Saved profile (network name) the Wi-Fi adapter is connected to, or "" when it isn't."""
    try:
        found = _adapter(interface)
        if not found:
            return ""
        with _Wlan() as wlan:
            return wlan.current_profile(found[0])
    except OSError:
        return ""


def _netsh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["netsh", "wlan", *args], capture_output=True, text=True, encoding="oem",
                          errors="replace")


def local_ip_towards(host: str) -> str:
    """Local IPv4 address Windows would use to reach `host` ("" if there is no route)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 9))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def on_camera_network() -> bool:
    return local_ip_towards(CAMERA_HOST).startswith(CAMERA_SUBNET)


def join(ssid: str, password: str, timeout: float = 45, interface: str | None = None) -> str:
    """Connect the Wi-Fi adapter to the camera's access point; returns the PC's address on it."""
    if on_camera_network():
        ip = local_ip_towards(CAMERA_HOST)
        log(f"Already on the camera network ({ip})")
        return ip
    name = adapter_name(interface)
    if not name:
        raise RuntimeError("No Wi-Fi adapter found" + (f" named {interface!r}" if interface else ""))
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(PROFILE_XML.format(ssid=escape(ssid), key=escape(password)))
        result = _netsh("add", "profile", f"filename={path}", "user=current")
        if result.returncode != 0:
            raise RuntimeError(f"Could not add the camera's Wi-Fi profile: {result.stdout.strip()} {result.stderr.strip()}")
    finally:
        os.remove(path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _netsh("connect", f"name={ssid}", f"ssid={ssid}", f"interface={name}")
        log(f"Joining {ssid} on {name}: {result.stdout.strip()[:100]}")
        for _ in range(8):
            time.sleep(1)
            ip = local_ip_towards(CAMERA_HOST)
            if ip.startswith(CAMERA_SUBNET):
                log(f"Joined {ssid} ({ip})")
                return ip
    raise RuntimeError(f"Could not join {ssid} within {timeout:.0f} s")


def restore(profile: str, interface: str | None = None) -> None:
    """Reconnect the Wi-Fi adapter to the saved network it was on before (`profile`)."""
    if not profile:
        return
    try:
        found = _adapter(interface)
        if found:
            with _Wlan() as wlan:
                code = wlan.connect(found[0], profile)
            if code == 0:
                log(f"Returning Wi-Fi to {profile!r}")
                return
            log(f"WlanConnect to {profile!r} returned {code}, trying netsh")
    except OSError as error:
        log(f"Native Wi-Fi API unavailable ({error}), trying netsh")
    args = ["connect", f"name={profile}", f"ssid={profile}"]
    name = adapter_name(interface)
    if name:
        args.append(f"interface={name}")
    result = _netsh(*args)
    log(f"Returning Wi-Fi to {profile!r}: {result.stdout.strip()[:100]}")
