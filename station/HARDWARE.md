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

**Not measured: the Pi.** There is no Pi 2B on this machine and I will not
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
| camera | hardware is capable; **the contract has no media channel**, so nothing it produces can be sent |
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
register of which.

| | Status |
|---|---|
| The agent, the loop, telemetry, enrolment, renewal | Run continuously on x86-64. Not run on a Pi |
| TLS to the broker, CA pinning, refusal to downgrade | **Verified against a real TLS-only Redis with a per-station ACL.** Not on ARM |
| The NMEA and MAVLink decoders | Unit-tested against synthetic and hand-worked frames. Never fed by a real instrument |
| The serial layer beneath them (`serialio.py`) | **Never opened a real UART.** Its error paths are tested; its success path is not |
| RTL-SDR airband | No driver. Reports `not supported by this software build` |
| Pi camera | No driver, and no media channel in the contract to carry one |
| The systemd unit | Parses cleanly under `systemd-analyze verify`. Never started on a Pi |
| `install.sh` | Syntax-checked and read through. **Never run end to end on a Pi** |
| ARMv7 itself | Nothing in this repository has executed on it |

`python -m gsu bench` and `python -m gsu preflight` are both in the build so
that the first two rows of that table can be closed by whoever first has the
hardware in front of them, rather than by arithmetic here.
