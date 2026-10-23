# OsmoCam

Use a **DJI Osmo Nano** as a webcam, wirelessly, over the camera's own Wi-Fi. Runs on **Windows, macOS
and Linux**.

OsmoCam receives the camera's live view and feeds it to a virtual camera (the **OBS Virtual Camera** on
Windows and macOS, a **v4l2loopback** device on Linux), which Zoom, Teams, Google Meet, FaceTime, Discord
and most other apps can pick as a webcam. A preview page in the browser lets you check the picture
without opening a video-call app.

> Unofficial and independent: not affiliated with, endorsed or sponsored by DJI. See [Legal](#legal).

> **Osmo Action 4: tested** by a user on a Raspberry Pi 4 with Linux: its H.265 live view into a
> v4l2loopback device, about 77,000 frames without a decoding error. `kill` now stops OsmoCam as cleanly as
> Ctrl+C (the camera's Bluetooth link is released).
>
> **New in 1.2 (experimental): Osmo Pocket 3, 4 and 4P** as a webcam, including the H.265 live view of
> the Pocket 4 and 4P. **Not tested with a real Pocket yet**: reports are welcome.
>
> **Since 1.1.5:** a steadier picture. A large frame from the camera is no longer cut in two (that broke
> the picture until the next keyframe).
>
> **Since 1.1: macOS and Linux.** On macOS, the connection and the live view are tested with the Osmo
> Nano (about 13 ms median latency); Linux with an Osmo Action 4 on a Raspberry Pi 4 (a user's tests). The
> OBS Virtual Camera on macOS is still being tested on real hardware. Reports are welcome.

- 1280×720 at 30 fps (the camera's live view stream), about 6 Mbit/s.
- Low latency: about 12 ms (median) from the first packet of a frame to the virtual camera, plus the
  camera's own capture and Wi-Fi delay.
- Reconnects by itself if the video stops; puts your Wi-Fi back when it exits.
- Tested with the Osmo Nano and the Osmo Action 4. Osmo Pocket 3, 4 and 4P: supported experimentally (not
  tested with a real one yet). Other DJI Osmo cameras (Action 5 Pro/6) use the same protocol and may work,
  fully or in part. The Osmo 360 is not supported.

Want to take photos, change modes, ISO and formats, or download your files to your computer? See the
companion app, **[Osmo Client](https://github.com/luisreysan/osmo-client)**.

## Requirements

- A computer with **Bluetooth LE** and **Wi-Fi**:
  - **Windows** 10 or 11.
  - **macOS** 12 or newer.
  - **Linux** with **NetworkManager** (Ubuntu, Fedora, Mint, Debian desktop and most others) and BlueZ.
- **[Python](https://www.python.org/downloads/) 3.10 to 3.14** (3.14 recommended). Python 3.15 is too
  new for now: some of the libraries used here (pyvirtualcam among them) have no build for it yet. If
  you already installed 3.15, install 3.14 as well: with several versions installed, OsmoCam picks the
  newest from 3.10 to 3.14 by itself.
- A **virtual camera**: [OBS Studio](https://obsproject.com/) on Windows and macOS, v4l2loopback on
  Linux (see below for each system). Without one you can still use the preview page (`--no-vcam`).
- **Internet through another adapter** (Ethernet, USB tethering or a second Wi-Fi adapter) if you need
  it while OsmoCam runs: the Wi-Fi adapter joins the camera's network. The first run also needs
  internet, to install the requirements.

## Install and run

Before you start: turn the camera on and close the DJI Mimo app on your phone (the camera accepts one
app at a time).

### Windows

1. Install **Python 3.14** from <https://www.python.org/downloads/windows/>.
2. Install [OBS Studio](https://obsproject.com/). OBS does not need to be open while OsmoCam runs.
3. Download the [latest release](https://github.com/luisreysan/osmocam/releases/latest)
   (**Source code (zip)**) and extract it.
4. Double-click **`osmocam.bat`**. The first run takes a minute: it creates a private Python environment
   and installs the requirements.
5. The first time, the camera shows **"OSMO"**: approve it on the camera screen within ~20 seconds.
6. In your video-call app, choose the camera **"OBS Virtual Camera"**.

Stop with **Ctrl+C** in the console window: the PC's Wi-Fi goes back to the network it was on.

### macOS

1. Install **Python 3.14**: on <https://www.python.org/downloads/macos/>, download the **macOS 64-bit
   universal2 installer** of the latest Python 3.14 and run it. (The `python3` that comes with macOS is
   too old. With Homebrew: `brew install python@3.14`.)
2. Set up the virtual camera, once:
   1. Install [OBS Studio](https://obsproject.com/) 30 or newer and open it.
   2. Click **Start Virtual Camera** (in the Controls panel, bottom right).
   3. macOS asks to allow OBS's **system extension**: open System Settings > Privacy & Security and
      click **Allow** for OBS. Restart the Mac if macOS asks you to.
   4. Click **Stop Virtual Camera** and quit OBS. It does not need to be open while OsmoCam runs.
3. Download the [latest release](https://github.com/luisreysan/osmocam/releases/latest)
   (**Source code (zip)**). Safari unzips it in your Downloads folder.
4. Open **Terminal** (press Cmd+Space, type `Terminal`, press Return). Type `cd ` (with a space at the
   end), drag the unzipped folder onto the Terminal window, and press Return. Then run:
   ```
   sh osmocam.sh
   ```
   The first run takes a minute: it creates a private Python environment and installs the
   requirements.
5. macOS asks whether **Terminal** may use **Bluetooth**: click **Allow**.
6. The first time, the camera shows **"OSMO"**: approve it on the camera screen within ~20 seconds.
7. In FaceTime, Zoom, Meet or another app, choose the camera **"OBS Virtual Camera"** (in a browser,
   allow it to use the camera when asked).

While OsmoCam runs, the Mac's Wi-Fi is on the camera's network, so there is no internet over Wi-Fi.
Stop with **Ctrl+C** in Terminal: OsmoCam makes the Mac forget the camera's network and switches Wi-Fi
off and on, and the Mac rejoins its usual network by itself within a few seconds.

Next time, open Terminal, `cd` to the folder again (or press the Up arrow to find the earlier commands)
and run `sh osmocam.sh`: nothing is installed again.

### Linux

1. Install Python 3.10 to 3.14 if your system doesn't have it. On Debian and Ubuntu, also run
   `sudo apt install python3-venv`.
2. Set up the virtual camera (v4l2loopback). On Ubuntu:
   ```
   sudo apt install v4l2loopback-dkms
   sudo modprobe v4l2loopback devices=1 exclusive_caps=1 card_label="OsmoCam"
   ```
   `exclusive_caps=1` lets Chrome and other browsers see it. The `modprobe` lasts until you restart.
3. Download the [latest release](https://github.com/luisreysan/osmocam/releases/latest)
   (**Source code (tar.gz)** or **(zip)**) and extract it.
4. In a terminal in that folder, run `sh osmocam.sh` (or `./osmocam.sh`). The first run takes a minute:
   it creates a private Python environment and installs the requirements.
5. The first time, approve **"OSMO"** on the camera screen.
6. In your video-call app, choose the camera **"OsmoCam"**.

Stop with **Ctrl+C**: the Wi-Fi goes back to the connection it was on.

### Preview, updates and options

The preview page is at <http://127.0.0.1:8090/>. To update, download the new release and start it the
same way: your linked cameras are kept, and the private Python environment is reused (it is updated
only when the requirements change).

Options, the same on every system (`osmocam.bat` on Windows, `sh osmocam.sh` on macOS and Linux):

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
   FFmpeg (PyAV) and hands each frame to the virtual camera. A live view that isn't 16:9 is
   letterboxed, never stretched.

The protocol is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md). The `camlink` package is reusable
for other projects.

## Troubleshooting

- **"No DJI Osmo camera found"**: turn the camera on, bring it closer, and make sure the phone app is
  not connected to it. `--scan` shows what the computer can hear.
- **Stuck at "approve"**: look at the camera screen and accept "OSMO" (the prompt lasts ~20 s).
- **No internet while it runs**: expected on the Wi-Fi adapter; use Ethernet or a second adapter.
- **"Virtual camera unavailable"**: OsmoCam carries on with the preview page only. Set up the virtual
  camera for your system (see [Install and run](#install-and-run)) and start OsmoCam again.
- **"OBS Virtual Camera" missing in an app**: install OBS Studio, then restart the video-call app. On
  macOS, start the virtual camera once from OBS first.
- **Linux, "No v4l2 loopback device found"**: run the `modprobe` line again (it is lost on restart).
- **"Python 3.10 to 3.14 is needed"** (or, with version 1.1.0 and older, **"Failed building wheel for
  winrt-…"**, **"Microsoft Visual C++ 14.0 or greater is required"** or **"No matching distribution found
  for pyvirtualcam"**): only Python 3.15 is installed, and some libraries have no build for it yet.
  Install Python 3.14 as well (see [Requirements](#requirements)) and start OsmoCam again: it picks 3.14
  by itself.
- **Windows, "Connection request failed with error 0x80342002"** (version 1.1.1 and older): Wi-Fi is
  turned off on the computer (the Wi-Fi button in the quick settings, or airplane mode). Newer versions
  turn it on by themselves; otherwise turn it on and try again. Bluetooth alone is not enough: the
  video comes over Wi-Fi.
- **macOS, "Bluetooth is not available" or no camera found**: allow Bluetooth for Terminal (or the app
  you started it from) in System Settings > Privacy & Security > Bluetooth, then start it again.
- **macOS 15 or newer, "No route to host"**: allow Terminal under System Settings > Privacy & Security >
  Local Network.
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
