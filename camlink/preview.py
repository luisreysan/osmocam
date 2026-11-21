"""Local HTTP preview server (127.0.0.1 only) and image rotation helpers for OsmoCam.

Endpoints:
    /                          Full-bleed live view (accepts ?rotate=0|90|180|270&mirror=0|1)
    /stream                    MJPEG stream (accepts ?rotate=...&mirror=...; omits -> follows live state)
    /snapshot.jpg              Single JPEG frame (accepts ?rotate=...&mirror=...)
    /control                   Minimal web panel to toggle rotation & horizontal mirror live
    /api/rotate?deg=..&mirror= Set/get live rotation & mirror (returns JSON {"rotation", "mirror"})
"""

from __future__ import annotations

import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from PIL import Image

from .util import log

VALID_ROTATIONS = (0, 90, 180, 270)
# Pillow's ROTATE_* constants are counter-clockwise; map clockwise degrees to Pillow transposes.
_PIL_TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}

_CONTROL_HTML = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OsmoCam Control</title>
<style>
  body{margin:0;background:#111;color:#eee;font-family:-apple-system,system-ui,sans-serif;
       display:flex;flex-direction:column;align-items:center;gap:14px;padding:16px}
  img{max-width:100%;max-height:68vh;background:#000;border-radius:8px;border:1px solid #333}
  .bar{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
  button{padding:10px 16px;border-radius:8px;border:1px solid #444;background:#222;color:#eee;
         font-size:14px;cursor:pointer}
  button.active{background:#0a6fd1;border-color:#3a9cff;color:#fff;font-weight:600}
  button#mirrorBtn.active{background:#1a8f42;border-color:#2ec262}
  .hint{font-size:12px;color:#999;text-align:center;line-height:1.6}
  code{background:#222;padding:2px 6px;border-radius:4px;color:#ddd}
</style>
<div class="bar" id="btns">
  <button data-deg="0">0° Landscape</button>
  <button data-deg="90">90° CW</button>
  <button data-deg="180">180°</button>
  <button data-deg="270">270° CCW</button>
  <button id="mirrorBtn" type="button">⇄ Mirror: OFF</button>
</div>
<img id="view" src="/stream" alt="OsmoCam live view">
<div class="hint">
  OBS live-controlled URL: <code>http://127.0.0.1:PORT/</code><br>
  OBS fixed URL: <code>http://127.0.0.1:PORT/?rotate=90&amp;mirror=1</code> (<code>rotate=0|90|180|270</code>, <code>mirror=0|1</code>)
</div>
<script>
  const rotBtns = [...document.querySelectorAll('#btns button[data-deg]')];
  const mirrorBtn = document.getElementById('mirrorBtn');
  let state = { rotation: 0, mirror: false };
  function render(){
    rotBtns.forEach(b => b.classList.toggle('active', Number(b.dataset.deg) === state.rotation));
    mirrorBtn.classList.toggle('active', Boolean(state.mirror));
    mirrorBtn.textContent = state.mirror ? '⇄ Mirror: ON' : '⇄ Mirror: OFF';
  }
  async function update(query){
    const r = await fetch('/api/rotate?' + query);
    state = await r.json();
    render();
  }
  rotBtns.forEach(b => b.onclick = () => update('deg=' + b.dataset.deg));
  mirrorBtn.onclick = () => update('mirror=' + (state.mirror ? '0' : '1'));
  fetch('/api/rotate').then(r => r.json()).then(j => { state = j; render(); });
</script>
"""


def normalize_rotation(value: int | str | None) -> int | None:
    """Return 0, 90, 180 or 270 (clockwise degrees), or None when invalid/omitted."""
    if value is None or value == "":
        return None
    try:
        deg = int(value) % 360
    except (TypeError, ValueError):
        return None
    return deg if deg in VALID_ROTATIONS else None


def normalize_mirror(value: bool | int | str | None) -> bool | None:
    """Return True/False for mirror query flags ('1'/'0'/'true'/'false'/'on'/'off'), or None if omitted."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def rotate_image(image: Image.Image, degrees: int, mirror: bool = False) -> Image.Image:
    """Rotate a Pillow image clockwise by 0/90/180/270 degrees, then optionally flip left-to-right."""
    op = _PIL_TRANSPOSE.get(degrees)
    if op is not None:
        image = image.transpose(op)
    if mirror:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    return image


def start_preview(outputs, port: int, fps: int = 30) -> None:
    """Start the local preview & rotation/mirror control server on 127.0.0.1:`port`."""
    min_interval = 1.0 / fps if fps > 0 else 0.0
    control_bytes = _CONTROL_HTML.replace("PORT", str(port)).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            fixed_rot = normalize_rotation((params.get("rotate") or [None])[0])
            fixed_mirror = normalize_mirror((params.get("mirror") or [None])[0])

            if parsed.path == "/":
                q = {}
                if fixed_rot is not None:
                    q["rotate"] = str(fixed_rot)
                if fixed_mirror is not None:
                    q["mirror"] = "1" if fixed_mirror else "0"
                src = f"/stream?{urlencode(q)}" if q else "/stream"
                page = (
                    f"<!doctype html><title>OsmoCam</title><body style='margin:0;overflow:hidden;background:#000;"
                    f"display:grid;place-items:center;height:100vh'>"
                    f"<img src='{src}' style='width:100vw;height:100vh;object-fit:contain'>"
                ).encode("utf-8")
                self._send_bytes(200, "text/html; charset=utf-8", page)
                return

            if parsed.path == "/control":
                self._send_bytes(200, "text/html; charset=utf-8", control_bytes)
                return

            if parsed.path in ("/api/rotate", "/api/transform"):
                deg = normalize_rotation((params.get("deg") or params.get("rotate") or [None])[0])
                if deg is not None:
                    outputs.set_rotation(deg)
                mir = normalize_mirror((params.get("mirror") or [None])[0])
                if mir is not None:
                    outputs.set_mirror(mir)
                body = json.dumps({"rotation": outputs.rotation, "mirror": outputs.mirror}).encode("utf-8")
                self._send_bytes(200, "application/json", body)
                return

            if parsed.path not in ("/stream", "/snapshot.jpg"):
                self.send_error(404)
                return

            single = parsed.path == "/snapshot.jpg"
            if not single:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            last = -1
            last_sent = 0.0
            while True:
                with outputs.cond:
                    outputs.cond.wait_for(lambda: outputs.seq != last, timeout=2)
                    frame, last = outputs.latest, outputs.seq
                if frame is None:
                    continue
                if not single and min_interval:
                    wait = min_interval - (time.monotonic() - last_sent)
                    if wait > 0:
                        time.sleep(wait)
                rot = fixed_rot if fixed_rot is not None else outputs.rotation
                mir = fixed_mirror if fixed_mirror is not None else outputs.mirror
                image = rotate_image(frame.to_image(), rot, mir)
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=85)
                jpeg = buffer.getvalue()
                try:
                    if single:
                        self._send_bytes(200, "image/jpeg", jpeg)
                        return
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % len(jpeg) + jpeg + b"\r\n")
                    last_sent = time.monotonic()
                except OSError:
                    return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="preview").start()
    log(f"Preview: http://127.0.0.1:{port}/ ({fps} fps) | Control: http://127.0.0.1:{port}/control")
