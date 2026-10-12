# DJI Osmo over Wi-Fi: protocol notes

How OsmoCam and Osmo Client for Windows talk to a DJI Osmo camera: Bluetooth LE to find, pair and wake
it, then the camera's own Wi-Fi access point for commands, status, live video and file downloads.

This is an unofficial, independent description written for interoperability. Everything here was
verified on an **Osmo Nano**. Other Osmo models share the transport (see [Models](#12-models)) but
their commands and values may differ. Code references point to the `camlink` package in this repository.

Notation: `02/E1` means command set `0x02`, command id `0xE1`. Payloads are hex bytes; multi-byte
integers are little-endian unless noted.

## Contents

1. [Overview](#1-overview)
2. [DUML frames](#2-duml-frames)
3. [Bluetooth LE](#3-bluetooth-le)
4. [The camera's Wi-Fi access point](#4-the-cameras-wi-fi-access-point)
5. [The UDP datalink](#5-the-udp-datalink)
6. [Camera control](#6-camera-control)
7. [Settings](#7-settings)
8. [Status pushes](#8-status-pushes)
9. [Media library and downloads](#9-media-library-and-downloads)
10. [Power and sleep](#10-power-and-sleep)
11. [Limits and warnings](#11-limits-and-warnings)
12. [Models](#12-models)

---

## 1. Overview

```
PC                                         camera
 │  BLE: connect, pair ("OSMO" approve)  ──▶  │
 │  BLE: wake, ask Wi-Fi SSID + password ──▶  │   (BLE stays up: keepalive every 1 s)
 │  Wi-Fi: join the camera's access point     │   192.168.2.1, WPA2-PSK
 │  TCP 7001: one pairing frame ("poke")  ──▶ │
 │  UDP 9004: handshake, then                 │
 │     ◀── video (H.264), status pushes,      │
 │         replies, camera requests           │
 │     ──▶ acks every 25 ms, commands,        │
 │         answers to camera requests         │
 │  HTTP 80: file downloads (/v2)             │
```

Only one client at a time: close the DJI Mimo app on the phone (or let it disconnect) first.

## 2. DUML frames

Every message, over BLE and inside the UDP datalink, is a DUML frame (`camlink/duml.py`):

| offset | size | field |
|---|---|---|
| 0 | 1 | `0x55` start of frame |
| 1 | 2 | u16: bits 0-9 total frame length, bits 10-15 version (`1`) |
| 3 | 1 | CRC-8 of bytes 0-2 |
| 4 | 1 | sender address |
| 5 | 1 | receiver address |
| 6 | 2 | u16 sequence; a reply carries the sequence of its request |
| 8 | 1 | flags: `0x40` request expecting a reply, `0x80` reply |
| 9 | 1 | command set |
| 10 | 1 | command id |
| 11 | n | payload |
| 11+n | 2 | u16 CRC-16 of everything before it |

- CRC-8: reflected, polynomial `0x8C`, initial value `0x77`.
- CRC-16: reflected, polynomial `0x8408`, initial value `0x3692`.
- Total length = 13 + payload length.

Addresses used here (`camlink/protocol.py`):

| address | role |
|---|---|
| `0x02` | the client app (us) |
| `0x01` | camera control and status |
| `0x07` | Wi-Fi module: pairing and access point credentials (BLE) |
| `0x41` | live view transmitter |
| `0xF0` | BLE session wake / keepalive |
| `0x1C` | wakes the camera after pairing (BLE) |

## 3. Bluetooth LE

### 3.1 Advertisement

Manufacturer data, company id `0x08AA`:

```
<model id u16> 40 <6-byte device address> <state>
```

`state` is `02` while the camera is awake and `03` while it sleeps, so the sleep state can be read
passively without connecting. Model ids are listed in [section 12](#12-models). Newer adverts may
carry a product type at bytes 10-11 when bit `0x04` of byte 5 is set. Scan responses without
manufacturer data still carry the name (`OsmoNano-XXXX`, ...).

### 3.2 GATT

| characteristic | use |
|---|---|
| `0000fff4-0000-1000-8000-00805f9b34fb` | notifications; write `01 00` after subscribing |
| `0000fff5-0000-1000-8000-00805f9b34fb` | DUML frames in both directions (write without response) |

Frames can arrive split over several notifications; reassemble per characteristic.

### 3.3 Connection sequence

| step | frame (sender → receiver, seq) | payload |
|---|---|---|
| subscribe to notifications on FFF4 and FFF5, write `01 00` to FFF4 | | |
| session wake | `00/2B` 0x02 → 0xF0, seq `0x802B` | `04 00` |
| pair | `07/45` 0x02 → 0x07, seq `0x8092` | `<len><app identity><len>"osmo"` |
| ← pairing status | `07/45` reply | byte 1: `01` already paired, `02` approval needed |
| ← approval result | `07/46` (may come as a request) | `01` approved; anything else = denied or the ~20 s prompt timed out |
| wake the camera | `53/10` 0x02 → 0x1C, seq `0x8053` | `00 00 00 00` |
| keepalive, every second | `00/2B` 0x02 → 0xF0, seq `0x802B` | `01 01` |
| ask SSID | `07/07` 0x02 → 0x07, seq `0x8007` | (empty) |
| ask password | `07/0E` 0x02 → 0x07, seq `0x800E` | (empty) |
| ← SSID / password | `07/07`, `07/0E` replies | `<result> <len> <ASCII>` |

The app identity is the 32-character string the MIT-licensed
[osmosis](https://github.com/KonradIT/osmosis) project uses (`camlink/protocol.py`). The first time, the
camera shows "OSMO" and the user must approve it on the camera screen; after that, pairing answers
"already paired". If no pairing status arrives, send the pairing frame again (the apps retry at 2.5 s
and 5 s).

### 3.4 The camera's own requests

The camera also sends requests (flags `0x40`) and treats the link as lost when they go unanswered.
Over BLE, answer each one with flags `0xC0`, the same command and sequence, addresses swapped, and the
request's payload echoed back, except `00/81` ("who are you"), which gets the app record:

```
00 'A' 'P' 'P' 00×37 02 00×8 02 08 00×10        (62 bytes)
```

### 3.5 Status topics over BLE

While connected over BLE the camera may push named status topics as `00/99` frames; see
[section 8.3](#83-named-status-topics-0099).

## 4. The camera's Wi-Fi access point

- SSID and password come from `07/07` / `07/0E` over BLE. The network is WPA2-PSK (AES).
- The camera is `192.168.2.1`; the PC gets an address in `192.168.2.0/24`.
- While the PC's Wi-Fi adapter is on the camera's network it has no internet through that adapter.
  On Windows, keeping Ethernet (or a second adapter) connected keeps internet working.
- Every Osmo camera uses the same address, so files listed on one camera must not be requested from
  another.

## 5. The UDP datalink

`camlink/datalink.py`.

### 5.1 Poke

Open TCP `192.168.2.1:7001`, send the BLE pairing frame (`07/45`, section 3.3), wait ~0.4 s, close. The
UDP datalink then answers on port **9004**. (Some rebadged models listen on UDP 10004 and need no
poke; the apps fall back to that if the handshake goes unanswered.)

### 5.2 Link header

Every UDP packet, both directions, starts with 8 bytes:

| offset | size | field |
|---|---|---|
| 0 | 2 | u16 `0x8000 \| length` (length includes the header, 14 bits) |
| 2 | 2 | u16 session id, random, chosen by the client |
| 4 | 2 | u16 sequence |
| 6 | 1 | packet type |
| 7 | 1 | XOR of bytes 0-6 |

Packet types:

| type | direction | content |
|---|---|---|
| 0 | both | handshake |
| 1 | camera → client | link packet; u16 at offset 18 = data cursor, u16 at offset 26 = extra cursor |
| 2 | camera → client | video |
| 3 | camera → client | link packet; its header sequence is the data cursor |
| 4 | client → camera | acknowledgement |
| 5 | client → camera | data: DUML messages |

DUML messages from the camera (replies, status pushes, its own requests) ride inside its non-video
packets: scan everything after the 8-byte header for DUML frames.

### 5.3 Handshake

The client picks a random session id and a base sequence (multiple of 8), then sends type-0 packets
(up to 30 tries, ~0.35 s apart) until the camera answers with a type-0 packet. Body (40 bytes):

```
<base seq u16>
64 00 64 00 c0 05 14 00 00 64 00       channel block (100, 100, 1472, 20, 0, 100)
00 01 90 01 c0 05 14 00 00 64 00       channel block (256, 400, 1472, 20, 0, 100)
14 00 64 00 c0 05 14 00 00 64 00       channel block (20, 100, 1472, 20, 0, 100)
01 01 04 01 02
```

These are the values the camera accepts; the meaning of each field is not documented.

### 5.4 Sequences and acknowledgements

- Sequences advance in steps of 8.
- Keep **two** sequences: one for link packets (handshake and acknowledgements) and one for data
  packets. After the handshake both start at `base + 8`. Data packets must not share the counter
  with acknowledgements: if they do, commands sent after the first acknowledgement are ignored.
- Every 25 ms send a type-4 acknowledgement (26-byte body): three groups of
  `<cursor u16> <cursor u16> 00 00 00 00` for the video, data and extra cursors, then `00 00`.
  - video cursor = header sequence of the last video packet,
  - data / extra cursors = taken from type-1 packets (offsets 18 and 26) and type-3 packets (header
    sequence). Both start at the base sequence.

### 5.5 Data packets

A type-5 packet carries one DUML frame after a 12-byte routing header:

```
<previous data seq u16> <this data seq u16> 00 00 00 00 <counter u8> 01 00 00
```

The link header's sequence is the data sequence too. `counter` increments by one per data packet.
The DUML frame inside uses sender `0x02`, receiver `0x01` (or `0x41` for `09/A8`), flags `0x40` and its
own message sequence (the apps start at `0xA000`).

### 5.6 Registration and live view

Right after the handshake, send:

| command | payload | purpose |
|---|---|---|
| `00/81` | `00 'A' 'P' 'P' 00×38 02 00×8 02 08 00×11` (64 bytes) | register as an app |
| `00/99` | topic subscription to `cam_status` (below) | status |
| `02/09` | `00×10 03` | live view source: camera |
| `09/A8` → receiver `0x41` | `00 04 02 00 00 00 00 00 00 00` | start transmitting video |

Topic subscription payload:

```
02 02 00 00 <subscription id u32> 00 00 00 <name length + 6 u16> <name length u16> <name> 00 00 00 00
```

(the apps use subscription id `0x69DF`.) Subscriptions to other topic names are refused (`E0`) on the
Nano.

### 5.7 Answering the camera's requests

Over the datalink the camera keeps sending requests: `00/81` about once a second (from `0x48`), `00/74`
(from `0x88`) and named topics as requests (`00/99` from `0x28`). Answer each with flags `0x80`, sender
`0x02`, receiver = the asker, the same command and sequence:

| request | answer payload |
|---|---|
| `00/81` | the 62-byte app record (section 3.4) |
| `00/88` whose payload starts with `19` | `1a 00 00 00 00` |
| anything else | `00` |

Left unanswered, the camera treats the link as lost: a camera sitting in its Vision Dock makes the dock
show "connecting…" every few seconds.

### 5.8 Video

Type-2 packets carry H.264 (Annex-B) after a 20-byte header; byte 16 is the frame id. All packets of
one frame share the frame id; concatenating their payloads gives one access unit. There is no
end-of-frame marker (byte 18 counts fragments but there is no total), but a frame arrives as a burst
of packets less than 0.3 ms apart followed by ~30 ms of silence, so a frame can be handed to the decoder
after ~5 ms without new packets. Packets of a frame already handed on can be dropped. Start decoding at
the first SPS (`00 00 00 01 67`).

The Nano's live view is 1280×720 at 30 fps (960×720 when the camera records 4:3), about 6 Mbit/s,
without B-frames.

### 5.9 Replies

A reply has flags `0x80`, the command of the request and its message sequence. The first payload byte is
the result:

| code | meaning |
|---|---|
| `00` | OK |
| `E0` | command not supported |
| `E3` | invalid parameter, or not allowed in the current state |
| `DF` | not available right now |
| `D8` | storage not available (e.g. no microSD in the dock) |

Send one command at a time and wait for its reply (a mode switch takes ~0.8 s).

## 6. Camera control

All in command set `0x02`, receiver `0x01`.

| action | command | payload | notes |
|---|---|---|---|
| capture mode | `02/E1` | `<mode>` | see below; the live view pauses ~1 s |
| take photo | `02/01` | `01` | photo mode |
| record start / stop | `02/02` | `01` / `00` | video, slow-mo, hyperlapse, super night |
| timelapse start / stop | `02/01` | `01` / `00` | `02/02` answers `DF` in timelapse; stop is accepted while starting (~5 s) |

Capture modes (`02/E1` value, also byte 57 of the status push):

| value | mode |
|---|---|
| `05` | Photo |
| `01` | Video |
| `28` | Super Night |
| `00` | Slow-mo |
| `02` | Timelapse |
| `0A` | Hyperlapse |

Not usable on the Nano: `02/10` (answers `E0`); pause / resume (`02/02` `02` / `03`) answer `00` but do
nothing.

## 7. Settings

Each capture mode keeps its own exposure and format: after `02/E1`, read them again.

### 7.1 Exposure

| action | command | payload | reply |
|---|---|---|---|
| read exposure mode | `02/1F` | | `00 <mode> 00` |
| set exposure mode | `02/1E` | `<mode> 00` | `01` Auto, `04` Manual (`02`/`03` → `E3`) |
| read ISO | `02/2B` | | `00 <code>` (`00` in Auto) |
| set ISO | `02/2A` | `<code>` | Manual only (`E3` in Auto) |
| read shutter | `02/29` | | `00 01 <u16> <decimal>` (`DF` in Auto) |
| set shutter | `02/28` | `01 <u16> <decimal> 00 00 00` | Manual only |

ISO codes (one stop each): `03`=100, `04`=200, `05`=400, `06`=800, `07`=1600, `08`=3200, `09`=6400,
`0A`=12800, `0B`=25600, `0C`=51200. (`01`/`02` behave like `03`, `0D` like `0C`.)

Shutter speed: a u16 where bit 15 set means 1/x seconds (x = bits 0-14) and clear means whole seconds,
plus a decimal digit. Examples: `0x8019` + 0 = 1/25 s, `0x800C` + 5 = 1/12.5 s, `0x0001` + 3 = 1.3 s.
In the code the two are combined as `u16 | decimal << 16`; the list of accepted values, from 1/8000 s to
30 s, is `SHUTTER_SPEEDS` in `camlink/camera.py`.

### 7.2 Video format

`02/18 [resolution, fps, 00, 00, slow-mo factor]`; read with `02/19` → `00 <resolution> <fps> 00 00 <factor>`.
Changing the format restarts the camera's encoder: the live view stops for 2-5 s.

| resolution code | format |
|---|---|
| `10` | 4K 16:9 (3840×2160) |
| `2D` | 2.7K 16:9 |
| `0A` | 1080p 16:9 |
| `67` | 4K 4:3 |
| `5F` | 2.7K 4:3 |
| `0C` | 1080p 4:3 |

| fps code | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| fps | 24 | 25 | 30 | 48 | 50 | 60 | 120 | 240 |

What each capture mode accepts (anything else answers `E3`):

| mode | resolutions | fps |
|---|---|---|
| Video | all six | 24-60 (1080p 4:3: up to 30) |
| Super Night | 16:9 ones | 24, 25, 30 |
| Timelapse, Hyperlapse | 16:9 ones | 25, 30 |
| Slow-mo | 4K, 2.7K, 1080p at 120 fps (factor 4); 1080p at 240 fps (factor 8) | |

**Aspect ratio.** The 4:3 codes are only accepted with the aspect set to 4:3 first, through the
parameter command `02/8E` with a one-byte TLV:

```
set: 01 01 <type u16> <length u8> <value>       type 0x0046: 1 = 4:3, 2 = 16:9
get: 00 01 <type u16>
```

Sending a 16:9 resolution switches the picture back to 16:9 but leaves the aspect setting at 4:3, so
set it to 2 explicitly in Video mode.

### 7.3 Photo format

`02/12 [size, ratio]`, read with `02/13` (photo mode only). Size `04` = L, `03` = M; ratio `00` = 4:3,
`01` = 16:9. L 4:3 = 36 MP, L 16:9 = 31 MP, M 4:3 = 9 MP, M 16:9 = 8 MP.

### 7.4 White balance

`02/2C [mode, kelvin / 100, 00, 00, 00]`, read with `02/2D`. Mode `00` = Auto, `06` = manual colour
temperature, 2000-10000 K. In Auto the camera remembers the last manual temperature.

## 8. Status pushes

After registration the camera pushes, without being asked:

| frame | from | rate | content |
|---|---|---|---|
| `02/80` | `0x01` | ~10/s | camera status (8.1) |
| `0D/02` | `0x05` | ~1/s | battery (8.2) |
| `00/99` | `0x28` | varies | named status topics (8.3), not always |

### 8.1 Camera status `02/80`

| offset | size | field |
|---|---|---|
| 0 | 1 | bits 6-7: record state (0 idle, 1 starting, 2 recording, 3 stopping); bits 3-5: non-zero while taking a photo |
| 2 | 1 | bit `0x08`: writing a file |
| 3 | 1 | bit `0x40`: the app's media mode is on |
| 4 | 1 | 0 photo/timelapse, 1 video, 2 playback on the camera |
| 5 | 4 | total storage, MB |
| 9 | 4 | free storage, MB |
| 13 | 4 | photos left (0 in video modes; frames left in timelapse) |
| 17 | 4 | seconds of video left at the current format (0 in photo mode) |
| 29 | 4 | seconds recorded in the current clip |
| 57 | 1 | capture mode (section 6) |

In playback, offsets 5-20 read 0. The frame rate and exposure are not in this push.

The camera reserves a fixed amount of storage per second of video and per photo, so
`free MB / seconds left` (or `/ photos left`) identifies the current format. Measured at 25 fps:

| free ÷ seconds left | ≈ Mbit/s | format |
|---|---|---|
| 11.903 | 95 | 4K 16:9 |
| 14.288 | 114 | 4K 4:3 |
| 7.135 | 57 | 2.7K 16:9 |
| 8.327 | 67 | 2.7K 4:3 |
| 4.750 | 38 | 1080p 16:9 |
| 5.347 | 43 | 1080p 4:3 |
| 12.143 | 97 | Slow-mo 1080p 4× |
| 14.582 | 117 | Slow-mo 1080p 8× |
| 14.527 | 116 | Slow-mo 2.7K 4× |
| 15.537 | 124 | Slow-mo 4K 4× |

| free ÷ photos left | photo |
|---|---|
| 1.209 MB | M 16:9 (8 MP) |
| 1.317 MB | M 4:3 (9 MP) |
| 2.714 MB | L 16:9 (31 MP) |
| 2.935 MB | L 4:3 (36 MP) |

48/50/60 fps use a higher bitrate (4K ≈ 114 Mbit/s, 1080p ≈ 57 Mbit/s).

### 8.2 Battery `0D/02`

| offset | size | field |
|---|---|---|
| 1 | 4 | voltage, mV |
| 5 | 4 | current, mA, signed (negative = discharging) |
| 17 | 2 | temperature, 0.1 °C |
| 20 | 1 | charge, % |

### 8.3 Named status topics `00/99`

Sometimes the camera publishes named topics (`cam_expo_param`, `cam_storage`, `cam_video_param_v2`, ...)
over BLE and over the datalink. They are optional: some sessions get none. Layout:

```
02 06 00 00 <seq u16> 00 00 00 00 00 <rest length u16> <name length u16> <name> 00×6 <data length u16> <data>
```

Those that arrive as requests (flags `0x40`) must be answered (section 5.7).

`cam_expo_param` gives the exposure actually in use, also in Auto: ISO = u32 at data offset 16
(any value, e.g. 2509 in Auto), shutter = u16 at data offset 20 (same encoding as section 7.1, without
the decimal digit).

## 9. Media library and downloads

`camlink/media.py`. The listing follows the media protocol documented by
[osmosis](https://github.com/KonradIT/osmosis).

1. **Media mode**: `02/0C [01 01 00 01]`. Status byte 3 gets bit `0x40` and the live view stops. Leave
   with `02/0C [01 01 00 00]`. Wait ~1.7 s after entering before the first listing. Without the live
   view the camera serves files about 6× faster (~45 MB/s instead of ~8 MB/s) and runs cooler.
2. **List a page**: `00/26`
   ```
   4a 00 2a 10 <counter u32> 00 00 <cursor u32> <page size u16> 0d 01 00 ff×8 00 01 00 00 00 00 00 00 00 00 00
   ```
   `counter` identifies the request. `cursor` `0x40000001` = newest file in internal memory,
   `0x00000001` = newest on the dock's microSD (`D8` when there is none). Page size up to `0x2D`.
   Next page: `cursor + number of records` (pages overlap by one record).
3. **Answer**: the reply is `00`, then `00/27` chunks arrive. Each starts with a 10-byte sub-header
   `4a <type> <length u16> <counter u16> <seq u16> 00 00` (type `04` start, `01` data, `03` end).
   Concatenating the data chunks in `seq` order gives the manifest.
4. **Manifest**: u32 record count, then per record a block anchored on a `19 06` tag at offset T:

   | offset | field |
   |---|---|
   | T-18 | u32 time: FAT date << 16 \| FAT time (can be an hour off; the file name carries the local time) |
   | T-14 | u32 size, bytes |
   | T-10 | u32 handle (bit 30 set = internal memory) |
   | T-6 | u16 duration, s |
   | T-2 | type: `03` video, `00` photo |

   followed by TLVs: `0d <len> <file name>`, `1a <len> 00 00 00 01 <path without extension>`,
   `1a <len> 00 00 00 02 <thumbnail path>`.
5. **Download**: the camera runs an HTTP server on port 80:
   `GET http://192.168.2.1/v2?storage=<1 internal | 0 dock microSD>&path=<path>.<EXT>`. Range requests
   work (206), so interrupted downloads can resume.
   - Thumbnail: the thumbnail path + `.THM` (JPEG, 256×144 or 256×192).
   - Low-resolution preview of a video: the video path with `.LRF` instead of `.MP4` (H.264 720p MP4,
     plays in a browser).

## 10. Power and sleep

- **Wake**: the normal BLE connection (section 3.3) wakes a sleeping camera; it keeps advertising while
  asleep.
- **Sleep / power off**: no command is known for the Nano. Releasing BLE and Wi-Fi lets the camera's
  idle timer put it to sleep by itself.

## 11. Limits and warnings

- One client at a time; close the phone app first.
- Many format changes in a row heat the camera (each restarts the encoder). Around 67 °C it stops
  charging and may refuse Wi-Fi until it cools down.
- Do not sweep unknown commands or values: sweeping capture-mode values has been reported to freeze a
  Nano until its battery was disconnected.

## 12. Models

Model ids in the BLE advertisement (from [osmosis](https://github.com/KonradIT/osmosis)). Only the
Osmo Nano has been tested with these apps.

| id | model | notes |
|---|---|---|
| `0x0010` | Osmo Action 2 | |
| `0x0012` | Osmo Action 3 | |
| `0x0014` | Osmo Action 4 | |
| `0x0015` | Osmo Action 5 Pro | |
| `0x0017` | Osmo 360 | WPA3 Wi-Fi and 360° video: not supported |
| `0x0018` | Osmo Action 6 | |
| `0x0019` | Osmo Nano | tested |
| `0x0020` | Osmo Pocket 3 | |
| `0x0021` | Osmo Pocket 4 | |
| `0x0022` | Osmo Pocket 4 Pro | |
