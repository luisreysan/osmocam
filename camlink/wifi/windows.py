"""Windows backend of camlink.wifi.

Joining turns Wi-Fi on if it is off (Native Wifi API), adds a WPA2 profile for the camera's network and
connects with netsh. Going back uses the Native Wifi API (wlanapi.dll) directly: netsh refuses to connect
to a saved network unless Windows location access is enabled, WlanConnect does not. Without location access
Windows won't say which network the adapter is on either; its network list (Get-NetConnectionProfile)
still names it.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import time
from ctypes import wintypes
from xml.sax.saxutils import escape

from ..util import log

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


class _PhyRadioState(ctypes.Structure):
    _fields_ = [("phy_index", wintypes.DWORD), ("software", ctypes.c_uint), ("hardware", ctypes.c_uint)]


class _RadioState(ctypes.Structure):
    _fields_ = [("count", wintypes.DWORD), ("phys", _PhyRadioState * 64)]


_CONNECTION_MODE_PROFILE = 0
_BSS_INFRASTRUCTURE = 1
_OPCODE_RADIO_STATE = 4
_OPCODE_CURRENT_CONNECTION = 7
_STATE_CONNECTED = 1
_ACCESS_DENIED = 5                     # WlanQueryInterface without location access (Windows 11)
_RADIO_ON = 1
_RADIO_OFF = 2
# netsh's code for "the wireless LAN interface is powered down" (Wi-Fi off, or airplane mode)
POWERED_DOWN = "0x80342002"
RADIO_OFF = ("Wi-Fi is turned off on this computer: turn it on (the Wi-Fi button in the quick settings, with "
             "airplane mode off) and try again")
RADIO_SWITCH = "Wi-Fi is switched off with a hardware switch or key on this computer: switch it on and try again"
# WlanOpenHandle's answer when the WLAN AutoConfig service isn't running: Windows only runs it on a computer
# with a Wi-Fi adapter (a desktop on Ethernet has none), unless someone disabled it
SERVICE_NOT_ACTIVE = 1062
NO_WIFI_SERVICE = ("This computer has no Wi-Fi: Windows' Wi-Fi service (WLAN AutoConfig) is not running, so "
                   "there is no Wi-Fi adapter or it is disabled")


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

    def current_profile(self, guid: _GUID) -> str | None:
        """The connected network's profile name, "" when not connected, None when Windows won't say."""
        size = wintypes.DWORD()
        pointer = ctypes.c_void_p()
        code = self.api.WlanQueryInterface(self.handle, ctypes.byref(guid), _OPCODE_CURRENT_CONNECTION, None,
                                           ctypes.byref(size), ctypes.byref(pointer), None)
        if code:
            return None if code == _ACCESS_DENIED else ""     # else: not connected
        try:
            state = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint))[0]
            name = ctypes.wstring_at(pointer.value + 8)
            return name if state == _STATE_CONNECTED else ""
        finally:
            self.api.WlanFreeMemory(pointer)

    def connect(self, guid: _GUID, profile: str) -> int:
        params = _ConnectionParameters(_CONNECTION_MODE_PROFILE, profile, None, None, _BSS_INFRASTRUCTURE, 0)
        return self.api.WlanConnect(self.handle, ctypes.byref(guid), ctypes.byref(params), None)

    def radio(self, guid: _GUID) -> list[tuple[int, int, int]]:
        """(phy index, software state, hardware state) of each radio of the adapter ([] if unknown)."""
        size = wintypes.DWORD()
        pointer = ctypes.c_void_p()
        if self.api.WlanQueryInterface(self.handle, ctypes.byref(guid), _OPCODE_RADIO_STATE, None,
                                       ctypes.byref(size), ctypes.byref(pointer), None):
            return []
        try:
            state = ctypes.cast(pointer, ctypes.POINTER(_RadioState)).contents
            return [(p.phy_index, p.software, p.hardware) for p in state.phys[:min(state.count, 64)]]
        finally:
            self.api.WlanFreeMemory(pointer)

    def radio_on(self, guid: _GUID, phy_index: int) -> int:
        state = _PhyRadioState(phy_index, _RADIO_ON, _RADIO_ON)
        return self.api.WlanSetInterface(self.handle, ctypes.byref(guid), _OPCODE_RADIO_STATE,
                                         ctypes.sizeof(state), ctypes.byref(state), None)


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
    """Name of the Wi-Fi adapter in use ("" when the computer has none). Raises when Windows' Wi-Fi
    service can't be used at all."""
    try:
        found = _adapter(interface)
    except OSError as error:
        if error.errno == SERVICE_NOT_ACTIVE:
            raise RuntimeError(NO_WIFI_SERVICE) from None
        raise RuntimeError(f"Windows' Wi-Fi service is unavailable on this computer ({error})") from None
    return found[1] if found else ""


def current_network(interface: str | None = None) -> str:
    """Saved profile (network name) the Wi-Fi adapter is connected to, or "" when it isn't."""
    try:
        found = _adapter(interface)
        if not found:
            return ""
        with _Wlan() as wlan:
            name = wlan.current_profile(found[0])
    except OSError:
        return ""
    return _listed_network(found[1]) if name is None else name


def _listed_network(alias: str) -> str:
    """Name of the network the adapter `alias` is connected to, from Windows' network list."""
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-NetConnectionProfile | "
              "ForEach-Object { $_.InterfaceAlias + [char]9 + $_.Name }")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        interface, _, name = line.strip().partition("	")
        if interface.lower() == alias.lower():
            return name
    return ""


def _decode(raw: bytes) -> str:
    """netsh writes UTF-8 on current Windows versions and the OEM code page on older ones."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("oem", errors="replace")


def _netsh(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["netsh", "wlan", *args], capture_output=True)
    return subprocess.CompletedProcess(result.args, result.returncode, _decode(result.stdout), _decode(result.stderr))


def _turn_radio_on(name: str) -> None:
    """Turn the adapter's Wi-Fi radio on when it is switched off in Windows (the Wi-Fi button, airplane
    mode); a hardware switch can't be overridden."""
    found = _adapter(name)
    if not found:
        return
    with _Wlan() as wlan:
        phys = wlan.radio(found[0])
        if not phys or any(software == _RADIO_ON and hardware == _RADIO_ON for _, software, hardware in phys):
            return
        if all(hardware == _RADIO_OFF for _, _, hardware in phys):
            raise RuntimeError(RADIO_SWITCH)
        log("Wi-Fi is turned off: turning it on")
        for index, software, _ in phys:
            if software == _RADIO_OFF:
                code = wlan.radio_on(found[0], index)
                if code:
                    raise RuntimeError(f"{RADIO_OFF} (could not turn it on: error {code})")
        for _ in range(20):
            time.sleep(0.5)
            if any(software == _RADIO_ON and hardware == _RADIO_ON for _, software, hardware in wlan.radio(found[0])):
                time.sleep(2)                  # give the adapter a moment to see the networks around
                return
    raise RuntimeError(RADIO_OFF)


def prepare(ssid: str, password: str, name: str) -> None:
    """Make sure Wi-Fi is on, and save a WPA2 profile for the camera's network (connect manually: Windows
    never joins it by itself)."""
    try:
        _turn_radio_on(name)
    except OSError as error:
        log(f"Could not read the Wi-Fi radio state ({error}), trying anyway")
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(PROFILE_XML.format(ssid=escape(ssid), key=escape(password)))
        result = _netsh("add", "profile", f"filename={path}", "user=current")
        if result.returncode != 0:
            raise RuntimeError(f"Could not add the camera's Wi-Fi profile: {result.stdout.strip()} {result.stderr.strip()}")
    finally:
        os.remove(path)


def connect(ssid: str, _password: str, name: str) -> str:
    """One attempt to join the camera's network; returns what netsh said."""
    said = _netsh("connect", f"name={ssid}", f"ssid={ssid}", f"interface={name}").stdout.strip()
    if POWERED_DOWN in said:
        raise RuntimeError(RADIO_OFF)
    return said


def restore(profile: str, interface: str | None = None, _camera: str = "") -> None:
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
