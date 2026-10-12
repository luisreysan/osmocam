"""H.264 decoding of the live view (PyAV / FFmpeg), on its own thread."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

import av

from .util import log


class H264Decoder(threading.Thread):
    """Decodes the access units the datalink hands over (one complete frame each) and passes every
    decoded av.VideoFrame to `on_frame(frame, first_packet_time)`; the time is 0.0 for extra frames."""

    def __init__(self, on_frame: Callable[[av.VideoFrame, float], None], stop: threading.Event) -> None:
        super().__init__(daemon=True, name="h264-decoder")
        self.on_frame = on_frame
        self.stop_event = stop
        # Small queue: a 720p frame decodes in a few ms, so it stays near empty. The cap keeps a hiccup
        # from piling up seconds of stale frames, which would show up as lag.
        self._queue: queue.Queue[tuple[bytes, float] | None] = queue.Queue(maxsize=90)
        self.frames = 0
        self.errors = 0               # access units FFmpeg could not decode
        self.dropped = 0
        self.callback_errors = 0

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
    def _codec() -> av.CodecContext:
        codec = av.CodecContext.create("h264", "r")
        # Output each frame as soon as it is decoded: the live stream has no B-frames, nothing to reorder.
        codec.options = {"flags": "low_delay", "flags2": "fast"}
        codec.thread_type = "SLICE"
        return codec

    def run(self) -> None:
        codec = self._codec()
        while not self.stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                codec = self._codec()
                continue
            data, first_packet = item
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
