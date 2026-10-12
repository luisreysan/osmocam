"""DUML message framing.

Every message exchanged with the camera, over Bluetooth LE or inside the Wi-Fi datalink, is one frame:

    offset  size  field
    0       1     0x55, start of frame
    1       2     u16 LE: bits 0-9 = total frame length, bits 10-15 = version (1)
    3       1     CRC-8 of bytes 0-2
    4       1     sender address
    5       1     receiver address
    6       2     u16 LE sequence number (a reply carries the sequence of its request)
    8       1     flags: 0x40 = request that expects a reply, 0x80 = reply
    9       1     command set
    10      1     command id
    11      n     payload
    11+n    2     u16 LE CRC-16 of everything before it

Both CRCs are the reflected variants: CRC-8 polynomial 0x8C seeded with 0x77, CRC-16 polynomial
0x8408 seeded with 0x3692.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

START = 0x55
OVERHEAD = 13                 # header (11) + CRC-16 (2)
VERSION = 1
FLAG_REQUEST = 0x40
FLAG_REPLY = 0x80


def _reflected_table(poly: int) -> tuple[int, ...]:
    table = []
    for index in range(256):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ poly if value & 1 else value >> 1
        table.append(value)
    return tuple(table)


_CRC8_TABLE = _reflected_table(0x8C)
_CRC16_TABLE = _reflected_table(0x8408)


def crc8(data: bytes, value: int = 0x77) -> int:
    for byte in data:
        value = _CRC8_TABLE[value ^ byte]
    return value


def crc16(data: bytes, value: int = 0x3692) -> int:
    for byte in data:
        value = (value >> 8) ^ _CRC16_TABLE[(value ^ byte) & 0xFF]
    return value


@dataclass(frozen=True)
class Message:
    sender: int
    receiver: int
    seq: int
    flags: int
    cmd_set: int
    cmd_id: int
    payload: bytes = b""

    @property
    def command(self) -> tuple[int, int]:
        return self.cmd_set, self.cmd_id

    @property
    def is_reply(self) -> bool:
        return bool(self.flags & FLAG_REPLY)

    @property
    def needs_reply(self) -> bool:
        return self.flags & (FLAG_REQUEST | FLAG_REPLY) == FLAG_REQUEST

    def encode(self) -> bytes:
        size = OVERHEAD + len(self.payload)
        head = bytes((START,)) + struct.pack("<H", (size & 0x3FF) | VERSION << 10)
        frame = (head + bytes((crc8(head), self.sender, self.receiver))
                 + struct.pack("<HBBB", self.seq, self.flags, self.cmd_set, self.cmd_id) + self.payload)
        return frame + struct.pack("<H", crc16(frame))

    def answer(self, payload: bytes, flags: int = FLAG_REPLY, sender: int | None = None) -> "Message":
        """The reply to this message: same command and sequence, sent back to whoever asked."""
        return Message(self.receiver if sender is None else sender, self.sender, self.seq, flags,
                       self.cmd_set, self.cmd_id, payload)


class FrameReader:
    """Rebuilds messages from a byte stream that may split or join frames (BLE notifications), dropping
    noise and frames whose CRC does not match."""

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, data: bytes) -> list[Message]:
        self._pending += data
        messages = []
        buf = self._pending
        while True:
            start = buf.find(START)
            if start < 0:
                buf.clear()
                break
            del buf[:start]
            if len(buf) < 4:
                break
            if crc8(buf[:3]) != buf[3]:
                del buf[0]
                continue
            size = (buf[1] | buf[2] << 8) & 0x3FF
            if size < OVERHEAD:
                del buf[0]
                continue
            if len(buf) < size:
                break
            frame = bytes(buf[:size])
            del buf[:size]
            if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
                continue
            seq, flags, cmd_set, cmd_id = struct.unpack_from("<HBBB", frame, 6)
            messages.append(Message(frame[4], frame[5], seq, flags, cmd_set, cmd_id, frame[11:-2]))
        return messages


def parse_all(data: bytes) -> list[Message]:
    """Every complete message inside one buffer, such as a datalink packet."""
    return FrameReader().feed(data)
