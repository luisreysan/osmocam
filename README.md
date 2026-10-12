# OsmoCam

Use a **DJI Osmo Nano** as a Windows webcam, wirelessly, over the camera's own Wi-Fi.

OsmoCam receives the camera's live view and feeds it to the **OBS Virtual Camera**, which Zoom, Teams,
Google Meet, Discord and most other apps can pick as a webcam. A preview page in the browser lets you
check the picture without opening a video-call app.

> Unofficial and independent: not affiliated with, endorsed or sponsored by DJI. See [Legal](#legal).

- 1280×720 at 30 fps (the camera's live view stream), about 6 Mbit/s.
- Low latency: about 12 ms (median) from the first packet of a frame to the virtual camera, plus the
  camera's own capture and Wi-Fi delay.
- Reconnects by itself if the video stops; puts your Wi-Fi back when it exits.
- Other DJI Osmo cameras (Action 4/5 Pro/6, Pocket 3/4) use the same protocol and may work, fully or
  in part. Only the Osmo Nano has been tested. The Osmo 360 is not supported.

Want to take photos, change modes, ISO and formats, or download your files from Windows? See the
companion app, **Osmo Client for Windows**.

## Requirements

- Windows 10 or 11 with **Bluetooth LE** and a **Wi-Fi adapter**.
- **Internet through another adapter** (Ethernet, USB tethering or a second Wi-Fi adapter) if you need
  it while OsmoCam runs: the Wi-Fi adapter joins the camera's network.
- [Python](https://www.python.org/downloads/windows/) 3.10 or newer.
- [OBS Studio](https://obsproject.com/) installed, for the virtual camera. OBS does not need to be open.
  (Without it you can still use the preview page with `--no-vcam`.)

## Run it

1. Download or clone this repository.
2. Turn the camera on and close the DJI Mimo app on your phone (the camera accepts one client at a time).
3. Double-click **`osmocam.bat`**. The first run creates a private Python environment and installs the
   requirements (needs internet once).
4. The first time, the camera shows **"OSMO"**: approve it on the camera screen within ~20 seconds.
5. In your video-call app, choose the camera **"OBS Virtual Camera"**.

Preview: <http://127.0.0.1:8090/>. Stop with **Ctrl+C**: the PC's Wi-Fi goes back to the network it was
on.

### Options

```
osmocam.bat --no-vcam         preview page only (no OBS needed)
osmocam.bat --scan            list the DJI Osmo cameras nearby and exit
osmocam.bat --address XX:XX:XX:XX:XX:XX   use this camera (default: the one used last, else the first found)
osmocam.bat --interface "Wi-Fi 2"         use this Wi-Fi adapter (default: the first one)
osmocam.bat --preview-port 0  no preview page
osmocam.bat --help            everything else
```

`tools\check_vcam.py` reads the OBS Virtual Camera like a video-call app would and saves one frame, to
check that the picture gets through.

## How it works

1. **Bluetooth LE**: finds the camera, pairs (the "OSMO" prompt), wakes it and asks for the name and
   password of its Wi-Fi access point.
2. **Wi-Fi**: the PC joins that access point (the camera is `192.168.2.1`).
3. **Datalink**: a small UDP protocol carries the camera's H.264 live view; OsmoCam decodes it with
   FFmpeg (PyAV) and hands each frame to the OBS Virtual Camera.

The protocol is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md). The `camlink` package is reusable
for other projects.

## Troubleshooting

- **"No DJI Osmo camera found"**: turn the camera on, bring it closer, and make sure the phone app is
  not connected to it. `osmocam.bat --scan` shows what the PC can hear.
- **Stuck at "approve"**: look at the camera screen and accept "OSMO" (the prompt lasts ~20 s).
- **No internet while it runs**: expected on the Wi-Fi adapter; use Ethernet or a second adapter.
- **"OBS Virtual Camera" missing in an app**: install OBS Studio, then restart the video-call app.
- **The camera gets warm**: streaming over Wi-Fi uses power; keep it on its dock or a USB charger.

## Privacy

Everything stays on your PC. OsmoCam talks only to the camera and to `127.0.0.1`. The cameras you link
(Bluetooth address, name, and the camera's Wi-Fi password) are stored in
`%LOCALAPPDATA%\osmo-pc\cameras.json`, shared with Osmo Client. No telemetry.

## Legal

OsmoCam is an independent project, not affiliated with, endorsed or sponsored by DJI. DJI, Osmo,
Osmo Nano and DJI Mimo are trademarks of SZ DJI Technology Co., Ltd., used here only to name the
cameras this software works with. The repository contains no DJI code, firmware or assets. Its purpose
is interoperability: letting owners use their own camera with a Windows PC.

It is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). It may stop working after
a camera firmware update. Use it at your own risk.

Third-party notices and credits: [NOTICE.md](NOTICE.md).

## License

[MIT](LICENSE). Note that OsmoCam uses pyvirtualcam (GPL-2.0) as a separately installed library; see
[NOTICE.md](NOTICE.md).
