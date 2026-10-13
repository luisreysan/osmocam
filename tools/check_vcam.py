"""Reads the virtual camera the way a video-call app would and saves one frame, to check OsmoCam.

    python tools/check_vcam.py [seconds] [out.jpg] [device]

The device defaults to "OBS Virtual Camera" on Windows and macOS, and to the first v4l2loopback device
on Linux (or give its path, e.g. /dev/video2, as OsmoCam logs it).
"""

import sys
import time
from pathlib import Path

import av


def loopback_device() -> str:
    """The first v4l2loopback device (they are named "OsmoCam" or "Dummy video device" by default)."""
    for name in sorted(Path("/sys/class/video4linux").glob("video*/name")):
        if any(word in name.read_text() for word in ("OsmoCam", "Dummy", "Loopback", "loopback")):
            return f"/dev/{name.parent.name}"
    return "/dev/video0"


seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5
out = sys.argv[2] if len(sys.argv) > 2 else "vcam_frame.jpg"
device = sys.argv[3] if len(sys.argv) > 3 else None
if sys.platform == "win32":
    container = av.open(f"video={device or 'OBS Virtual Camera'}", format="dshow", options={"rtbufsize": "64M"})
elif sys.platform == "darwin":
    container = av.open(f"{device or 'OBS Virtual Camera'}:none", format="avfoundation", options={"framerate": "30"})
else:
    container = av.open(device or loopback_device(), format="v4l2")
stream = container.streams.video[0]
print(f"Opened: {stream.codec_context.width}x{stream.codec_context.height} {stream.codec_context.pix_fmt}")
start = time.monotonic()
frames = 0
saved = False
for frame in container.decode(stream):
    frames += 1
    if not saved and time.monotonic() - start > 1.0:
        frame.to_image().save(out, quality=85)
        saved = True
    if time.monotonic() - start > seconds:
        break
elapsed = time.monotonic() - start
container.close()
print(f"Read {frames} frames in {elapsed:.1f} s ({frames / elapsed:.1f} fps), saved {out if saved else 'nothing'}")
