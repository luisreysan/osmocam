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

import os
import random
import socket
import struct
import threading
import time
from collections import Counter
from collections.abc import Callable
from enum import IntEnum
from functools import reduce
from pathlib import Path

from .duml import FLAG_REPLY, FLAG_REQUEST, Message, parse_all
from .nal import CODEC_NAMES, H264, parameter_sets, picture_kind
from .protocol import (ADDR_APP, ADDR_CAMERA, ADDR_ENCODER_1, ADDR_ENCODER_2, ADDR_GIMBAL_APP, ADDR_LIVEVIEW,
                       ADDR_LIVEVIEW_POCKET, ADDR_TOPICS,
                       APP_INFO_REPLY, CAMERA_HOST, DATALINK_PORT, ENCODER_REGISTER, POKE_PORT,
                       REGISTER_PAYLOAD, pairing_message, topic_subscription)
from .util import log

_HEADER = struct.Struct("<HHHBB")
VIDEO_HEADER_SIZE = 20          # video packets: 8-byte link header + 12 bytes, transport group at offset 16
GROUP_OFFSET = 16
SEQ_STEP = 8                    # sequences advance in steps of 8
ACK_INTERVAL = 0.025
# The first packet of each picture starts with 00 00 01 FF, the size of the access unit (u32 LE) and 8
# bytes of metadata; the access unit follows.
PICTURE_MAGIC = b"\x00\x00\x01\xff"
PICTURE_HEADER_SIZE = 16
# The Osmo Nano adds a private SEI to each picture: NAL type 6, payload type 0xF0, 25 raw bytes, then the
# 0x80 stop bit. The raw bytes are not escaped and can contain 00 00 01, which a decoder would read as the
# start of another NAL unit, so the whole SEI is removed.
PRIVATE_SEI = b"\x00\x00\x01\x06\xf0\x19"
PRIVATE_SEI_SIZE = 3 + 3 + 25 + 1
# Cameras without the picture header: a picture arrives as a burst of packets less than 0.3 ms apart, then
# ~30 ms of silence until the next one, so it is handed to the decoder after 5 ms of silence.
FRAME_IDLE = 0.005
# Action series: its video encoder streams only while it gets a heartbeat every 200 ms, with the app's
# registration again every second and the live view trigger every two seconds.
HEARTBEAT_INTERVAL = 0.2
FLAG_WRITE = 0x80               # what the encoder's registration and trigger are sent with
# Osmo Pocket: its video stops 8-10 s after the app last said it is there (0x00/0x88), so it is said every
# second. The first command sequence follows the camera's first 34-byte status packet (u16 at 8, + 8).
PRESENCE_INTERVAL = 1.0
TELEMETRY_WAIT = 2.0
# Osmo Pocket 3: it sends a keyframe only when its stream starts, and the stream keeps running between
# sessions (there is no command that stops it, and 0x09/0xA8 brings no keyframe while it runs). Its pictures
# refresh themselves gradually instead (parameter sets every 1.5 s): a lost packet heals within ~1.5 s, and a
# session that joins the running stream can start at its parameter sets; that picture builds up over a few
# seconds. So a Pocket session takes the first picture with parameter sets when no keyframe comes.
POCKET_JOIN_WAIT = 1.0          # video without a keyframe this long: join the running stream
# A keyframe arrives as a burst of packets: a larger receive buffer keeps it whole while the app is busy.
RECEIVE_BUFFER = 2 << 20
# Data packets (the client's commands) form a chain: each names the one before it, and the camera drops
# everything after one it never got. Its link packets acknowledge the last one it took (u16 at 26), so a
# packet still unacknowledged after RESEND_AFTER is sent again, with the ones after it, in order. Without
# this one lost packet silenced every later command (an Osmo Pocket then stops its video: no presence).
RESEND_AFTER = 0.3
RESEND_GIVE_UP = 5.0            # the camera takes none of them for this long: leave it to the watchdog
UNACKED_MAX = 256

# Handshake: the client's base sequence, then one parameter block per channel (the values the camera
# accepts; the meaning of each field is not documented), then a fixed tail.
_CHANNEL_PARAMS = ((100, 100), (256, 400), (20, 100))
_HANDSHAKE_TAIL = bytes((1, 1, 4, 1, 2))
# Diagnostics: with OSMO_VIDEO_DUMP set to a folder, each session writes every packet it receives and every
# packet it sends there (records: f64 perf_counter, u8 0 received / 1 sent, u16 length, the packet).
DUMP_DIR = os.environ.get("OSMO_VIDEO_DUMP")
_DUMP_RECORD = struct.Struct("<dBH")


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


def strip_private_sei(unit: bytes) -> bytes:
    """Remove the camera's private SEI (see PRIVATE_SEI) from an access unit, by its fixed length."""
    start = 0
    while (i := unit.find(PRIVATE_SEI, start)) >= 0:
        end = i + PRIVATE_SEI_SIZE
        if end > len(unit) or unit[end - 1] != 0x80:
            start = i + 1
            continue
        if i > 0 and unit[i - 1] == 0:
            i -= 1                            # 4-byte start code
        unit = unit[:i] + unit[end:]
        start = i
    return unit


class _FrameAssembler:
    """Rebuilds the access units of the live view from the video packets.

    A picture is complete once the size its header announces has arrived. It can span several transport
    groups (byte 16 of the packet; an Osmo Nano group ends after 63 packets, and a keyframe can be bigger),
    so the group number must not split pictures. A gap in the packet sequence means a lost packet: the part
    that arrived is handed on and the decoder conceals the rest. Cameras that send no picture header are
    split on the group number and after a short silence instead. Nothing is handed on before the first
    keyframe (an H.264 IDR or I picture, an H.265 random-access picture) with its parameter sets: an Osmo
    Pocket repeats its parameter sets alone every 1.5 s, but sends a keyframe only when its live view starts,
    so parameter sets don't mean a picture can be decoded. Parameter sets that come alone ride with the
    next picture.

    `join`: a camera that refreshes its pictures gradually (Osmo Pocket 3) may also be joined at its
    parameter sets after POCKET_JOIN_WAIT without a keyframe; the decoder then shows the picture while it
    builds up."""

    def __init__(self, deliver: Callable[[bytes, float], None], join: bool = False) -> None:
        self._deliver = deliver
        self._join = join
        self._data = bytearray()
        self._need = 0                        # bytes still missing from the current picture
        self._headers = False                 # this camera sends picture headers
        self._group: int | None = None        # without headers: group being joined
        self._finished: int | None = None     # without headers: group handed on last (late packets dropped)
        self._next_seq: int | None = None
        self._first_packet = 0.0
        self._last_packet = 0.0
        self._synced = False
        self._dropping = False                # dropping the rest of a picture whose first packet was lost
        self.truncated = 0                    # pictures broken by lost packets (cut short, or dropped)
        self._params: bytes | None = None     # parameter sets that came alone, for the next picture
        self.codec = H264                     # from the first parameter sets
        self.keyframes = 0                    # keyframes received
        self._video_since: float | None = None   # monotonic() of the first video packet
        self.pictures: Counter = Counter()    # pictures by kind (nal.picture_kind), for the diagnostics
        self.timeline: list[str] = []         # kind and size of the first pictures

    def _lost(self) -> None:
        self.truncated += 1
        if self.truncated == 1:
            log("Live view: a picture arrived incomplete (lost packets)")

    def add(self, seq: int, group: int, data: bytes, now: float) -> None:
        if self._video_since is None:
            self._video_since = time.monotonic()
        lost = self._next_seq is not None and seq != self._next_seq
        self._next_seq = (seq + SEQ_STEP) & 0xFFFF
        if data[:4] == PICTURE_MAGIC and len(data) >= PICTURE_HEADER_SIZE:
            if self._data:
                self.finish(short=True)       # the previous picture never completed
            elif lost and not self._dropping:
                self._lost()                  # whole packets (a small picture?) went missing in between
            self._dropping = False
            self._headers = True
            self._need = int.from_bytes(data[4:8], "little")
            self._first_packet = now
            data = data[PICTURE_HEADER_SIZE:]
        elif self._need:
            if lost:
                self.finish(short=True)       # the rest of this picture can't be placed: drop it
                self._dropping = True
                return
        elif self._headers:
            if not self._dropping:            # the rest of a picture whose first packet was lost
                self._dropping = True
                self._lost()
            return
        else:
            if group == self._finished:
                return
            if self._group is not None and group != self._group:
                self.finish()
            if self._group is None:
                self._group, self._first_packet = group, now
            self._data += data
            self._last_packet = now
            return
        part = data[:self._need]
        self._data += part
        self._need -= len(part)
        self._last_packet = now
        if not self._need:
            self.finish()

    def finish_if_idle(self, now: float) -> None:
        if self._group is not None and now - self._last_packet >= FRAME_IDLE:
            self.finish()

    def finish(self, short: bool = False) -> None:
        data, self._data = bytes(self._data), bytearray()
        self._need = 0
        self._finished, self._group = self._group, None
        if short:
            self._lost()
        data = strip_private_sei(data)
        if not data:
            return
        found = parameter_sets(data)
        if found:
            self.codec = found[1]
        kind = picture_kind(data, self.codec)
        self.pictures[kind] += 1
        if len(self.timeline) < 16:
            self.timeline.append(f"{kind} {len(data) // 1024} KB")
        if kind == "PS":
            self._params = data[found[0]:] if found else data
            return
        if self._params is not None:
            data, self._params = self._params + data, None
            found = parameter_sets(data)
        keyframe = kind.split("+")[0] in ("IDR", "I")
        if keyframe and not short:
            self.keyframes += 1
        if not self._synced:
            if found is None:
                return
            if keyframe:
                log(f"First keyframe received ({CODEC_NAMES[found[1]]}): video is flowing")
            elif self._join and time.monotonic() - (self._video_since or 0.0) >= POCKET_JOIN_WAIT:
                log(f"No keyframe from the camera: joined its running stream ({CODEC_NAMES[found[1]]}); "
                    "the picture builds up over a few seconds")
            else:
                return
            self._synced = True
            data = data[found[0]:]
        if data:
            self._deliver(data, self._first_packet)


class Datalink:
    """One datalink session with the camera. run() blocks until stop() (or a socket error); commands can be
    sent from any thread with send(). Camera messages go to on_message, video access units to on_video
    together with the perf_counter() time their first packet arrived."""

    def __init__(self, port: int = DATALINK_PORT, host: str = CAMERA_HOST,
                 on_video: Callable[[bytes, float], None] | None = None,
                 on_message: Callable[[Message], None] | None = None, liveview: str = "nano",
                 topics: tuple[str, ...] = (), start_live_view: bool = True) -> None:
        self.host, self.port = host, port
        self.liveview = liveview              # "nano", "pocket" or "action": see camlink.models.liveview_mode
        self._liveview_addr = ADDR_LIVEVIEW_POCKET if liveview == "pocket" else ADDR_LIVEVIEW
        self.topics = topics                  # named status topics to subscribe to (pushed as 0x00/0x99)
        self.start_live_view = start_live_view  # False: handshake only (e.g. to ask the camera who it is)
        self.on_video = on_video
        self.on_message = on_message
        # The camera sends requests of its own ("who are you" about once a second, status topics...). They
        # must be answered: otherwise it treats the link as lost (a docked camera's Vision Dock then keeps
        # showing "connecting...").
        self.answer_requests = True
        self.stopped = threading.Event()
        self.ready = threading.Event()        # handshake done and live view requested: send() works
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
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER)
        except OSError:
            pass
        self._sock.bind(("0.0.0.0", 0))
        self._lock = threading.Lock()
        self._unacked: list[tuple[int, bytes, float]] = []   # data packets sent, not acknowledged yet
        self._acks_seen = False               # the camera acknowledged data packets in this session
        self._acked_cursor = -1               # the last data packet the camera acknowledged
        self._last_ack = 0.0                  # monotonic() of the last new acknowledgement
        self.resent = 0                       # data packets sent again
        self.frames: _FrameAssembler | None = None   # live view assembly (its counters), once receiving
        self._dump = None                     # diagnostics file (DUMP_DIR)
        self._dump_lock = threading.Lock()

    # ---- diagnostics
    def _open_dump(self) -> None:
        if not DUMP_DIR:
            return
        try:
            folder = Path(DUMP_DIR)
            folder.mkdir(parents=True, exist_ok=True)
            label = "live" if self.start_live_view else "identify"
            path = folder / f"{time.strftime('%H%M%S')}-{self.host}-{label}-{self._session:04x}.osmodump"
            self._dump = path.open("wb")
            log(f"Diagnostics: this {label} session is written to {path}")
        except OSError as error:
            log(f"Diagnostics: could not write to {DUMP_DIR} ({error})")

    def _record(self, direction: int, packet: bytes) -> None:
        dump = self._dump
        if dump is not None:
            with self._dump_lock:
                try:
                    dump.write(_DUMP_RECORD.pack(time.perf_counter(), direction, len(packet)) + packet)
                except (OSError, ValueError):
                    pass

    # ---- sending
    def _send_link(self, kind: int, body: bytes) -> None:
        with self._lock:
            packet = link_packet(kind, self._session, self._link_seq, body)
            self._sock.sendto(packet, (self.host, self.port))
            self._record(1, packet)
            self._link_seq = (self._link_seq + SEQ_STEP) & 0xFFFF

    def _send_data(self, message: Message) -> None:
        with self._lock:
            seq = self._data_seq
            routing = struct.pack("<HH4xBB2x", (seq - SEQ_STEP) & 0xFFFF, seq, self._data_count & 0xFF, 1)
            packet = link_packet(Packet.DATA, self._session, seq, routing + message.encode())
            self._sock.sendto(packet, (self.host, self.port))
            self._record(1, packet)
            self._unacked.append((seq, packet, time.monotonic()))
            del self._unacked[:-UNACKED_MAX]
            self._data_seq = (seq + SEQ_STEP) & 0xFFFF
            self._data_count = (self._data_count + 1) & 0xFF

    def _acknowledged(self, cursor: int) -> None:
        """The camera took our data packets up to `cursor`. Only a cursor that names a packet we sent counts
        (a camera that reports something else there never gets packets sent again)."""
        with self._lock:
            for i, (seq, _packet, _sent) in enumerate(self._unacked):
                if seq == cursor:
                    self._unacked = self._unacked[i + 1:]
                    self._acks_seen = True
                    return

    def _resend(self) -> None:
        """Send the unacknowledged data packets again, in order (see RESEND_AFTER)."""
        with self._lock:
            if not self._unacked or not self._acks_seen:
                return
            now = time.monotonic()
            first_sent = self._unacked[0][2]
            if now - first_sent < RESEND_AFTER:
                return
            if self.resent and now - self._last_ack > RESEND_GIVE_UP:
                return
            if not self.resent:
                log(f"Datalink: the camera missed a command packet; sending the last {len(self._unacked)} again")
            for i, (seq, packet, _sent) in enumerate(self._unacked):
                self._sock.sendto(packet, (self.host, self.port))
                self._record(1, packet)
                self._unacked[i] = (seq, packet, now)
            self.resent += len(self._unacked)

    def send(self, cmd_set: int, cmd_id: int, payload: bytes = b"", receiver: int | None = None,
             flags: int = FLAG_REQUEST) -> int:
        """Send a DUML request; returns its sequence number, which the camera's reply carries."""
        if receiver is None:
            receiver = self._liveview_addr if (cmd_set, cmd_id) == (0x09, 0xA8) else ADDR_CAMERA
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
                self._record(0, data)
                if len(data) >= 8 and data[6] == Packet.HANDSHAKE:
                    self._link_seq = self._data_seq = (self._base_seq + SEQ_STEP) & 0xFFFF
                    log("Datalink handshake OK")
                    if self.liveview == "pocket":
                        self._sequence_from_telemetry()
                    return
        raise RuntimeError(f"The camera did not answer the datalink handshake on {self.host}:{self.port}")

    def _sequence_from_telemetry(self) -> None:
        """Osmo Pocket: number the commands from the camera's first 34-byte status packet (u16 at 8, + 8);
        starting from the handshake alone gave slow or failed starts."""
        deadline = time.monotonic() + TELEMETRY_WAIT
        while time.monotonic() < deadline:
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            self._record(0, data)
            if len(data) == 34 and data[6] == Packet.LINK:
                self._data_seq = (struct.unpack_from("<H", data, 8)[0] + SEQ_STEP) & 0xFFFF
                return
        log("No status packet from the camera after the handshake; numbering commands from the handshake")

    def _start_pocket_live_view(self) -> None:
        """Osmo Pocket: register with the encoder, say the app is there, tell the gimbal an app is attached,
        subscribe, then prepare (0x02/0x68 [08]) and request the live view from its transmitter (0x08). No
        0x02/0x09 (the Nano's). Each step gets a moment for its acknowledgement."""
        steps = [(0x00, 0x81, APP_INFO_REPLY, ADDR_ENCODER_2, FLAG_WRITE),
                 (0x00, 0x88, ENCODER_REGISTER, ADDR_ENCODER_1, FLAG_REQUEST),
                 (0x03, 0xDA, bytes.fromhex("05ffffffff"), ADDR_GIMBAL_APP, FLAG_REQUEST)]
        steps += [(0x00, 0x99, topic_subscription(name, 0x6A00 + i), ADDR_TOPICS, FLAG_REQUEST)
                  for i, name in enumerate(self.topics)]
        steps += [(0x02, 0x68, b"\x08", ADDR_CAMERA, FLAG_REQUEST)]
        for cmd_set, cmd_id, payload, receiver, flags in steps:
            self.send(cmd_set, cmd_id, payload, receiver, flags)
            time.sleep(0.03)
        self.request_keyframe()
        log("Registered and live view requested (Osmo Pocket)")

    def _presence_loop(self) -> None:
        while not self.stopped.wait(PRESENCE_INTERVAL):
            try:
                self.send(0x00, 0x88, ENCODER_REGISTER, ADDR_ENCODER_1)
            except OSError:
                pass

    def _start_live_view(self) -> None:
        self.send(0x00, 0x81, REGISTER_PAYLOAD)                      # register as an app
        for i, name in enumerate(self.topics):
            self.send(0x00, 0x99, topic_subscription(name, 0x6A00 + i), ADDR_TOPICS)
        self.send(0x02, 0x09, bytes(10) + b"\x03")                   # live view source: camera
        self.request_keyframe()                                      # start transmitting
        if self.liveview == "action":
            # Action series: register with the video encoder and trigger it (then keep it alive).
            self.send(0x00, 0x81, APP_INFO_REPLY, ADDR_ENCODER_2, FLAG_WRITE)
            self.send(0x00, 0x88, ENCODER_REGISTER, ADDR_ENCODER_1)
            self._trigger_encoder()
        log("Registered and live view requested" + (" (Action series)" if self.liveview == "action" else ""))

    def request_keyframe(self) -> None:
        """Ask for the live view again: the camera answers with a fresh keyframe (not an Osmo Pocket 3 whose
        stream runs: see POCKET_JOIN_WAIT). Never in a loop: each request restarts the encoder (see
        client/watchdog.py)."""
        self.send(0x09, 0xA8, bytes((0, 4, 2, 0, 0, 0, 0, 0, 0, 0)))

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
                self._resend()
            except OSError:
                pass
            time.sleep(ACK_INTERVAL)

    def _receive(self) -> None:
        frames = self.frames = _FrameAssembler(self._deliver_video, join=self.liveview == "pocket")
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
            self._record(0, packet)
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
                frames.add(seq, packet[GROUP_OFFSET], packet[VIDEO_HEADER_SIZE:], now)
                continue
            if kind == Packet.LINK and len(packet) >= 34:
                # Until video flows (or while it is stalled), acknowledge the video cursor the camera
                # reports itself: some models don't start their video channel otherwise.
                if now - video_at > 0.5:
                    self._cursors[0] = struct.unpack_from("<H", packet, 10)[0]
                self._cursors[1] = struct.unpack_from("<H", packet, 18)[0]
                self._cursors[2] = struct.unpack_from("<H", packet, 26)[0]
                if self._cursors[2] != self._acked_cursor:
                    self._acked_cursor, self._last_ack = self._cursors[2], time.monotonic()
                    self._acknowledged(self._acked_cursor)
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
        self._open_dump()
        try:
            self._handshake()
            if self.liveview == "pocket":
                # acknowledgements run from the start: the Pocket's registration steps expect them
                threading.Thread(target=self._ack_loop, daemon=True, name="datalink-ack").start()
                if self.start_live_view:
                    self._start_pocket_live_view()
                threading.Thread(target=self._presence_loop, daemon=True, name="datalink-presence").start()
            elif self.start_live_view:
                self._start_live_view()
            self.ready.set()
            if self.liveview != "pocket":
                threading.Thread(target=self._ack_loop, daemon=True, name="datalink-ack").start()
            if self.liveview == "action":
                threading.Thread(target=self._heartbeat_loop, daemon=True, name="datalink-heartbeat").start()
            self._receive()
        finally:
            self.stopped.set()
            self._sock.close()
            if self._dump is not None:
                with self._dump_lock:
                    self._dump.close()

    def stop(self) -> None:
        self.stopped.set()

    def summary(self) -> str:
        """What the camera sent in this session (for the log when no video comes)."""
        names = {int(k): k.name.lower() for k in Packet}
        kinds = ", ".join(f"{names.get(k, k)} {n}" for k, n in sorted(self.kinds.items())) or "none"
        replies = ", ".join(f"{s:02x}/{i:02x} from {sender:02x}: {payload or '-'}"
                            for (s, i, sender), payload in list(self.replies.items())[:12]) or "none"
        short = f"; incomplete pictures: {self.frames.truncated}" if self.frames and self.frames.truncated else ""
        resent = f"; command packets sent again: {self.resent}" if self.resent else ""
        return f"camera packets: {kinds}; replies: {replies}{short}{resent}; {self.pictures_summary()}"

    def pictures_summary(self) -> str:
        """The pictures of this session by kind (IDR, I, P; +PS: with parameter sets), and the first ones."""
        frames = self.frames
        if frames is None or not frames.pictures:
            return "pictures: none"
        kinds = ", ".join(f"{k} {n}" for k, n in frames.pictures.most_common())
        return f"pictures: {kinds} (first: {', '.join(frames.timeline[:8])})"
