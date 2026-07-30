# The hardware in the current station, and what it can carry

Findings, with sources and measurements. Where something could not be measured
on the target it says so rather than estimating quietly.

**Summary for the procurement conversation:** the Pi 2B can almost certainly
carry *airband + camera + the two serial feeds*. What it cannot carry, on the
evidence, is that **plus** an SDR-based ADS-B decoder — but with a uAvionix ping
RX Pro doing ADS-B on a UART instead of a second dongle, that specific problem
goes away and the remaining risks are USB contention and the absence of a
real-time clock. Two of the three things I would spend money on are cheap.

---

## What is fitted

| | |
|---|---|
| Compute | Raspberry Pi 2B — ARMv7 Cortex-A7, 4 × ~900 MHz, 1 GB RAM |
| USB | One USB 2.0 host, shared with 100 Mbit Ethernet through the LAN9514 hub |
| Camera | Pi camera on the CSI ribbon |
| Weather | Airmar 110WX via USB-UART, NMEA 0183 |
| Airband | One RTL2838 (RTL2832U + R820T2) |
| ADS-B | uAvionix ping RX Pro over MAVLink on a USB-UART — **not yet connected** |

---

## 1. The Airmar 110WX does not measure rainfall

Verified against Airmar's product documentation and dealer specifications, not
assumed. The 110WX measures **ultrasonic wind speed and direction, air
temperature and barometric pressure**. Relative humidity is an **optional
module** — the unit is sold both ways — and dew point and heat index are
calculated from it. There is **no rain gauge, no visibility sensor, no
pyranometer, no GPS and no compass**.

The console renders a rain gauge (`rain_rate_mmh`, `rain_mm_today`), visibility
and a sky icon. **None of those has a source on this station.** `humidity_pct`
has one only if the RH module was ordered, and the telemetry schema *requires*
it.

What the station does about it: the device registry declares per-device
capabilities, and the driver publishes only what the instrument actually
reported. Absent, never zero — and since `humidity_pct` was made optional
(CONTRACT-QUESTIONS item 2) that is now schema-valid as well as honest. The
console strikes an absent reading through, distinctly from the dashes it shows
while waiting.

**The rain gauge is decided: there will not be one.** No 110WX variant has the
sensor, and the owner's answer is to leave rainfall struck through in the
console rather than fit a gauge. Omitting the rain fields is therefore the
correct long-term behaviour, not a placeholder, and no rain driver should be
written for this hardware.

Two values are derived station-side and labelled as derived:

- **gust** — peak of a rolling 10-minute window over 1 Hz wind samples. The
  instrument reports wind, not gusts.
- **true wind direction** — the relative angle plus a configured mast
  orientation, because the 110WX has no compass. Get that constant wrong and
  every wind reading is rotated.

Sources: [Airmar 110WX](https://www.airmar.com/Product/110WX),
[dealer specification](https://weatherscientific.com/products/airmar-110wx-nmea-0183-2000%C2%AE-weatherstation%C2%AE-no-relative-humidity-rs232),
[NauticExpo listing](https://www.nauticexpo.com/prod/airmar/product-22646-491496.html).

---

## 2. CPU: what was measured, and what could not be

**Measured here** (x86-64 Xeon 4310, Python 3.14), via `python -m gsu bench`,
which is in the repo so it can be re-run on the Pi:

| | CPU per tick | at 1 Hz |
|---|---|---|
| Full station tick, squelch closed | 0.32 ms | 0.03% of one core |
| Radio measurement (spectrum → floor → gate) | 0.13 ms | 0.01% |
| ADS-B poll, full MAVLink decode | 0.17 ms | 0.02% |
| Weather + power reads | <0.01 ms | ~0 |
| Radio tick with the gate open | 12.3 ms | 1.2% |

The 12.3 ms is **simulation only** — synthesising a second of audio in pure
Python. On real hardware that work belongs to the SDR pipeline in its own
process, and this agent's share of a tick is the 0.32 ms figure.

**Not measured: the Pi's own bench numbers.** The station now runs on a real
Pi 2B (§7), but `python -m gsu bench` has not been re-run there and folded in,
so the table above stays x86 and stays labelled. I will not
convert an x86 number into an ARM one and present it as a measurement. Even at a
pessimistic 50× penalty the agent is ~1.6% of one core, so *this software* is
not the constraint on that hardware. What is, on published third-party figures:

| Workload | Reported | Pi 2B estimate |
|---|---|---|
| `dump1090` at ~2.4 MSPS | ~15% CPU on a Pi 3B; 8–41% for dump1090-fa depending on kernel | **~25–70% of one core** |
| RTLSDR-Airband, 1 dongle at 2.5 MHz | "<15% of the main CPU time" on a Pi 3, using the GPU for the FFT; documentation states no overclock is needed on a Pi 2 for two dongles | **~25% of one core** |
| Pi camera H.264 | hardware encoder on the VideoCore IV | negligible CPU |
| This agent | measured above | ~1% of one core |

Pi 3B (Cortex-A53, 1.2 GHz) to Pi 2B (Cortex-A7, 900 MHz) is roughly 1.7–1.8×
per core, which is where the estimates come from. **Verify by running the real
workload on the real box** — `vmstat 1` alongside `top -H` for an hour will
settle it in a way no amount of arithmetic can.

Sources: [RTLSDR-Airband wiki](https://github.com/rtl-airband/RTLSDR-Airband/wiki),
[FlightAware discussions](https://discussions.flightaware.com/t/fyi-new-pi-kernel-causes-dump1090-fa-to-run-much-harder/67309),
[Raspberry Pi forums](https://forums.raspberrypi.com/viewtopic.php?t=366305).

**Conclusion on CPU:** airband + camera + two serial feeds looks comfortable —
call it 30–40% of one core of four. Adding an SDR-based ADS-B decoder on a
second dongle would roughly double it and still fit on paper, which is exactly
the kind of "fits on paper" that USB then breaks. See below.

---

## 3. USB is the real constraint, and it is shared with Ethernet

The Pi 2B has **one** USB 2.0 host controller, and the LAN9514 puts the Ethernet
MAC behind it. Everything contends: SDR, both UARTs, and every network packet.

| Device | Sustained | Notes |
|---|---|---|
| RTL-SDR at 2.4 MSPS, 8-bit I/Q | **4.8 MB/s = 38.4 Mbit/s** | continuous, isochronous-like bulk transfers |
| RTLSDR-Airband default 2.56 MSPS | 5.1 MB/s = 41 Mbit/s | reducible: the airband channel needs far less than 2.5 MHz |
| Airmar at 4800 baud | 4.8 kbit/s | trivial bandwidth, non-trivial interrupt overhead |
| ping RX Pro at 57600 baud | 57.6 kbit/s | same |
| Station uplink over Starlink | **10.7 kbit/s telemetry**, 138 kbit/s with a busy airband channel | measured from this agent |

Bandwidth is not the problem; the DWC OTG driver's interrupt behaviour is. Lost
SDR samples on Pi 1/2 with a dongle plus network traffic are a well-documented
failure, and they present as *degraded reception* — a squelch that misses weak
transmissions — rather than as an error anyone sees. Adding a third USB device
to that bus increases the contention.

**What I would do, in order:**

1. Drop the SDR sample rate. Airband needs a fraction of 2.5 MHz; halving it
   halves both the USB load and the FFT cost, and costs nothing that matters for
   one AM channel.
2. Keep ADS-B on the UART. The ping RX Pro decodes on the device and sends
   ~58 kbit/s of MAVLink instead of ~38 Mbit/s of raw I/Q. That is the single
   biggest saving available and it is already the plan.
3. Measure for lost samples on the real box (`rtl_test -s 2400000` for an hour,
   with the camera and the network busy) before committing.

---

## 4. There is no battery-backed clock

A Pi has no RTC. `contract/enrolment.md` §6 is blunt about what that costs: a
station with a wrong clock cannot authenticate, and if it believes its credential
has expired it cannot renew either — a site visit for a bad number.

The station refuses to enrol with an implausible clock and says so, and reports
`clock.implausible` as a critical health condition rather than proceeding. It
also now reports **what is disciplining the clock** — NTP, GPS or nothing — in
every health frame and on the setup page (`gsu/clock.py`, `discipline()`), so
"is that box's clock being kept" is answerable from a desk. That covers the
boot-at-epoch case and makes the silent case visible. It does **not** cover a
site that loses both power and connectivity for long enough to matter.

### The three sources, and what each is worth here

| | Accuracy | Survives a power cut | Needs the link | Cost |
|---|---|---|---|---|
| NTP over the uplink | ~10 ms | no | **yes** | nothing |
| DS3231 RTC | ±2 ppm, ~1 min/year | **yes** | no | ~£4 |
| GPS + PPS via chrony | <1 µs | no (but re-acquires in seconds) | no | ~£20 + an antenna with sky view |

They are complementary rather than alternatives, and the failure each one covers
is different:

- **NTP alone** is what is fitted today. It fails in exactly the case that
  matters most: the box reboots during an outage, comes up with no idea of the
  time, cannot reach anything to find out, and therefore cannot authenticate to
  the platform that would have told it. Its clock is wrong *because* the link is
  down, and the link is what would fix it.
- **An RTC** breaks that circle for a few pounds. It does not need the network,
  it does not need sky, and it holds time across a power cut — which is the only
  thing standing between "the site lost power overnight" and "the site lost
  power overnight and now needs a visit".
- **GPS** is the best clock of the three and the owner's stated intent, but it
  needs an antenna with a clear view and it re-acquires from cold rather than
  holding. It is the right long-term source and it is not a substitute for the
  RTC, because a GPS with no fix and no RTC still boots not knowing the year.

### Recommendation: fit a DS3231 now, and GPS when it arrives

**A DS3231 module is about £4** and removes an entire class of unattended
failure. It goes on the I²C header pins (3.3 V, GND, SDA on GPIO 2, SCL on
GPIO 3) and needs one line in `/boot/firmware/config.txt`:

```
dtoverlay=i2c-rtc,ds3231
```

After a reboot `/sys/class/rtc/rtc0` exists, the kernel reads the clock at boot
before anything else runs, and `gsu preflight` stops warning about it. Use a
DS3231 rather than a DS1307: the DS1307 is ±2 minutes a month, which is enough
drift to matter across a long outage, and it wants 5 V.

The reason to fit it even with GPS planned is the order of events at boot: the
RTC is read by the kernel in the first second, and a GPS fix takes 30 seconds to
several minutes from cold. The agent is trying to renew a credential in between.

**GPS goes into chrony, not into this software.** `gpsd` feeds chrony over SHM
and the PPS line is a kernel `pps-gpio` device. The station's code needs no
change at all when it arrives — `discipline()` starts reporting `source: "gps"`
because chrony's reference id becomes `PPS`. The configuration is in
DEPLOYMENT.md §11, and the reasoning for keeping GPS timing out of the agent is
in `gsu/clock.py`: a Python loop reading `$GPRMC` and calling `settimeofday`
would be a worse clock than the NTP it replaced, and would fight chrony for the
privilege.

**What I would spend, in order:** the DS3231 first because it is £4 and closes
the failure that strands sites; the GPS second because it is better and is
already the plan; and nothing at all on making the software cleverer about time,
because the software is not the weak part.

---

## 5. One dongle, one band — no longer a conflict, but keep the constraint

An RTL-SDR has one tuner and samples ~2.4 MHz at a time. Airband (108–137 MHz)
and ADS-B (1090 MHz) cannot be received simultaneously by one dongle, and
time-slicing is not a workaround: retuning costs settling time, and a
squelch-gated audio stream that drops out every few seconds because the receiver
went to look at 1090 MHz is worse than not having audio.

With the ping RX Pro handling ADS-B this does not arise. The design keeps the
constraint anyway, because it will: SDR tuners are **allocatable resources in
the device registry, keyed on serial number** — not USB index, which changes
between boots — and assigning one tuner to two slots is reported as a conflict
rather than silently half-working. Adding a second dongle later is an entry in
the inventory, not a restructure.

One caveat found while implementing it: **a dongle with no serial programmed
cannot be told apart from an identical one.** The console says so and suggests
`rtl_eeprom` before a second dongle is fitted.

---

## 6. What this hardware supports, plainly

| Stream | On this hardware |
|---|---|
| airband audio + radio telemetry | yes, with the SDR sample rate turned down |
| ADS-B | yes, **once the ping RX Pro is connected** — nothing today |
| weather | partly: wind, temperature, pressure. **No humidity unless the RH module is fitted, and no rainfall, visibility or sky at all** |
| power | no device specified yet |
| camera, snapshots | yes — MJPEG on the video channel, 640×480 at 2 fps by default. §6b has what it costs |
| camera, live 1080p30 | **unproven on this hardware**, and no longer a blocker: hardware sizing is a later decision. The encoder is an interface with a hardware and a software implementation, probed at start-up. §9 |
| floodlight | needs a relay and, for honest reporting, a read-back line |

Two *hardware* gaps rather than software ones. **Rainfall is now closed as a
decision, not fixed**: there will be no gauge, and the console strikes the
reading through. **The RTC is still worth spending money on** — it is a few
pounds against a class of unattended lockout that no amount of station-side care
fully removes.

---

## 7. What is deployed, and what is only written

The distinction that matters when reading anything above: some of this has been
run against real hardware and some has not, and this section is the honest
register of which. It was rewritten on 2026-07-29, the day the first real
station went live — **Station1**, a Pi 2B rev 1.1 on Raspberry Pi OS 13
(Trixie), armhf, with an ov5647 camera and an Airmar 110WX — and the caveats it
used to carry are retired below rather than deleted, because which claims
survived contact with hardware is itself information.

| | Status |
|---|---|
| The agent, the loop, telemetry, enrolment, renewal | Run continuously on x86-64, and **now live on the Pi 2B**: enrolled, publishing, renewing |
| TLS to the broker, CA pinning, refusal to downgrade | Verified against a real TLS-only Redis with a per-station ACL, and now in daily use from ARM |
| The NMEA decoder | **Fed by a real Airmar 110WX**, publishing real weather. MAVLink remains synthetic-only |
| The serial layer beneath them (`serialio.py`) | **Has opened a real UART** — the Airmar's — and runs it continuously |
| RTL-SDR airband | No driver. Reports `not supported by this software build` |
| Pi camera, snapshots (`camera/picsi.py`) | **Run against the real ov5647.** The picamera2 path carries the live 2 fps snapshot channel; the same path also ran inside the camera container |
| Pi camera, H.264 (`camera/h264.py`) | **Run against the hardware encoder.** The Pi 2B presents `/dev/video11`; `GSU_ENCODER=auto` chose it; 804 KB of 1080p30 H.264 in a 3-second test |
| The synthetic camera and its JPEG encoder | Run continuously. Output decoded with libjpeg (Pillow 12.3) and checked pixel for pixel |
| The synthetic H.264 source | Run. Output decoded with ffmpeg 7.0.2 at 640×480 and 1920×1080, no errors, picture correct |
| The on-demand stream logic | Run end to end against the synthetic encoder **and against `rpicam-vid` on the real camera**, including the sensor handover the synthetic rig could never show (the fixes are pinned in `tests/test_video.py`, `StartupContentionTests`) |
| The stream uplink to the platform | Verified against the running platform with the synthetic source (1080p30, 459 fragments, none dropped) and **from the real station with the real encoder: live stream end to end in a real browser, 208 fragments, `avc1.640028`.** Never over a real satellite link |
| The camera stack in a container | **Built (271 MB) and run on the Pi.** It enumerates the ov5647 and captures snapshots under exactly the overlay's constraints — and `rpicam-vid` takes a bus error in the encoder path (Debian armhf userland against a Raspbian host) and dies before one frame. Camera stations therefore run systemd; DEPLOYMENT.md §3 has the account and the route past it |
| `picamera2` vs `rpicam-jpeg` | Decided, reported, and now **measured**: picamera2 is the path in production on both host and container. The driver still states which path it took and why |
| The systemd unit | **Runs the live station** — after its `DeviceAllow` list gained `char-video4linux`, `char-media` and `char-dma_heap`, the three entries libcamera actually opens. An allow-list with the camera missing reads as "no cameras available" from a service whose own user can see the camera from a shell |
| `install.sh` | **Run end to end on the Pi.** It surfaced the uid mismatch between the host account and the image, now pinned to one number (10001) on both paths |
| ARMv7 itself | **The whole station runs on it** |

`python -m gsu bench`, `python -m gsu preflight`, `python -m gsu camera` and
`python -m gsu stream` were built so those rows could be closed by whoever
first had the hardware, rather than by arithmetic here. That is how each of
them was closed.

---

## 8. Video: snapshots, measured

Two video paths exist and they are different things. This section is the cheap
one — one complete JPEG per message on `gsu/{station_id}/video`, published
continuously at a low rate. §9 is the live H.264 stream.

**Measured here** (x86-64 Xeon 4310, Python 3.14), from the synthetic test card,
via `python -m gsu camera`. "Published" is the JSON payload that actually
crosses the link — base64 costs a third on top of the JPEG, exactly as it does
for audio (CONTRACT-QUESTIONS item 9) — and is the number to plan with:

| Resolution | JPEG | Published | CPU/frame | At 2 fps |
|---|---|---|---|---|
| 320×240 | 3.7 kB | 5.0 kB | 1.9 ms | **82 kbit/s** |
| **640×480** | **11.1 kB** | **14.9 kB** | **5.4 ms** | **245 kbit/s** |
| 1280×720 | 27.8 kB | 37.2 kB | 9.3 ms | 610 kbit/s |
| 1920×1080 | 58.8 kB | 78.5 kB | 16.5 ms | 1287 kbit/s |

Quality moves those by about ±10% between q40 and q90, which is **not** what
quality normally does and is a property of the synthetic source rather than of
JPEG: its test card is made of flat blocks, so there are no high-frequency
coefficients for a quantiser to throw away (`gsu/camera/jpeg.py` explains why
the encoder is built that way — a general one would cost hundreds of
milliseconds a frame on an ARMv7 core).

**The synthetic frame is 3–5× smaller than a real one.** A photographic 640×480
JPEG at reasonable quality is 30–60 kB (`contract/schemas/video.schema.json`),
so a real camera publishes 40–80 kB a frame and costs **640–1280 kbit/s at
2 fps**. Plan with that, not with the table above. The station measures and
reports its own figure in every health frame (`health.video.bytes_per_frame` and
`bitrate_bps`), so the real number arrives from the real hardware rather than
from arithmetic here.

An `available: false` frame is about 90 bytes and is rate-limited to 1 Hz, so a
station with no camera costs **0.7 kbit/s** to keep saying so. That is the price
of not going quiet, and it is the right price.

**The Pi now runs this, and the prediction that mattered held.** The table
above remains x86 numbers for the *synthetic* encoder — but the real station
does not use that path at all, exactly as predicted: picamera2 produces the
JPEG in hardware and the station only base64-encodes it, and the live 2 fps
channel runs on the Pi 2B without the CPU cost the synthetic arithmetic
feared. The station's own measured figures are in every health frame
(`health.video.bytes_per_frame`, `bitrate_bps`), which is the number to plan
any further site with.

---

## 9. The live stream: 1080p30 H.264, and which chip encodes it

The owner's requirement is **1080p at 30 fps, on demand**. That is not a setting
change on the snapshot path, it is a different format, and the reason is
arithmetic rather than preference:

| At 1080p30 | Per frame | Sustained |
|---|---|---|
| MJPEG, as the snapshot channel sends it | 200–400 kB | **50–100 Mbit/s** |
| H.264, hardware-encoded | ~12 kB average | **2–4 Mbit/s** |

A factor of about twenty-five. `contract/schemas/video.schema.json` says so in
its own description: if this ever needs smooth full-rate video, MJPEG is the
wrong answer and should be replaced rather than tuned.

### What was measured, here, and what it does and does not mean

**Reference encodes** (x264 `veryfast`, CRF 23, 1080p30, on this x86 machine —
*not* the Pi's encoder):

| Content | Bitrate |
|---|---|
| High motion (`testsrc2`, everything moving) | **5.4 Mbit/s** |
| Near-static scene, small moving element | **0.05 Mbit/s** |

A remote site is nearly always the second case and occasionally the first, which
is why 3 Mbit/s is the default target and why it is a *target*: the hardware
encoder is rate-controlled, so on this path bitrate is a setting and quality is
the variable. The honest question is not "how many bytes per second" — you
choose that — but "what does 1080p30 look like at 3 Mbit/s from this sensor",
and that needs the sensor.

**The station's own cost of carrying the stream** is negligible and is the one
thing that can be settled from here: it copies bytes and finds frame boundaries,
never touching a macroblock. Parsing measured at **1071 MB/s** on this machine —
0.04% of one core at 3 Mbit/s. Even at a pessimistic 50× penalty on ARMv7 that
is under 2%.

**The synthetic H.264 source is not a bandwidth measurement.** It emits real,
decodable H.264 built from `I_PCM` macroblocks — uncompressed samples — so the
platform can build against a genuine bitstream with no camera in existence. Same
picture, three ways: **18.1 Mbit/s** synthetic, **0.05 Mbit/s** through x264.
Use it to prove the pipe; never to size the link.

### Which encoder does the work is discovered, not assumed

There are two ways to make H.264 and this station implements both behind one
interface, probed at start-up and reported in telemetry
(`gsu/camera/h264.py`, `HardwareEncoder` and `SoftwareEncoder`;
`GSU_ENCODER=auto|hardware|software`):

| | Encoder | Cost |
|---|---|---|
| Pi 2/3/4 | Fixed-function block in the VideoCore, via V4L2 M2M (`/dev/video11`) | Near zero CPU |
| Pi 5 | **Believed to have none** — `libav`/x264 on the CPU instead | Real CPU work |

**The Pi 5 claim needs verifying before it decides a purchase.** The BCM2712 is
understood to have dropped the H.264 *encode* block that the earlier VideoCore
parts carry, trading it for a much faster CPU on which 1080p30 x264 is
plausible. That is my understanding and not something I have confirmed on
hardware — and it is the sort of thing that is cheap to check and expensive to
be wrong about, because the two boards then sit at opposite ends: hardware
encode with a weak CPU, or no hardware encode with a strong one.

Which is why the encoder is an interface rather than a code path. `auto` probes
for `/dev/video11`, prefers hardware when it is there, and falls back to
software; asking explicitly for one that is not available is **refused with the
reason rather than silently swapped**, because a quiet fallback would hide
exactly the fact somebody set the option to establish. Health telemetry carries
`video.stream.encoder`, `encoder_kind`, `encoder_choice` and the full probe
list, alongside the frame rate and bitrate actually achieved — so no one has to
work out later which path a given station was on.

### What was measured on the Pi 2B, and what is still open

The Pi 2B has now answered the questions it could answer:

| | Measured on Station1 |
|---|---|
| Hardware encode block | **Present and used.** `/dev/video11` exists; `GSU_ENCODER=auto` probed it and chose hardware |
| 1080p30 | **804 KB of H.264 in a 3-second test** — about 2.1 Mbit/s, inside the 3 Mbit/s target — and the live stream verified end to end in a real browser (208 fragments, `avc1.640028`) |
| CPU cost | As designed for a fixed-function block: the station copies bytes and finds frame boundaries; the encode itself is not the CPU's problem |

Still open, and still worth a number before it decides a purchase: **the Pi 5**
(believed to have no H.264 encode block — the software path exists for it and
has never met that board). The command that closes it is unchanged:

```bash
python -m gsu stream --seconds 30                    # the site default
python -m gsu stream --seconds 30 --encoder software # the Pi 5 path
python -m gsu stream --seconds 30 --size 1280x720
```

It prints the probe result, measured frames per second, measured bitrate and
dropped frames, and writes **fragmented MP4** to a file — the same container the
platform receives, so playing it back checks the container as well as the
camera. It says plainly when the encoder did not keep up. Things worth knowing
before running it on a 2B: `gpu_mem` has to be large enough for the encoder (the
default 64 MB split may not be), and §3's USB contention applies to the network
the stream leaves by rather than to the camera, which is on CSI.

### The uplink, measured against the running platform

fMP4 over a WebSocket (`wss://…/media/ingest`), muxed on the station, one
fragment per frame. Measured from the bench station to the live platform over
the LAN, with the synthetic encoder at the site default:

| | |
|---|---|
| 1080p30, 15 seconds | **459 fragments, 0 dropped, 0 resynchronisations, 30.1 fps measured** |
| Carried | 14–21 Mbit/s — synthetic `I_PCM` rates, not a camera's |
| Codec string sent | `avc1.420033`, derived from the encoder's own SPS |
| Initialisation segment | 651 bytes, sent once per session |
| Container overhead | ~120 bytes a frame — 4 kB/s at 30 fps |

That is the pipe proven at full frame rate; it is **not** a bandwidth
measurement, because the synthetic encoder's bitrate is uncompressed. A real
camera at 3 Mbit/s is about a fifth of what the LAN carried here.

**When the link cannot carry it**, which is the case that matters on Starlink:
the uplink asks whether the socket is writable, and if it is not within 250 ms
the fragment is **dropped whole** — nothing is queued and no partial frame is
ever written, because a half-written frame leaves the platform waiting on bytes
that never arrive, and a stall is much harder to see than a drop. After a drop
it waits for the next keyframe before sending again: the frames in between
depend on one that never arrived and would render as a smear that looks like a
broken camera rather than a busy link. Both counters — `dropped` and `resyncs` —
are in health telemetry, and `resyncs` is the one an operator feels, because it
is a gap in what they can see rather than a number of frames. Tested against a
server that accepts the connection and then stops reading
(`tests/test_video.py::MediaUplinkTests`).

### What it costs to leave off

Everything above is why the stream is **off by default and leased**: it starts
only when the platform asks and stops when the platform stops asking
(`gsu/stream.py`). At 3 Mbit/s, an hour of forgotten stream is 1.35 GB. The
snapshot channel at 640×480/2 fps is 245 kbit/s of synthetic frames or around
1 Mbit/s of real ones — a twelfth of the stream — and is what a console shows
when nobody is watching live.

## 10. The RTSP camera path: what is written, and what has never met a camera

The network-camera driver (`gsu/camera/rtsp.py`) is the long-term camera path —
the CSI ribbon was the easy test article — and this register exists so nobody
mistakes tested seams for tested hardware.

**Exercised, in unit tests with fake subprocesses and synthetic H.264:**

| Seam | How |
|---|---|
| URL construction | credentials percent-encoded, credentialled URLs refused, redaction everywhere a human or telemetry reads |
| Snapshot handling | complete-JPEG gate, dimensions parsed from the frame's own SOF, failure back-off, ffmpeg-absent reason |
| Remux command | `-c copy`, Annex B normalisation, no transcode flags anywhere |
| Remux pump | the ffmpeg stdout path driven with real synthetic Annex B through the same access-unit reader and fMP4 muxer as every other source |
| Stream seam | `StreamSession` takes the camera's own source, skips the sensor relinquish dance, paces the muxer to the camera's configured rate |

**Never exercised:** RTSP negotiation against a real camera, authentication
against a real camera, camera-side keyframe intervals, network loss mid-stream,
and the actual CPU cost on a Pi 2B of a per-snapshot decode running beside a
live remux. The ffmpeg command lines follow documented behaviour; the first
real camera will say whether the documentation was the whole story, exactly as
the first real CSI camera did (§7).

Two costs to know before fitting one:

* **Snapshots decode.** One frame per capture at `video_fps` — the only
  decoder anywhere in this station. At 1080p on a Pi 2B that is real work per
  frame; turn `video_fps` down before blaming the camera.
* **The stream is the camera's.** Bitrate, resolution and keyframe interval
  are set on the camera and remuxed as-is. The site's stream ceilings cannot
  narrow them, and the `fps` device parameter must match the camera's own
  setting or playback runs fast or slow. A camera not sending H.264 is
  refused with ffmpeg's own message — a Pi 2B cannot transcode and is never
  asked to.
