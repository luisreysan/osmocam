# OsmoCam

Use a **DJI Osmo Nano** as a webcam, wirelessly, over the camera's own Wi-Fi. Runs on **Windows, macOS
and Linux**.

OsmoCam receives the camera's live view and feeds it to a virtual camera (the **OBS Virtual Camera** on
Windows and macOS, a **v4l2loopback** device on Linux), which Zoom, Teams, Google Meet, Discord and most
other apps can pick as a webcam. A preview page in the browser lets you check the picture without
opening a video-call app.

> Unofficial and independent: not affiliated with, endorsed or sponsored by DJI. See [Legal](#legal).

> **macOS and Linux support is new** (version 1.1) and still being tested on real hardware. Windows is
> the platform tested end to end. Reports are welcome.

- 1280×720 at 30 fps (the camera's live view stream), about 6 Mbit/s.
- Low latency: about 12 ms (median) from the first packet of a frame to the virtual camera, plus the
  camera's own capture and Wi-Fi delay.
- Reconnects by itself if the video stops; puts your Wi-Fi back when it exits.
- Other DJI Osmo cameras (Action 4/5 Pro/6, Pocket 3/4) use the same protocol and may work, fully or
  in part. Only the Osmo Nano has been tested. The Osmo 360 is not supported.

Want to take photos, change modes, ISO and formats, or download your files to your computer? See the
companion app, **Osmo Client**.

## Requirements

- A computer with **Bluetooth LE** and **Wi-Fi**:
  - **Windows** 10 or 11.
  - **macOS** 12 or newer.
  - **Linux** with **NetworkManager** (Ubuntu, Fedora, Mint, Debian desktop and most others) and BlueZ.
- [Python](https://www.python.org/downloads/) 3.10 to 3.14 (3.14 recommended). Python 3.15 is too new
  for now: some of the libraries used here have no build for it yet. With several versions installed,
  the newest from 3.10 to 3.14 is used.
  - macOS: the `python3` that comes with macOS is too old; install Python 3.14 from python.org (or with
    Homebrew: `brew install python@3.14`).
  - Debian / Ubuntu: also `sudo apt install python3-venv`.
- **Internet through another adapter** (Ethernet, USB tethering or a second Wi-Fi adapter) if you need
  it while OsmoCam runs: the Wi-Fi adapter joins the camera's network.
- The virtual camera (without it you can still use the preview page with `--no-vcam`):
  - **Windows**: [OBS Studio](https://obsproject.com/) installed. OBS does not need to be open.
  - **macOS**: [OBS Studio](https://obsproject.com/) 30 or newer. Once: open OBS, click **Start Virtual
    Camera**, allow the system extension if macOS asks, then **Stop Virtual Camera** and quit OBS.
  - **Linux**: v4l2loopback. On Ubuntu:
    ```
    sudo apt install v4l2loopback-dkms
    sudo modprobe v4l2loopback devices=1 exclusive_caps=1 card_label="OsmoCam"
    ```
    (`exclusive_caps=1` lets Chrome and other browsers see it; the `modprobe` lasts until you restart.)

## Run it

1. Download or clone this repository.
2. Turn the camera on and close the DJI Mimo app on your phone (the camera accepts one client at a time).
3. Start OsmoCam. The first run creates a private Python environment and installs the requirements
   (needs internet once).
   - **Windows**: double-click **`osmocam.bat`**.
   - **macOS / Linux**: open a terminal in this folder and run **`./osmocam.sh`** (or `sh osmocam.sh`).
     On macOS, allow **Bluetooth** for Terminal when asked.
4. The first time, the camera shows **"OSMO"**: approve it on the camera screen within ~20 seconds.
5. In your video-call app, choose the camera **"OBS Virtual Camera"** (Windows, macOS) or **"OsmoCam"**
   (Linux).

Preview: <http://127.0.0.1:8090/>. Stop with **Ctrl+C**: the computer's Wi-Fi goes back to the network it
was on.

### Options

The same on every system (`osmocam.bat` on Windows, `./osmocam.sh` on macOS and Linux):

```
--no-vcam            preview page only (no virtual camera needed)
--scan               list the DJI Osmo cameras nearby and exit
--address ADDRESS    use this camera, as --scan lists it (default: the one used last, else the first found)
--interface NAME     Wi-Fi adapter to use: "Wi-Fi 2" on Windows, en0 on macOS, wlan0 on Linux
                     (default: the first one)
--home NETWORK       Wi-Fi network to go back to on Windows and Linux (default: the one the computer is on)
--preview-port 0     no preview page
--help               everything else
```

`tools/check_vcam.py` reads the virtual camera like a video-call app would and saves one frame, to
check that the picture gets through.

## How it works

1. **Bluetooth LE**: finds the camera, pairs (the "OSMO" prompt), wakes it and asks for the name and
   password of its Wi-Fi access point.
2. **Wi-Fi**: the computer joins that access point (the camera is `192.168.2.1`).
3. **Datalink**: a small UDP protocol carries the camera's H.264 live view; OsmoCam decodes it with
   FFmpeg (PyAV) and hands each frame to the virtual camera.

The protocol is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md). The `camlink` package is reusable
for other projects.

## Troubleshooting

- **"No DJI Osmo camera found"**: turn the camera on, bring it closer, and make sure the phone app is
  not connected to it. `--scan` shows what the computer can hear.
- **Stuck at "approve"**: look at the camera screen and accept "OSMO" (the prompt lasts ~20 s).
- **No internet while it runs**: expected on the Wi-Fi adapter; use Ethernet or a second adapter.
- **"OBS Virtual Camera" missing in an app**: install OBS Studio, then restart the video-call app. On
  macOS, start the virtual camera once from OBS first (see [Requirements](#requirements)).
- **Linux, "No v4l2 loopback device found"**: run the `modprobe` line from [Requirements](#requirements)
  (again after each restart).
- **macOS, "Bluetooth is not available" or nothing found**: allow Bluetooth for Terminal (or the app you
  started it from) in System Settings > Privacy & Security > Bluetooth, then start it again.
- **macOS 15 or newer, "No route to host"**: allow Terminal under System Settings > Privacy & Security >
  Local Network.
- **macOS, Wi-Fi after stopping**: OsmoCam makes the Mac forget the camera's network and switches Wi-Fi
  off and on; the Mac then rejoins its usual network by itself, in a few seconds.
- **Linux, "NetworkManager (nmcli) was not found"**: join the camera's Wi-Fi yourself; its name and
  password are in `cameras.json` (see [Privacy](#privacy)). OsmoCam carries on once you are connected.
- **The camera gets warm**: streaming over Wi-Fi uses power; keep it on its dock or a USB charger.

## Privacy

Everything stays on your computer. OsmoCam talks only to the camera and to `127.0.0.1`. The cameras you
link (Bluetooth address, name, and the camera's Wi-Fi password) are stored in `cameras.json`, shared
with Osmo Client:

- Windows: `%LOCALAPPDATA%\osmo-pc\cameras.json`
- macOS: `~/Library/Application Support/osmo-pc/cameras.json`
- Linux: `~/.local/share/osmo-pc/cameras.json`

The same folder holds OsmoCam's private Python environment. No telemetry.

## Legal

OsmoCam is an independent project, not affiliated with, endorsed or sponsored by DJI. DJI, Osmo,
Osmo Nano and DJI Mimo are trademarks of SZ DJI Technology Co., Ltd., used here only to name the
cameras this software works with. The repository contains no DJI code, firmware or assets. Its purpose
is interoperability: letting owners use their own camera with their computer.

It is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). It may stop working after
a camera firmware update. Use it at your own risk.

Third-party notices and credits: [NOTICE.md](NOTICE.md).

## License

[MIT](LICENSE). Note that OsmoCam uses pyvirtualcam (GPL-2.0) as a separately installed library; see
[NOTICE.md](NOTICE.md).
