"""Bluetooth LE side of the camera: find it, pair, wake its Wi-Fi and read the access point credentials.

The camera exposes two characteristics: FFF4 (notifications switch) and FFF5 (DUML messages, both
ways). After connecting: enable notifications, write 01 00 to FFF4, send the session wake, then the
pairing request. The first time, the camera shows "OSMO" and the user approves it on the camera screen.
Once paired, the camera is woken and asked for the SSID and password of its own Wi-Fi access point.
While the BLE link is up, a keepalive goes out every second and the camera's own requests are answered
(left unanswered, it treats the link as lost).

Instead of running its own access point, the camera can also join another Wi-Fi network (a router, or
this computer's hotspot) as a station: see join_network(). Verified on an Osmo Nano with WPA2 networks.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from bleak import BleakClient, BleakScanner

from . import models
from .duml import FLAG_REQUEST, FrameReader, Message
from .protocol import ADDR_APP, ADDR_SESSION, ADDR_WAKE, ADDR_WIFI, APP_INFO_REPLY, pairing_message
from .util import log

CHAR_CONTROL = "0000fff4-0000-1000-8000-00805f9b34fb"
CHAR_DATA = "0000fff5-0000-1000-8000-00805f9b34fb"

SESSION_WAKE = Message(ADDR_APP, ADDR_SESSION, 0x802B, FLAG_REQUEST, 0x00, 0x2B, b"\x04\x00")
KEEPALIVE = Message(ADDR_APP, ADDR_SESSION, 0x802B, FLAG_REQUEST, 0x00, 0x2B, b"\x01\x01")
CAMERA_WAKE = Message(ADDR_APP, ADDR_WAKE, 0x8053, FLAG_REQUEST, 0x53, 0x10, bytes(4))
ASK_SSID = Message(ADDR_APP, ADDR_WIFI, 0x8007, FLAG_REQUEST, 0x07, 0x07)
ASK_PASSWORD = Message(ADDR_APP, ADDR_WIFI, 0x800E, FLAG_REQUEST, 0x07, 0x0E)

# Wi-Fi role (station mode). Role read: 00 00 = own access point, 00 01 = station; the Osmo Nano has no
# role read (E0). Role set: [01] station, [00] back to its own access point.
ROLE_GET, ROLE_SET, NETWORK_JOIN = 0x39, 0x48, 0x47
JOINED = (b"\x00\x00", b"\x00\x00\x00")       # network join replies that mean success
JOIN_RETRY = b"\x01\xff"                       # refused for now: seen first on WPA3 networks, retried


class JoinRefused(RuntimeError):
    """The camera could not join the network it was given (password, WPA3 only, out of its reach): asking
    again won't help until the network or its password changes."""

PAIR_ALREADY = 0x01
PAIR_NEEDS_APPROVAL = 0x02
PAIR_LATER = 0x06               # busy (seen on the Osmo Pocket): try again in a few seconds

UNAVAILABLE_HINT = ("is Bluetooth on? On macOS, allow Bluetooth for the Terminal app (System Settings > Privacy & "
                    "Security > Bluetooth); on Linux, BlueZ (bluetoothd) must be running")


def _describe(device, adv) -> dict | None:
    name = adv.local_name or device.name or ""
    info = models.decode_advert(name, adv.manufacturer_data)
    if info is None:
        return None
    return dict(info, address=device.address.upper(), name=name, rssi=adv.rssi, t=time.monotonic())


async def scan(seconds: float, on_found: Callable[[dict], None] | None = None,
               stop: Callable[[], bool] | None = None) -> dict[str, dict]:
    """Listen to BLE advertisements for `seconds` (or until `stop()` is true). Returns the DJI Osmo
    cameras heard, by address: {address, name, rssi, model_id, model, tested, unsupported, power, t}."""
    found: dict[str, dict] = {}

    def heard(device, adv) -> None:
        entry = _describe(device, adv)
        if entry is None:
            return
        if not entry["name"]:
            entry["name"] = found.get(entry["address"], {}).get("name", "")
        found[entry["address"]] = entry
        if on_found:
            on_found(entry)

    async with BleakScanner(detection_callback=heard):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (stop and stop()):
            await asyncio.sleep(0.25)
    return found


class BleLink:
    """One camera over BLE: pairing, Wi-Fi credentials, keepalive and the status topics it pushes."""

    def __init__(self) -> None:
        self.client: BleakClient | None = None
        self.address: str | None = None
        self.name = ""
        self.advert: dict | None = None        # decode_advert() of the camera we connected to
        self.ssid: str | None = None
        self.password: str | None = None
        self.disconnected = asyncio.Event()
        self.on_topic: Callable[[bytes], None] | None = None   # 0x00/0x99 status topic pushes
        self.on_stage: Callable[[str], None] | None = None     # searching, connecting, approve, credentials
        self._readers: dict[str, FrameReader] = {}
        self._paired = asyncio.Event()
        self._credentials = asyncio.Event()
        self._pair_status: int | None = None
        self._refused: bool = False
        self._logged: set[tuple[int, int, int]] = set()
        self._keepalive: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiting: dict[tuple[int, int], asyncio.Future] = {}   # replies awaited by request()
        self._seq = 0x9000

    # ---- sending / receiving
    async def _send(self, message: Message) -> None:
        if self.client and self.client.is_connected:
            await self.client.write_gatt_char(CHAR_DATA, message.encode(), response=False)

    def _send_soon(self, message: Message) -> None:
        self._loop.call_soon_threadsafe(lambda: self._loop.create_task(self._send(message)))

    def _notified(self, char, data: bytearray) -> None:
        reader = self._readers.setdefault(str(char.uuid), FrameReader())
        for message in reader.feed(bytes(data)):
            self._handle(message)

    def _handle(self, m: Message) -> None:
        key = (m.flags, m.cmd_set, m.cmd_id)
        first = key not in self._logged
        self._logged.add(key)
        waiter = self._waiting.get(m.command) if m.is_reply else None
        if waiter is not None:
            payload = bytes(m.payload)
            self._loop.call_soon_threadsafe(lambda: waiter.done() or waiter.set_result(payload))
        if m.needs_reply:
            # The camera asks things too (who are you, approval results...) and drops the link when they
            # go unanswered: echo the payload back, or our app record for "who are you".
            self._send_soon(m.answer(APP_INFO_REPLY if m.command == (0x00, 0x81) else m.payload, flags=0xC0))
            if first:
                log(f"BLE request {m.cmd_set:02x}/{m.cmd_id:02x} answered")
            if m.command == (0x07, 0x46):
                self._approval(m.payload)
            return
        if m.command == (0x00, 0x99):
            if self.on_topic:
                self.on_topic(m.payload)
        elif m.command == (0x07, 0x45) and len(m.payload) >= 2:
            self._pairing_status(m.payload[1])
        elif m.command == (0x07, 0x46):
            self._approval(m.payload)
        elif m.command in ((0x07, 0x07), (0x07, 0x0E)):
            self._credential(m.cmd_id, m.payload)
        elif first:
            log(f"BLE message {m.cmd_set:02x}/{m.cmd_id:02x} flags={m.flags:02x}")

    def _pairing_status(self, status: int) -> None:
        if status != self._pair_status:
            if status == PAIR_ALREADY:
                log("Camera: already paired with this app")
            elif status == PAIR_NEEDS_APPROVAL:
                log("Camera: approve the pairing on the camera screen")
                self._stage("approve")
            else:
                log(f"Camera: pairing status 0x{status:02x}")
        self._pair_status = status
        if status == PAIR_ALREADY:
            self._paired.set()

    def _approval(self, payload: bytes) -> None:
        # 01 = approved on the camera screen; anything else = denied or timed out (~20 s without a tap).
        if payload[:1] == b"\x01":
            log("Pairing approved on the camera")
            self._paired.set()
        else:
            self._refused = True
            log("Pairing was not approved (denied, or the prompt timed out)")

    def _credential(self, cmd_id: int, payload: bytes) -> None:
        if len(payload) < 2 or not payload[1] or 2 + payload[1] > len(payload):
            return
        value = payload[2:2 + payload[1]].decode("ascii", "replace")
        if cmd_id == 0x07:
            self.ssid = value
            log(f"Camera access point: {value}")
        else:
            self.password = value
            log("Camera access point password received")
        if self.ssid and self.password:
            self._credentials.set()

    def _stage(self, name: str) -> None:
        if self.on_stage:
            self.on_stage(name)

    async def _keep_alive(self) -> None:
        while self.client and self.client.is_connected:
            try:
                await self._send(KEEPALIVE)
            except Exception as error:  # noqa: BLE001 - keep going through transient GATT errors
                log(f"BLE keepalive failed: {error}")
            await asyncio.sleep(1.0)

    # ---- public
    async def connect(self, address: str | None, approve_timeout: float = 90) -> None:
        """Connect to the camera at `address` (or the first DJI Osmo heard), pair, wake it and fetch the
        credentials of its Wi-Fi access point (self.ssid / self.password)."""
        self._loop = asyncio.get_running_loop()
        self._stage("searching")
        log(f"Looking for {'camera ' + address if address else 'a DJI Osmo camera'} over Bluetooth...")
        heard: dict[str, dict] = {}

        def wanted(device, adv) -> bool:
            entry = _describe(device, adv)
            if entry is None or entry["unsupported"]:
                return False
            if address and entry["address"] != address.upper():
                return False
            heard[entry["address"]] = entry
            return True

        device = await BleakScanner.find_device_by_filter(wanted, timeout=15)
        if device is None:
            raise RuntimeError((f"Camera {address} not found" if address else "No DJI Osmo camera found")
                               + " over Bluetooth (is it on, nearby, and not connected to the phone app?)")
        self.address = device.address.upper()
        self.advert = heard.get(self.address)
        self.name = (self.advert or {}).get("name") or device.name or ""
        log(f"Found {self.name or self.address} ({(self.advert or {}).get('model', 'DJI Osmo')}), connecting...")
        self._stage("connecting")
        self.client = BleakClient(device, disconnected_callback=lambda _client: self.disconnected.set())
        await self.client.connect(timeout=20)
        for char in (CHAR_CONTROL, CHAR_DATA):
            try:
                await self.client.start_notify(char, self._notified)
            except Exception as error:  # noqa: BLE001 - not every model notifies on both
                log(f"Notifications unavailable on {char[4:8]}: {error}")
        await self.client.write_gatt_char(CHAR_CONTROL, b"\x01\x00", response=True)
        await self._send(SESSION_WAKE)
        await asyncio.sleep(0.12)
        await self._send(pairing_message())
        log("Pairing request sent")
        await self._wait_for_pairing(approve_timeout)

        self._stage("credentials")
        await asyncio.sleep(0.1)
        await self._send(CAMERA_WAKE)
        self._keepalive = asyncio.create_task(self._keep_alive())
        for attempt in range(1, 4):
            await asyncio.sleep(0.8)
            if not self.ssid:
                await self._send(ASK_SSID)
                await asyncio.sleep(0.5)
            if not self.password:
                await self._send(ASK_PASSWORD)
            try:
                await asyncio.wait_for(self._credentials.wait(), 4)
                return
            except asyncio.TimeoutError:
                log(f"No Wi-Fi credentials yet (attempt {attempt}/3)")
        raise RuntimeError("The camera did not send its Wi-Fi credentials")

    async def _wait_for_pairing(self, approve_timeout: float) -> None:
        started = time.monotonic()
        resend_at = [2.5, 5.0]
        while not self._paired.is_set():
            if self.disconnected.is_set():
                raise RuntimeError("The camera dropped the Bluetooth link while pairing")
            if self._pair_status == PAIR_LATER:
                raise RuntimeError("The camera asked to pair again in a moment")
            if self._refused:
                raise RuntimeError("Pairing was not approved on the camera screen")
            elapsed = time.monotonic() - started
            if elapsed > approve_timeout:
                raise TimeoutError("Pairing was not approved in time")
            if resend_at and self._pair_status is None and elapsed > resend_at[0]:
                resend_at.pop(0)
                await self._send(pairing_message())
                log("No answer to the pairing request yet, sent it again")
            await asyncio.sleep(0.1)

    async def request(self, cmd_set: int, cmd_id: int, payload: bytes = b"", receiver: int = ADDR_WIFI,
                      timeout: float = 5.0) -> bytes | None:
        """Send a request over BLE and wait for its reply payload (None when none comes in time)."""
        self._seq = self._seq % 0xFFFF + 1
        waiter = self._loop.create_future()
        self._waiting[(cmd_set, cmd_id)] = waiter
        try:
            await self._send(Message(ADDR_APP, receiver, self._seq, FLAG_REQUEST, cmd_set, cmd_id, payload))
            return await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiting.pop((cmd_set, cmd_id), None)

    async def join_network(self, ssid: str, password: str,
                           progress: Callable[[str], None] | None = None) -> bool:
        """Have the camera leave its own access point and join `ssid` as a station. True when it confirmed
        the join; False when no reply came (it may have joined anyway: look for it on the network). Raises
        when it refused. WPA3-only networks are refused by the Osmo Nano; WPA2 (or WPA2/WPA3) works.
        `progress(text)` hears each step (an attempt can take the camera half a minute)."""
        say = progress or (lambda _text: None)
        raw_ssid, raw_password = ssid.encode("utf-8"), password.encode("utf-8")
        if not raw_ssid or len(raw_ssid) > 32 or len(raw_password) > 63:
            raise ValueError("network name up to 32 bytes and password up to 63")
        # both can take the camera 5-6 s to answer (seen on the Osmo Pocket 3 and the Nano)
        role = await self.request(0x07, ROLE_GET, b"\x00", timeout=10)
        if role != b"\x00\x01":                           # not a station yet
            reply = await self.request(0x07, ROLE_SET, b"\x01", timeout=15)
            if reply not in (b"\x00", b"\x00\x00"):
                raise RuntimeError("The camera did not switch to joining other networks"
                                   + (f" (it answered {reply.hex()})" if reply else ""))
            log("Camera switched to station mode; joining the network in 10 s")
            say("Getting the camera ready (10 s)")
            await asyncio.sleep(10)                      # the camera needs a moment before it can join
        payload = bytes((len(raw_ssid),)) + raw_ssid + bytes((len(raw_password),)) + raw_password
        failed = ""
        for attempt in range(1, 4):
            say(f"Attempt {attempt} of 3"
                + (f" (the last one failed: the camera answered {failed})" if failed else ""))
            reply = await self.request(0x07, NETWORK_JOIN, payload, timeout=45)
            if reply in JOINED:
                log(f"Camera joined {ssid}")
                return True
            if reply is None:
                log("No answer to the network join; looking for the camera on the network")
                return False
            log(f"The camera did not join {ssid} (it answered {reply.hex() or 'nothing'}, attempt {attempt} of 3)")
            failed = reply.hex() or "nothing"
            if reply == JOIN_RETRY and attempt < 3:
                say(f"Attempt {attempt} of 3 failed (the camera answered {failed}); trying again in 5 s")
                await asyncio.sleep(5)
                continue
            raise JoinRefused(f"The camera could not join “{ssid}”. Check the password; networks that only "
                              "allow WPA3 are refused (use WPA2 or WPA2/WPA3), and a 5 GHz network may be out "
                              "of the camera's reach (try the router's 2.4 GHz network)")
        return False

    async def use_own_wifi(self) -> bool:
        """Back to the camera's own access point (after join_network)."""
        reply = await self.request(0x07, ROLE_SET, b"\x00")
        return reply in (b"\x00", b"\x00\x00")

    async def close(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
        if self.client and self.client.is_connected:
            await self.client.disconnect()
