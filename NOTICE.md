# Notices

## Trademarks

DJI, Osmo, Osmo Nano and DJI Mimo are trademarks of SZ DJI Technology Co., Ltd. They are used here only
to say which cameras this software works with. This project is independent and is not affiliated with,
endorsed by or sponsored by DJI. OBS Studio is a project of the OBS Project.

## Prior work

- **osmosis** by Konrad Iturbe — <https://github.com/KonradIT/osmosis> — MIT License. The camera model
  ids and the app identity used when pairing follow that project's documentation. Its license:

  > MIT License
  >
  > Copyright (c) 2026 Konrad Iturbe
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
  > associated documentation files (the "Software"), to deal in the Software without restriction,
  > including without limitation the rights to use, copy, modify, merge, publish, distribute,
  > sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
  > furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all copies or
  > substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
  > NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
  > NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
  > OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
  > CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

- **osmo_nano_webcam** by Matis-ard — <https://github.com/Matis-ard/osmo_nano_webcam> — showed how
  the Osmo Nano's live view can be received over Wi-Fi. No code from it is included here.

## Dependencies

Installed with pip from `requirements.txt` the first time the app runs; none of them are included in
this repository.

| package | license |
|---|---|
| [bleak](https://github.com/hbldh/bleak) | MIT |
| [PyAV](https://github.com/PyAV-Org/PyAV) | BSD-3-Clause; its wheels include FFmpeg libraries under their own (L)GPL licenses |
| [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU |
| [pyvirtualcam](https://github.com/letmaik/pyvirtualcam) | GPL-2.0 |

OsmoCam's own code is MIT. It uses pyvirtualcam (GPL-2.0) as a separately installed library to reach
the OBS Virtual Camera (Windows, macOS) or a v4l2loopback device (Linux). If you build a single bundled
executable that includes pyvirtualcam, that bundle as a whole must be distributed under the terms of the
GPL-2.0. OBS Studio (GPL-2.0) and v4l2loopback (GPL-2.0) are installed separately by the user.
