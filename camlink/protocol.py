"""Addresses, identities and fixed payloads used by both the BLE and the Wi-Fi side."""

from __future__ import annotations

import struct

from .duml import FLAG_REQUEST, Message

# DUML addresses (byte 4/5 of a frame), named after the role they play in this protocol.
ADDR_APP = 0x02          # us: the client app
ADDR_CAMERA = 0x01       # camera control and status
ADDR_WIFI = 0x07         # Wi-Fi module: pairing and access point credentials (BLE)
ADDR_LIVEVIEW = 0x41     # live view transmitter (Wi-Fi datalink)
ADDR_ENCODER_1 = 0x28    # video encoder, Action series: app registration (Wi-Fi datalink)
ADDR_ENCODER_2 = 0x48    # video encoder, Action series: live view trigger and heartbeat (Wi-Fi datalink)
ADDR_SESSION = 0xF0      # BLE session wake / keepalive
ADDR_WAKE = 0x1C         # wakes the camera after pairing (BLE)

# Camera side of the Wi-Fi link (the same for every camera: each one runs its own access point).
CAMERA_HOST = "192.168.2.1"
CAMERA_SUBNET = "192.168.2."
POKE_PORT = 7001                 # TCP: one pairing frame here before the datalink starts
DATALINK_PORT = 9004             # UDP datalink of the Osmo cameras
ALT_DATALINK_PORT = 10004        # UDP datalink of some rebadged models (no TCP poke)
HTTP_MEDIA = f"http://{CAMERA_HOST}/v2"

# App identity presented when pairing. Same value the MIT-licensed osmosis project uses
# (https://github.com/KonradIT/osmosis); the token is what the camera shows on its approve prompt.
APP_IDENTITY = "284ae5b8d76b3375a04a6417ad71bea3"
PAIR_TOKEN = "osmo"


def _short_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return bytes((len(raw),)) + raw


def pairing_message() -> Message:
    """Pairing request (Wi-Fi module 0x07/0x45). Also the first frame sent to TCP 7001."""
    return Message(ADDR_APP, ADDR_WIFI, 0x8092, FLAG_REQUEST, 0x07, 0x45,
                   _short_string(APP_IDENTITY) + _short_string(PAIR_TOKEN))


def _app_record(gap: int, tail: int) -> bytes:
    return b"\x00APP" + bytes(gap) + b"\x02" + bytes(8) + b"\x02\x08" + bytes(tail)


# What we tell the camera about ourselves: sent once to register on the datalink (0x00/0x81), and as
# the answer whenever the camera asks the same question (it does, about once a second).
REGISTER_PAYLOAD = _app_record(38, 11)      # 64 bytes
APP_INFO_REPLY = _app_record(37, 10)        # 62 bytes
# Registration with the Action series' video encoder (0x00/0x88 to ADDR_ENCODER_1), as described by the
# MIT-licensed dji-vcam project (https://github.com/RB14/dji-vcam) for the Osmo Action 5 Pro.
ENCODER_REGISTER = bytes.fromhex("170046237c415050000000000002")


def topic_subscription(name: str, subscription_id: int) -> bytes:
    """Payload of a 0x00/0x99 subscription to a named status topic."""
    raw = name.encode("utf-8")
    return struct.pack("<4sI3xHH", b"\x02\x02\x00\x00", subscription_id, len(raw) + 6, len(raw)) + raw + bytes(4)
