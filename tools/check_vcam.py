"""Reads the OBS Virtual Camera the way a video-call app would and saves one frame, to check OsmoCam.

    python tools\\check_vcam.py [seconds] [out.jpg]
"""

import sys
import time

import av

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5
out = sys.argv[2] if len(sys.argv) > 2 else "vcam_frame.jpg"
container = av.open("video=OBS Virtual Camera", format="dshow", options={"rtbufsize": "64M"})
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
