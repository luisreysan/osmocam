"""The Wi-Fi datalink: DUML commands, status pushes and the H.264 live view, over one UDP socket.

Once the computer is on the camera's access point, one pairing frame sent to TCP 7001 wakes the link;
then everything runs over UDP (port 9004 on the Osmo cameras). Every UDP packet starts with an 8-byte
header:

    u16 LE  0x8000 | packet length (header included, 14 bits)
    u16 LE  session id (random, chosen by the client)
    u16 LE  sequence
    u8      packet type
    u8      XOR of the 7 bytes before it

Packet types: 0 handshake, 1 and 3 camera link packets (they carry the cursors to acknowledge), 2 video,
4 acknowledgement (the client sends one every 25 ms), 5 data (DUML messages from the client). DUML
messages from the camera (replies, status pushes, its own requests) ride inside its non-video packets.
See docs/PROTOCOL.md for the details.
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from collections import Counter
from collections.abc import Callable
from enum import IntEnum
from functools import reduce

from .duml import FLAG_REPLY, FLAG_REQUEST, Message, parse_all
from .protocol import (ADDR_APP, ADDR_CAMERA, ADDR_ENCODER_1, ADDR_ENCODER_2, ADDR_LIVEVIEW, ADDR_TOPICS,
                       APP_INFO_REPLY, CAMERA_HOST, DATALINK_PORT, ENCODER_REGISTER, POKE_PORT,
                       REGISTER_PAYLOAD, pairing_message, topic_subscription)
from .util import log

_HEADER = struct.Struct("<HHHBB")
VIDEO_HEADER_SIZE = 20          # video packets: 8-byte link header + 12 bytes, frame id at offset 16
FRAME_ID_OFFSET = 16
SEQ_STEP = 8                    # sequences advance in steps of 8
ACK_INTERVAL = 0.025
# A video frame arrives as a burst of packets less than 0.3 ms apart, then ~30 ms of silence until the
# next one. Handing a frame to the decoder after 5 ms of silence (instead of when the next frame starts)
# saves ~30 ms of latency and never splits a frame.
FRAME_IDLE = 0.005
SPS_4 = b"\x00\x00\x00\x01\x67"
SPS_3 = b"\x00\x00\x01\x67"
# Action series: its video encoder streams only while it gets a heartbeat every 200 ms, with the app's
# registration again every second and the live view trigger every two seconds.
HEARTBEAT_INTERVAL = 0.2
FLAG_WRITE = 0x80               # what the encoder's registration and trigger are sent with

# Handshake: the client's base sequence, then one parameter block per channel (the values the camera
# accepts; the meaning of each field is not documented), then a fixed tail.
_CHANNEL_PARAMS = ((100, 100), (256, 400), (20, 100))
_HANDSHAKE_TAIL = bytes((1, 1, 4, 1, 2))


class Packet(IntEnum):
    HANDSHAKE = 0
    LINK = 1
    VIDEO = 2
    LINK_DATA = 3
    ACK = 4
    DATA = 5


def link_packet(kind: int, session: int, seq: int, body: bytes) -> bytes:
    head = struct.pack("<HHHB", 0x8000 | ((8 + len(body)) & 0x3FFF), session, seq, kind)
    return head + bytes((reduce(int.__xor__, head, 0),)) + body


def handshake_body(base_seq: int) -> bytes:
    body = struct.pack("<H", base_seq)
    for first, second in _CHANNEL_PARAMS:
        body += struct.pack("<HHHHBH", first, second, 1472, 20, 0, 100)
    return body + _HANDSHAKE_TAIL


def ack_body(video: int, data: int, extra: int) -> bytes:
    return b"".join(struct.pack("<HH4x", cursor, cursor) for cursor in (video, data, extra)) + bytes(2)


def poke(host: str = CAMERA_HOST) -> None:
    """Open TCP 7001 and send one pairing frame: the camera starts its UDP datalink after this."""
    try:
        with socket.create_connection((host, POKE_PORT), timeout=1.2) as sock:
            sock.sendall(pairing_message().encode())
            time.sleep(0.4)
        log("Datalink poke sent")
    except OSError as error:
        log(f"Datalink poke failed ({error}), continuing")


class _FrameAssembler:
    """Joins the packets of one video frame (same frame id) into a single H.264 access unit. Nothing is
    handed on before the first SPS, so the decoder always starts on a keyframe."""

    def __init__(self, deliver: Callable[[bytes, float], None]) -> None:
        self._deliver = deliver
        self._data = bytearray()
        self._frame: int | None = None
        self._finished: int | None = None     # frame handed on last: late packets of it are dropped
        self._first_packet = 0.0
        self._last_packet = 0.0
        self._synced = False

    def add(self, frame_id: int, data: bytes, now: float) -> None:
        if frame_id == self._finished:
            return
        if self._frame is not None and frame_id != self._frame:
            self.finish()
        if self._frame is None:
            self._frame, self._first_packet = frame_id, now
        self._data += data
        self._last_packet = now

    def finish_if_idle(self, now: float) -> None:
        if self._frame is not None and now - self._last_packet >= FRAME_IDLE:
            self.finish()

    def finish(self) -> None:
        data, self._data = bytes(self._data), bytearray()
        self._finished, self._frame = self._frame, None
        if not self._synced:
            start = data.find(SPS_4)
            if start < 0:
                start = data.find(SPS_3)
            if start < 0:
                return
            self._synced = True
            log("First keyframe received: video is flowing")
            data = data[start:]
        if data:
            self._deliver(data, self._first_packet)


class Datalink:
    """One datalink session with the camera. run() blocks until stop() (or a socket error); commands can be
    sent from any thread with send(). Camera messages go to on_message, video access units to on_video
    together with the perf_counter() time their first packet arrived."""

    def __init__(self, port: int = DATALINK_PORT, host: str = CAMERA_HOST,
                 on_video: Callable[[bytes, float], None] | None = None,
                 on_message: Callable[[Message], None] | None = None, liveview: str = "nano",
                 topics: tuple[str, ...] = ()) -> None:
        self.host, self.port = host, port
        self.liveview = liveview              # "nano" or "action": see camlink.models.liveview_mode
        self.topics = topics                  # named status topics to subscribe to (pushed as 0x00/0x99)
        self.on_video = on_video
        self.on_message = on_message
        # The camera sends requests of its own ("who are you" about once a second, status topics...). They
        # must be answered: otherwise it treats the link as lost (a docked camera's Vision Dock then keeps
        # showing "connecting...").
        self.answer_requests = True
        self.stopped = threading.Event()
        self.last_video = 0.0                 # monotonic() of the last video packet
        self.stats = {"packets": 0, "video_packets": 0, "video_bytes": 0}
        self.kinds: Counter = Counter()       # packets received, by type
        self.replies: dict[tuple[int, int, int], str] = {}   # (cmd set, cmd id, sender) -> reply payload
        self._session = random.randrange(0x1000, 0xFFFE)
        self._base_seq = random.randrange(0x1000, 0xF000) & 0xFFF8
        # Two separate sequences: link packets (handshake, acks) and data packets. Data packets chain to
        # the previous data packet, so acks must not advance their sequence.
        self._link_seq = 0
        self._data_seq = 0
        self._data_count = 0
        self._message_seq = 0xA000
        self._cursors = [0, self._base_seq, self._base_seq]   # video, data, extra: echoed in every ack
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", 0))
        self._lock = threading.Lock()

    # ---- sending
    def _send_link(self, kind: int, body: bytes) -> None:
        with self._lock:
            self._sock.sendto(link_packet(kind, self._session, self._link_seq, body), (self.host, self.port))
            self._link_seq = (self._link_seq + SEQ_STEP) & 0xFFFF

    def _send_data(self, message: Message) -> None:
        with self._lock:
            seq = self._data_seq
            routing = struct.pack("<HH4xBB2x", (seq - SEQ_STEP) & 0xFFFF, seq, self._data_count & 0xFF, 1)
            packet = link_packet(Packet.DATA, self._session, seq, routing + message.encode())
            self._sock.sendto(packet, (self.host, self.port))
            self._data_seq = (seq + SEQ_STEP) & 0xFFFF
            self._data_count = (self._data_count + 1) & 0xFF

    def send(self, cmd_set: int, cmd_id: int, payload: bytes = b"", receiver: int | None = None,
             flags: int = FLAG_REQUEST) -> int:
        """Send a DUML request; returns its sequence number, which the camera's reply carries."""
        if receiver is None:
            receiver = ADDR_LIVEVIEW if (cmd_set, cmd_id) == (0x09, 0xA8) else ADDR_CAMERA
        with self._lock:
            seq = self._message_seq
            self._message_seq = (seq + 1) & 0xFFFF
        self._send_data(Message(ADDR_APP, receiver, seq, flags, cmd_set, cmd_id, payload))
        return seq

    def _answer(self, m: Message) -> None:
        if m.command == (0x00, 0x81):
            payload = APP_INFO_REPLY
        elif m.command == (0x00, 0x88) and m.payload[:1] == b"\x19":
            payload = bytes.fromhex("1a00000000")    # heartbeat
        else:
            payload = b"\x00"
        self._send_data(m.answer(payload, flags=FLAG_REPLY, sender=ADDR_APP))

    # ---- session
    def _handshake(self) -> None:
        body = handshake_body(self._base_seq)
        for _ in range(30):
            self._send_link(Packet.HANDSHAKE, body)
            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                try:
                    data, _ = self._sock.recvfrom(65535)
                except socket.timeout:
                    break
                if len(data) >= 8 and data[6] == Packet.HANDSHAKE:
                    self._link_seq = self._data_seq = (self._base_seq + SEQ_STEP) & 0xFFFF
                    log("Datalink handshake OK")
                    return
        raise RuntimeError(f"The camera did not answer the datalink handshake on {self.host}:{self.port}")

    def _start_live_view(self) -> None:
        self.send(0x00, 0x81, REGISTER_PAYLOAD)                      # register as an app
        for i, name in enumerate(self.topics):
            self.send(0x00, 0x99, topic_subscription(name, 0x6A00 + i), ADDR_TOPICS)
        self.send(0x02, 0x09, bytes(10) + b"\x03")                   # live view source: camera
        self.send(0x09, 0xA8, bytes((0, 4, 2, 0, 0, 0, 0, 0, 0, 0)))  # start transmitting
        if self.liveview == "action":
            # Action series: register with the video encoder and trigger it (then keep it alive).
            self.send(0x00, 0x81, APP_INFO_REPLY, ADDR_ENCODER_2, FLAG_WRITE)
            self.send(0x00, 0x88, ENCODER_REGISTER, ADDR_ENCODER_1)
            self._trigger_encoder()
        log("Registered and live view requested" + (" (Action series)" if self.liveview == "action" else ""))

    def _trigger_encoder(self) -> None:
        self.send(0x00, 0x81, APP_INFO_REPLY, ADDR_ENCODER_2, FLAG_WRITE)
        self.send(0x00, 0x82, b"\x00", ADDR_ENCODER_2, FLAG_WRITE)

    def _heartbeat_loop(self) -> None:
        tick = 0
        while not self.stopped.wait(HEARTBEAT_INTERVAL):
            tick += 1
            try:
                self.send(0x00, 0x4F, struct.pack("<BBBBBI", 1, 0, (tick // 2) & 0xFF, 0, 0, 0xFFFFFFFF),
                          ADDR_ENCODER_2)
                if tick % 5 == 0:
                    self.send(0x00, 0x88, ENCODER_REGISTER, ADDR_ENCODER_1)
                if tick % 10 == 0:
                    self._trigger_encoder()
            except OSError:
                pass

    def _ack_loop(self) -> None:
        while not self.stopped.is_set():
            try:
                self._send_link(Packet.ACK, ack_body(*self._cursors))
            except OSError:
                pass
            time.sleep(ACK_INTERVAL)

    def _receive(self) -> None:
        frames = _FrameAssembler(self._deliver_video)
        video_at = 0.0                        # perf_counter() of the last video packet
        self._sock.settimeout(FRAME_IDLE)
        while not self.stopped.is_set():
            try:
                packet, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                frames.finish_if_idle(time.perf_counter())
                continue
            except OSError:
                break
            now = time.perf_counter()
            self.stats["packets"] += 1
            if len(packet) < VIDEO_HEADER_SIZE:
                continue
            _, _, seq, kind, _ = _HEADER.unpack_from(packet)
            self.kinds[kind] += 1
            if kind == Packet.VIDEO:
                self._cursors[0] = seq
                self.last_video = time.monotonic()
                video_at = now
                self.stats["video_packets"] += 1
                self.stats["video_bytes"] += len(packet) - VIDEO_HEADER_SIZE
                frames.add(packet[FRAME_ID_OFFSET], packet[VIDEO_HEADER_SIZE:], now)
                continue
            if kind == Packet.LINK and len(packet) >= 34:
                # Until video flows (or while it is stalled), acknowledge the video cursor the camera
                # reports itself: some models don't start their video channel otherwise.
                if now - video_at > 0.5:
                    self._cursors[0] = struct.unpack_from("<H", packet, 10)[0]
                self._cursors[1] = struct.unpack_from("<H", packet, 18)[0]
                self._cursors[2] = struct.unpack_from("<H", packet, 26)[0]
            elif kind == Packet.LINK_DATA:
                self._cursors[1] = seq
            for message in parse_all(packet[8:]):
                if message.needs_reply:
                    if self.answer_requests:
                        self._answer(message)
                elif message.flags & FLAG_REPLY:
                    self.replies[(message.cmd_set, message.cmd_id, message.sender)] = message.payload[:4].hex()
                if self.on_message:
                    self.on_message(message)

    def _deliver_video(self, data: bytes, first_packet: float) -> None:
        if self.on_video:
            self.on_video(data, first_packet)

    def run(self) -> None:
        self._sock.settimeout(0.25)
        try:
            self._handshake()
            self._start_live_view()
            threading.Thread(target=self._ack_loop, daemon=True, name="datalink-ack").start()
            if self.liveview == "action":
                threading.Thread(target=self._heartbeat_loop, daemon=True, name="datalink-heartbeat").start()
            self._receive()
        finally:
            self.stopped.set()
            self._sock.close()

    def stop(self) -> None:
        self.stopped.set()

    def summary(self) -> str:
        """What the camera sent in this session (for the log when no video comes)."""
        names = {int(k): k.name.lower() for k in Packet}
        kinds = ", ".join(f"{names.get(k, k)} {n}" for k, n in sorted(self.kinds.items())) or "none"
        replies = ", ".join(f"{s:02x}/{i:02x} from {sender:02x}: {payload or '-'}"
                            for (s, i, sender), payload in list(self.replies.items())[:12]) or "none"
        return f"camera packets: {kinds}; replies: {replies}"
