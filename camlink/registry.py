"""Cameras linked to this computer: cameras.json in util.data_dir(), shared by both apps.

Each entry keeps the camera's BLE address, name and model, the Wi-Fi credentials it handed over when it
was paired, and the datalink transport that worked. A camera that joins a shared network instead of
running its own access point (station mode) also has that network's name and its last address there;
the passwords of those networks are kept once, under "networks". The file stays on this computer; it is
never uploaded.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

from .protocol import ALT_DATALINK_PORT, DATALINK_PORT
from .util import data_dir, log

DEFAULT_TRANSPORT = {"port": DATALINK_PORT, "poke": True}
ALTERNATE_TRANSPORT = {"port": ALT_DATALINK_PORT, "poke": False}


def other_transport(transport: dict) -> dict:
    return dict(ALTERNATE_TRANSPORT if transport.get("port") == DATALINK_PORT else DEFAULT_TRANSPORT)


class CameraRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "cameras.json"
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.cameras: list[dict] = data.get("cameras", [])
        self.networks: dict[str, str] = data.get("networks", {})     # shared network name -> password

    def _save(self) -> None:
        data = {"cameras": self.cameras}
        if self.networks:
            data["networks"] = self.networks
        with contextlib.suppress(OSError):
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def get(self, address: str | None) -> dict | None:
        with self._lock:
            return next((dict(c) for c in self.cameras if c["address"] == (address or "").upper()), None)

    def last_used(self) -> dict | None:
        with self._lock:
            return dict(max(self.cameras, key=lambda c: c.get("last_used", 0))) if self.cameras else None

    def remember(self, ble, touch: bool = True) -> dict:
        """Store (or refresh) the camera a camlink.ble.BleLink just paired with. `touch`: it becomes the
        camera used last (the one the apps pick next time)."""
        info = ble.advert or {}
        with self._lock:
            cam = next((c for c in self.cameras if c["address"] == ble.address), None)
            if cam is None:
                cam = {"address": ble.address, "added": time.time(), "transport": dict(DEFAULT_TRANSPORT)}
                self.cameras.append(cam)
                log(f"New camera linked: {ble.name or ble.address} ({info.get('model', 'DJI Osmo')})")
            cam.update(name=ble.name or cam.get("name", ""), ssid=ble.ssid, password=ble.password)
            if touch or "last_used" not in cam:
                cam["last_used"] = time.time() if touch else 0
            if info.get("model_id") is not None or "model_id" not in cam:
                cam.update(model_id=info.get("model_id"), model=info.get("model", "DJI Osmo"),
                           tested=bool(info.get("tested")))
            self._save()
            return dict(cam)

    def set_transport(self, address: str, transport: dict) -> None:
        with self._lock:
            for cam in self.cameras:
                if cam["address"] == address:
                    cam["transport"] = dict(transport)
            self._save()

    def set_network(self, address: str, ssid: str | None, password: str | None = None) -> None:
        """Connect to this camera over the shared network `ssid` (None: over the camera's own access point).
        A password replaces the one kept for that network."""
        with self._lock:
            for cam in self.cameras:
                if cam["address"] == address:
                    cam["network"] = ssid or None
                    cam.pop("host", None)
            if ssid and password is not None:
                self.networks[ssid] = password
            self._save()

    def network_password(self, ssid: str) -> str | None:
        with self._lock:
            return self.networks.get(ssid)

    def network_names(self) -> list[str]:
        with self._lock:
            return sorted(self.networks)

    def set_host(self, address: str, host: str | None) -> None:
        """Where the camera was found on its shared network (tried first next time)."""
        with self._lock:
            for cam in self.cameras:
                if cam["address"] == address and cam.get("host") != host:
                    cam["host"] = host
                    self._save()

    def mark_station(self, address: str, on: bool) -> None:
        """The camera was asked to join a shared network (on) / put back on its own access point (off), so a
        later session knows to put it back even after the app was closed in between."""
        with self._lock:
            for cam in self.cameras:
                if cam["address"] == address and bool(cam.get("in_station")) != on:
                    cam["in_station"] = on
                    self._save()

    def forget(self, address: str) -> None:
        with self._lock:
            self.cameras = [c for c in self.cameras if c["address"] != address.upper()]
            self._save()

    def public(self) -> list[dict]:
        """Every linked camera, without the Wi-Fi password."""
        with self._lock:
            return [{k: v for k, v in c.items() if k != "password"} for c in self.cameras]
