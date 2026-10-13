#!/usr/bin/env python3
"""OsmoCam: use a DJI Osmo camera as a webcam over Wi-Fi (unofficial).

    osmocam.bat / ./osmocam.sh             # the virtual camera + a preview page at http://127.0.0.1:8090
    osmocam.bat / ./osmocam.sh --no-vcam   # preview page only (no virtual camera needed)
    osmocam.bat / ./osmocam.sh --scan      # list the DJI Osmo cameras nearby and exit

The camera's live view (H.264, 1280x720 at 30 fps) arrives over the camera's own Wi-Fi access point and
is sent to a virtual camera, which Zoom, Teams, Meet and others can select: "OBS Virtual Camera" on
Windows and macOS (OBS Studio must be installed), a v4l2loopback device on Linux.

The first time, the camera asks to approve "OSMO" on its screen. While OsmoCam runs, the computer's Wi-Fi
is joined to the camera, so internet has to come from another adapter (Ethernet, USB tethering or a
second Wi-Fi adapter). Ctrl+C stops it and puts the Wi-Fi back on the previous network.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import av
import numpy as np
from bleak.exc import BleakError

from camlink import ble, datalink, models, wifi
from camlink.registry import DEFAULT_TRANSPORT, CameraRegistry, other_transport
from camlink.util import fine_timer, log
from camlink.video import H264Decoder

# The OBS Virtual Camera on Windows and macOS; on Linux, a v4l2loopback device (see the README).
SYSTEM = "linux" if sys.platform.startswith("linux") else sys.platform
VCAM_BACKEND = "v4l2loopback" if SYSTEM == "linux" else "obs"
VCAM_HINTS = {
    "win32": "install OBS Studio (it does not need to be open)",
    "darwin": "install OBS Studio 30 or newer, click Start Virtual Camera in it once and allow its system "
              "extension (System Settings > Privacy & Security), then start OsmoCam again",
    "linux": "load v4l2loopback: sudo modprobe v4l2loopback devices=1 exclusive_caps=1 card_label=OsmoCam",
}
LIVE_VIEW_SIZE = (1280, 720)        # the camera's live view; other sizes are scaled (and letterboxed) to it


class Latency:
    """Rolling latency, in ms, from the first packet of a frame to the frame leaving for the webcam."""

    def __init__(self) -> None:
        self._samples: list[float] = []
        self._lock = threading.Lock()

    def add(self, ms: float) -> None:
        with self._lock:
            self._samples.append(ms)

    def drain(self) -> tuple[float, float]:
        """(median, 95th percentile) of the samples since the last call."""
        with self._lock:
            samples, self._samples = sorted(self._samples), []
        if not samples:
            return 0.0, 0.0
        return samples[len(samples) // 2], samples[min(len(samples) - 1, int(len(samples) * 0.95))]


class Outputs:
    """Sends decoded frames to the virtual camera and keeps the latest one for the preview page."""

    def __init__(self, use_vcam: bool, fps: int) -> None:
        self.use_vcam = use_vcam
        self.fps = fps
        self.cam = None
        self.latest: av.VideoFrame | None = None
        self.seq = 0
        self.cond = threading.Condition()
        self.latency = Latency()
        self._vcam_lock = threading.Lock()   # the decoder thread sends while the main thread may close
        self._closed = False

    def on_frame(self, frame: av.VideoFrame, first_packet: float = 0.0) -> None:
        if self.use_vcam:
            self._to_vcam(frame)
        if first_packet:
            self.latency.add((time.perf_counter() - first_packet) * 1000)
        with self.cond:
            self.latest = frame
            self.seq += 1
            self.cond.notify_all()

    def open_vcam(self) -> None:
        """Open the virtual camera before connecting to the camera. Opening it can take several seconds,
        and fails when it is missing (macOS takes ~13 s to say so): done while video flows, that stalls the
        live view. Without it, OsmoCam carries on with the preview page only."""
        if not self.use_vcam:
            return
        import pyvirtualcam

        width, height = LIVE_VIEW_SIZE
        log("Opening the virtual camera...")
        try:
            cam = pyvirtualcam.Camera(width, height, self.fps, fmt=pyvirtualcam.PixelFormat.I420, backend=VCAM_BACKEND)
        except Exception as error:  # noqa: BLE001 - missing driver or extension: keep the preview page
            self.use_vcam = False
            log(f"Virtual camera unavailable: {error}")
            log(f"Carrying on with the preview page only. To use the camera as a webcam, {VCAM_HINTS[SYSTEM]}.")
            return
        with self._vcam_lock:
            self.cam = cam
        log(f"Virtual camera ready: {cam.device} {width}x{height} @ {self.fps} fps")

    def _to_vcam(self, frame: av.VideoFrame) -> None:
        with self._vcam_lock:
            if self._closed or self.cam is None:
                return
            try:
                self.cam.send(fit_i420(frame, self.cam.width, self.cam.height))
            except Exception as error:  # noqa: BLE001 - the device went away: keep the preview page going
                log(f"Virtual camera stopped: {error}")
                self.use_vcam = False
                with contextlib.suppress(Exception):
                    self.cam.close()
                self.cam = None

    def close(self) -> None:
        with self._vcam_lock:
            self._closed = True
            if self.cam is not None:
                self.cam.close()
                self.cam = None


def fit_i420(frame: av.VideoFrame, width: int, height: int) -> np.ndarray:
    """The frame as I420 at the virtual camera's size: scaled to fit and, when its shape differs (a 4:3
    live view), centred between black bars instead of stretched."""
    if (frame.width, frame.height) == (width, height):
        return (frame if frame.format.name == "yuv420p" else frame.reformat(format="yuv420p")).to_ndarray()
    scale = min(width / frame.width, height / frame.height)
    w, h = max(2, int(frame.width * scale) & ~1), max(2, int(frame.height * scale) & ~1)
    planes = frame.reformat(width=w, height=h, format="yuv420p").to_ndarray().ravel()
    out = np.empty(width * height * 3 // 2, np.uint8)
    y, u, v = out[:width * height], out[width * height:width * height * 5 // 4], out[width * height * 5 // 4:]
    y[:], u[:], v[:] = 16, 128, 128                                   # black
    x0, y0 = (width - w) // 4 * 2, (height - h) // 4 * 2              # even offsets, centred
    y.reshape(height, width)[y0:y0 + h, x0:x0 + w] = planes[:w * h].reshape(h, w)
    for target, source in ((u, planes[w * h:w * h * 5 // 4]), (v, planes[w * h * 5 // 4:])):
        target.reshape(height // 2, width // 2)[y0 // 2:(y0 + h) // 2, x0 // 2:(x0 + w) // 2] = source.reshape(h // 2, w // 2)
    return out.reshape(height * 3 // 2, width)


def start_preview(outputs: Outputs, port: int) -> None:
    """A local page (127.0.0.1 only) with the live picture, to check framing without a video-call app."""
    page = (b"<!doctype html><title>OsmoCam</title><body style='margin:0;background:#111;display:grid;"
            b"place-items:center;height:100vh'><img src='/stream' style='max-width:100%;max-height:100vh'>")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(page)
                return
            if self.path not in ("/stream", "/snapshot.jpg"):
                self.send_error(404)
                return
            single = self.path == "/snapshot.jpg"
            if not single:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
            last = -1
            while True:
                with outputs.cond:
                    outputs.cond.wait_for(lambda: outputs.seq != last, timeout=2)
                    frame, last = outputs.latest, outputs.seq
                if frame is None:
                    continue
                buffer = io.BytesIO()
                frame.to_image().save(buffer, "JPEG", quality=80)
                jpeg = buffer.getvalue()
                try:
                    if single:
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(jpeg)))
                        self.end_headers()
                        self.wfile.write(jpeg)
                        return
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % len(jpeg) + jpeg + b"\r\n")
                except OSError:
                    return
                time.sleep(1 / 20)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="preview").start()
    log(f"Preview: http://127.0.0.1:{port}/")


async def scan(seconds: float) -> None:
    log(f"Listening for DJI Osmo cameras for {seconds:.0f} s...")
    found = await ble.scan(seconds)
    if not found:
        log("No DJI Osmo camera found. Is it on and close to the computer?")
    for cam in sorted(found.values(), key=lambda c: -c["rssi"]):
        notes = [cam["power"] or "", "tested" if cam["tested"] else "untested", cam["unsupported"] or ""]
        print(f"  {cam['address']}  {cam['name'] or '?':20} {cam['model']:18} {cam['rssi']} dBm  "
              + ", ".join(n for n in notes if n))


async def run(args: argparse.Namespace) -> None:
    stop = threading.Event()
    outputs = Outputs(not args.no_vcam, args.fps)
    await asyncio.to_thread(outputs.open_vcam)
    decoder = H264Decoder(outputs.on_frame, stop)
    decoder.start()
    if args.preview_port:
        start_preview(outputs, args.preview_port)
    registry = CameraRegistry()
    address = args.address or (registry.last_used() or {}).get("address")
    home = args.home or wifi.current_network(args.interface)
    if home.lower().startswith(models.NAME_PREFIXES):
        home = ""                    # started while still on a camera's network: nothing to return to
    deadline = time.monotonic() + args.seconds if args.seconds else None
    link: ble.BleLink | None = None
    session: datalink.Datalink | None = None
    task: asyncio.Future | None = None
    joined = False

    def time_up() -> bool:
        return stop.is_set() or (deadline is not None and time.monotonic() > deadline)

    async def pair() -> ble.BleLink:
        new = ble.BleLink()
        await new.connect(address, args.approve_timeout)
        registry.remember(new)
        return new

    try:
        link = await pair()
        address = link.address
        sessions = 0
        while not time_up():
            sessions += 1
            if link is None or link.disconnected.is_set():
                log("Bluetooth link is down, waking the camera again")
                try:
                    link = await pair()
                except Exception as error:  # noqa: BLE001 - keep trying: the Wi-Fi may still be up
                    log(f"Could not wake the camera: {error}")
                    link = None
            camera = registry.get(address) or {}
            ssid, password = camera.get("ssid"), camera.get("password")
            joined = True
            await asyncio.to_thread(wifi.join, ssid, password, args.join_timeout, args.interface)
            transport = camera.get("transport") or dict(DEFAULT_TRANSPORT)
            if transport.get("poke", True):
                await asyncio.to_thread(datalink.poke)
            session = datalink.Datalink(port=transport.get("port", datalink.DATALINK_PORT), on_video=decoder.feed)
            decoder.reset()
            task = asyncio.ensure_future(asyncio.to_thread(session.run))
            started = time.monotonic()
            report = (started, 0, 0)
            while not task.done():
                await asyncio.sleep(0.5)
                now = time.monotonic()
                stalled = now - (session.last_video or started)
                if time_up():
                    session.stop()
                elif stalled > args.stall:
                    log(f"No video for {stalled:.1f} s, restarting the live view")
                    session.stop()
                if now - report[0] >= args.report:
                    frames, video_bytes = decoder.frames, session.stats["video_bytes"]
                    span = now - report[0]
                    median, p95 = outputs.latency.drain()
                    log(f"{(frames - report[1]) / span:5.1f} fps  {(video_bytes - report[2]) * 8 / span / 1e6:4.1f} Mbit/s  "
                        f"latency {median:3.0f}/{p95:3.0f} ms (median/p95)  dropped={decoder.dropped} "
                        f"errors={decoder.errors} session={sessions}")
                    report = (now, frames, video_bytes)
            try:
                await task
            except RuntimeError as error:
                if "handshake" in str(error):
                    other = other_transport(transport)
                    log(f"{error}; trying UDP {other['port']} next")
                    registry.set_transport(address, other)
            except Exception as error:  # noqa: BLE001 - a failed session is retried
                log(f"Live view session ended: {error}")
            if not time_up():
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        log("Ctrl+C: stopping...")
        raise
    finally:
        # Shut down in order: end the live view session (its thread would otherwise keep Python from
        # exiting), stop the decoder before closing the virtual camera it writes to, then give the Wi-Fi
        # back and release the camera.
        if session is not None:
            session.stop()
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=2)
        stop.set()
        decoder.join(timeout=2)
        outputs.close()
        if joined:
            await asyncio.to_thread(wifi.restore, home, args.interface)
        if link is not None:
            with contextlib.suppress(Exception):
                await link.close()
        log(f"Stopped. frames={decoder.frames} decode_errors={decoder.errors}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-vcam", action="store_true", help="don't send the picture to the virtual camera")
    parser.add_argument("--preview-port", type=int, default=8090, help="port of the preview page (0 = off)")
    parser.add_argument("--fps", type=int, default=30, help="frame rate announced to the virtual camera")
    parser.add_argument("--scan", action="store_true", help="list the DJI Osmo cameras nearby and exit")
    parser.add_argument("--address", help="Bluetooth address of the camera, as --scan lists it (default: the one "
                                          "used last, else the first DJI Osmo found)")
    parser.add_argument("--interface", help="Wi-Fi adapter to use: 'Wi-Fi 2' on Windows, en0 on macOS, wlan0 "
                                            "on Linux (default: the first one)")
    parser.add_argument("--home", help="Wi-Fi network to return to (default: the current one)")
    parser.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = run until Ctrl+C)")
    parser.add_argument("--stall", type=float, default=3.0, help="restart the live view after N s without video")
    parser.add_argument("--report", type=float, default=10.0, help="seconds between stats lines")
    parser.add_argument("--join-timeout", type=float, default=45)
    parser.add_argument("--approve-timeout", type=float, default=90)
    args = parser.parse_args()
    try:
        with fine_timer():
            asyncio.run(scan(8) if args.scan else run(args))
    except KeyboardInterrupt:
        pass
    except BleakError as error:
        log(f"Bluetooth is not available: {error} ({ble.UNAVAILABLE_HINT})")
        sys.exit(1)


if __name__ == "__main__":
    main()
