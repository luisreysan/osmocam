"""Wi-Fi: join the camera's access point, and go back to the network the computer was on.

One backend per system behind the same functions: Windows (netsh and the Native Wifi API), macOS
(networksetup) and Linux (NetworkManager's nmcli). The adapter is the first Wi-Fi adapter unless one is
named ("Wi-Fi 2" on Windows, en0 on macOS, wlan0 on Linux).
"""

from __future__ import annotations

import socket
import sys
import time

from ..protocol import CAMERA_HOST, CAMERA_SUBNET
from ..util import log

class WifiError(RuntimeError):
    """Joining the camera's network failed; the message says why and what to do."""


if sys.platform == "win32":
    from . import windows as _system
elif sys.platform == "darwin":
    from . import macos as _system
else:
    from . import linux as _system

_camera_ssid = ""        # the camera network joined last, so restore() can make the system forget it


def local_ip_towards(host: str) -> str:
    """Local IPv4 address the system would use to reach `host` ("" if there is no route)."""
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


def adapter_name(interface: str | None = None) -> str:
    """Name of the Wi-Fi adapter in use ("" when the computer has none)."""
    return _system.adapter_name(interface)


def current_network(interface: str | None = None) -> str:
    """Saved network the Wi-Fi adapter is connected to, or "" when it isn't (or the system won't say)."""
    try:
        return _system.current_network(interface)
    except Exception as error:  # noqa: BLE001 - unknown just means "nothing to return to"
        log(f"Could not read the current Wi-Fi network: {error}")
        return ""


def join(ssid: str, password: str, timeout: float = 45, interface: str | None = None) -> str:
    """Connect the Wi-Fi adapter to the camera's access point; returns the computer's address on it."""
    global _camera_ssid
    _camera_ssid = ssid
    if on_camera_network():
        ip = local_ip_towards(CAMERA_HOST)
        log(f"Already on the camera network ({ip})")
        return ip
    try:
        name = adapter_name(interface)
        if not name:
            raise WifiError("No Wi-Fi adapter found" + (f" named {interface!r}" if interface else ""))
        _system.prepare(ssid, password, name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = _system.connect(ssid, password, name)
            log(f"Joining {ssid} on {name}: {result[:100]}")
            for _ in range(8):
                time.sleep(1)
                ip = local_ip_towards(CAMERA_HOST)
                if ip.startswith(CAMERA_SUBNET):
                    log(f"Joined {ssid} ({ip})")
                    return ip
    except WifiError:
        raise
    except RuntimeError as error:                 # the backends' own messages (Wi-Fi off, no nmcli...)
        raise WifiError(str(error)) from error
    raise WifiError(f"Could not join {ssid} within {timeout:.0f} s: is the camera on and close to the computer?")


def restore(profile: str, interface: str | None = None) -> None:
    """Put the Wi-Fi adapter back on the saved network it was on before (`profile`)."""
    try:
        _system.restore(profile, interface, _camera_ssid)
    except Exception as error:  # noqa: BLE001 - runs while shutting down: report, never raise
        log(f"Could not return the Wi-Fi to {profile or 'its network'!r}: {error}")
