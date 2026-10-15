"""Annex-B access units: start codes and the parameter sets that tell H.264 from H.265.

The Osmo Nano, the Pocket 3 and the Action series send H.264 (AVC); the Pocket 4 and 4 Pro send H.265
(HEVC). Parameter sets come only with keyframes: AVC SPS (NAL type 7) / PPS (8), HEVC VPS (32), SPS (33),
PPS (34), whose two-byte NAL headers start 40 01, 42 01, 44 01.
"""

from __future__ import annotations

from collections.abc import Iterator

H264, HEVC = "h264", "hevc"
CODEC_NAMES = {H264: "H.264", HEVC: "H.265"}


def nal_starts(unit: bytes, limit: int | None = None) -> Iterator[int]:
    """Offset of each NAL unit's first byte (right after a 00 00 01 start code) in unit[:limit]."""
    end = len(unit) if limit is None else min(limit, len(unit))
    at = unit.find(b"\x00\x00\x01", 0, end)
    while at >= 0:
        yield at + 3
        at = unit.find(b"\x00\x00\x01", at + 3, end)


def _kind(unit: bytes, at: int) -> str | None:
    if at + 1 >= len(unit):
        return None
    first, second = unit[at], unit[at + 1]
    if first in (0x40, 0x42, 0x44) and second == 0x01:        # HEVC VPS / SPS / PPS, layer 0
        return HEVC
    if not first & 0x80 and first & 0x1F in (7, 8):            # AVC SPS / PPS
        return H264
    return None


def parameter_sets(unit: bytes, limit: int | None = None) -> tuple[int, str] | None:
    """(offset of the start code, codec) of the first parameter set in unit[:limit], or None. The offset
    includes a 4-byte start code's leading zero."""
    for at in nal_starts(unit, limit):
        kind = _kind(unit, at)
        if kind:
            start = at - 3
            return (start - 1 if start and unit[start - 1] == 0 else start), kind
    return None


def codec_of(unit: bytes, limit: int = 2048) -> str | None:
    """H264 or HEVC when the access unit starts with parameter sets (a keyframe), otherwise None."""
    found = parameter_sets(unit, limit)
    return found[1] if found else None
