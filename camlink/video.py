"""Live view decoding (PyAV / FFmpeg), on its own thread: H.264 (Osmo Nano, Pocket 3, Action series) or
H.265 (Pocket 4 and 4 Pro), picked from the parameter sets of the stream's keyframes."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

import av

from .nal import CODEC_NAMES, H264, codec_of
from .util import log


class VideoDecoder(threading.Thread):
    """Decodes the access units the datalink hands over (one complete frame each) and passes every
    decoded av.VideoFrame to `on_frame(frame, first_packet_time)`; the time is 0.0 for extra frames. The
    codec (H.264 or H.265) is chosen from the first keyframe, and again if a keyframe of the other one
    arrives."""

    def __init__(self, on_frame: Callable[[av.VideoFrame, float], None], stop: threading.Event) -> None:
        super().__init__(daemon=True, name="video-decoder")
        self.on_frame = on_frame
        self.stop_event = stop
        # Small queue: a 720p frame decodes in a few ms, so it stays near empty. The cap keeps a hiccup
        # from piling up seconds of stale frames, which would show up as lag.
        self._queue: queue.Queue[tuple[bytes, float] | None] = queue.Queue(maxsize=90)
        self.frames = 0
        self.errors = 0               # access units FFmpeg could not decode
        self.dropped = 0
        self.callback_errors = 0
        self.codec_name: str | None = None   # nal.H264 or nal.HEVC, once known

    def feed(self, data: bytes, first_packet: float = 0.0) -> None:
        try:
            self._queue.put_nowait((data, first_packet))
        except queue.Full:
            self.dropped += 1

    def reset(self) -> None:
        """A new live view session starts: drop what is still queued (stale) and restart the decoder."""
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        self._queue.put(None)

    @staticmethod
    def _codec(kind: str = H264) -> av.CodecContext:
        codec = av.CodecContext.create(kind, "r")
        # Output each frame as soon as it is decoded: the live stream has no B-frames, nothing to reorder.
        codec.options = {"flags": "low_delay", "flags2": "fast"}
        codec.thread_type = "SLICE"
        return codec

    def run(self) -> None:
        codec: av.CodecContext | None = None
        while not self.stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                codec, self.codec_name = None, None   # a new session: the codec is found again
                continue
            data, first_packet = item
            kind = codec_of(data)
            if kind and kind != self.codec_name:
                codec, self.codec_name = self._codec(kind), kind
                log(f"Live view: {CODEC_NAMES[kind]}")
            if codec is None:
                continue                 # no keyframe yet
            try:
                frames = codec.decode(av.Packet(data))
            except av.error.FFmpegError:
                self.errors += 1
                continue
            for index, frame in enumerate(frames):
                self.frames += 1
                try:
                    self.on_frame(frame, first_packet if index == 0 else 0.0)
                except Exception as error:  # noqa: BLE001 - a failing consumer must not stop the decoder
                    self.callback_errors += 1
                    if self.callback_errors == 1:
                        log(f"Could not deliver a decoded frame: {error!r}")


H264Decoder = VideoDecoder                   # the name before H.265 support
