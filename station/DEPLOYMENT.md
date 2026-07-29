# Deploying a ground station

From a clean Raspberry Pi to a station the platform accepts data from.

This is the runbook. It assumes you are at a desk with the box in front of you
and an SSH session to it; if you are on a hillside with a phone, you want §7
onwards and somebody else should have done §1–§6 first.

**Nothing here uses `--insecure`, and nothing here disables certificate
verification.** If a step fails on a certificate, the certificate is the
problem. A documented step that skips verification is how skipping becomes
normal.

---

## What you need before you start

**Hardware**

| | |
|---|---|
| Raspberry Pi 2B | ARMv7, 1 GB RAM. See HARDWARE.md for what it will and will not carry |
| SD card | 8 GB or more, Class 10. This is the part that wears out |
| Airmar 110WX | on a USB-UART |
| uAvionix ping RX Pro | on a USB-UART. Not yet connected as of HARDWARE.md |
| RTL2838 | airband. **No driver in this build** — see §10 |
| Pi camera | CSI ribbon. Driven: snapshots on the video channel, and live H.264 on demand. **Never yet run against a real camera** — see §10 |
| Network | Ethernet or a Starlink terminal. The Pi 2B has no onboard Wi-Fi |

**Software**

Raspberry Pi OS **Bookworm** (32-bit, armhf) or newer. Bookworm ships Python
3.11; the agent needs 3.11 or later because it uses `datetime.UTC`. Bullseye
ships Python 3.9 and **will not run this** — that is an OS upgrade, not a patch,
and the installer refuses rather than half-working.

**From the platform admin, before you go anywhere**

1. **The platform URL and the broker URL**, with ports. For example
   `https://platform.example.net:8000` and `rediss://platform.example.net:6380/0`.
   They may be different hosts — the API is moving behind a reverse proxy and
   the broker is not.
2. **Whether the API has a public certificate yet.** If it is still serving its
   own, you need its CA as a PEM file and its SHA-256 fingerprint. See §4.
3. **Its SHA-256 fingerprint**, told to you separately from the file itself —
   read out, or from a message you already trust. Checking a file against itself
   proves nothing.
4. **A station record** created in the right organisation, and **an enrolment
   code** for it. The code is short-lived — 24 hours by default — so get it when
   you are ready to use it, not a fortnight before (`DECISIONS.md`, open
   decision 3).

You do **not** need to carry the broker's CA. It arrives in the enrolment
response and is pinned from then on.

You never type the station's UUID. It comes back in the enrolment response.

**The station runs as a container.** That is the deployment path and this
runbook is it. Running it as a plain systemd service is fully supported and
documented in **Appendix B**; use that if you would rather not have Docker on
the box.

The reason containers won is the owner's constraint, not a preference about
packaging: *once these stations are installed they are going to be difficult to
physically access.* An update is then the highest-risk routine operation there
is, and a container makes it atomic and makes the rollback a tag already on the
disk — no download, over a link that may be the reason you are rolling back.
§14 is that mechanism and is the most important section here.

**Isolation was traded away deliberately**, on the owner's instruction, because
nothing else runs on this box. The container gets the host's whole `/dev` and
broad device permissions so that a missing sensor cannot stop the station
starting and a replugged one is picked up without anybody touching it. See
`DECISIONS.md` item 35c.

---

## 1. Prepare the Pi

Flash Raspberry Pi OS Lite (64-bit will not run on a 2B; use the 32-bit image).
Enable SSH and set a user in Raspberry Pi Imager's advanced options, or put an
empty `ssh` file on the boot partition.

Then, on the box:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y docker.io docker-compose-v2 chrony
sudo systemctl enable --now docker
sudo raspi-config     # expand the filesystem; set the hostname and timezone
```

(For the systemd path instead, install `python3-venv` rather than Docker.)

**Time.** Install `chrony` even though `systemd-timesyncd` is present — chrony
is what a GPS time source will later plug into, so putting it in now means the
GPS upgrade is a config file rather than a change of daemon (§11).

**The SDR, if one is fitted.** The kernel's DVB driver grabs RTL2832U devices on
sight and then nothing else can open them:

```bash
echo -e 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830' \
  | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo reboot
```

The symptom if you skip it is "device busy" on a dongle nothing else is using.

**Reduce SD card wear.** This box writes an event database and audio recordings
continuously and an SD card is the most likely hardware failure on a remote
site. `sudo systemctl disable --now man-db.timer apt-daily.timer
apt-daily-upgrade.timer` removes the largest incidental writers.

---

## 2. Copy the code

From your machine:

```bash
rsync -a --exclude var/ --exclude .venv/ --exclude __pycache__ \
  ~/percepta/station/ pi@<box>:/tmp/station/
```

`var/` is excluded deliberately: it holds a credential and a device inventory
belonging to whichever box it came from, and copying one station's identity onto
another is precisely what enrolment exists to prevent.

---

## 3. Install

```bash
# While the platform serves its own certificate (the arrangement today):
sudo /tmp/station/deploy/install.sh --api-ca /tmp/platform-api-ca.pem

# Once it is behind a proxy with a public certificate:
sudo /tmp/station/deploy/install.sh

# The systemd path instead:
sudo /tmp/station/deploy/install.sh --path systemd
```

`--path docker` is the default. It builds the image, tags it
`percepta/gsu:current`, installs both unit files but enables only the ones that
path needs, and enables the update timer.

It is idempotent — re-run it to upgrade — and it never overwrites
`/etc/percepta/gsu.env`, the state directory, or an existing device inventory.

What it does:

| | |
|---|---|
| `/opt/percepta/station` | the code and `deploy/`, owned by root |
| `percepta/gsu:current` | the image the container runs. The updater moves this tag |
| `/etc/percepta/gsu.env` | configuration, `0640 root:gsu` |
| `/etc/percepta/platform-api-ca.pem` | the API's CA, if you pinned it |
| `/var/lib/percepta-gsu` | state: credential, pinned broker CA, inventory, events, recordings. `0700 gsu` |
| user `gsu` | system account, no login, in `dialout`, `video`, `plugdev` |
| `/etc/systemd/system/gsu-update.{service,timer}` | the update check, timer enabled |
| `/etc/systemd/system/gsu.service` | the systemd path, installed but **disabled** |
| `/etc/udev/rules.d/99-percepta-sdr.rules` | so the SDR is readable without root |

**Check any CA fingerprint it prints against the one you were told.** This is
the only step in the whole procedure that a person has to verify by eye, and
what follows rests on it.

No network at the site? `pip download -r requirements.txt -d deploy/wheels` on a
machine that has one, copy `deploy/wheels` across, and use `--offline`.

---

## 4. Configure

```bash
sudo nano /etc/percepta/gsu.env
```

The shipped file points at `example.net`. Set at least:

```sh
GSU_PLATFORM_URL=https://platform.example.net:8000
GSU_BROKER_URL=rediss://platform.example.net:6380/0
GSU_REQUIRE_TLS=1

# Only while the platform serves its own certificate:
GSU_API_CA_FILE=/etc/percepta/platform-api-ca.pem
```

Host and port are both yours to set on both URLs, and they may be different
hosts.

### The two trust roots, because they are not the same root

This trips people up once and then never again, so it is worth the paragraph.

| | Verified against | You configure |
|---|---|---|
| **Broker** `rediss://` | a **pinned private CA**, always | nothing — it arrives in the enrolment response and is persisted at `$GSU_HOME/broker-ca.pem`, 0600 |
| **Platform API** `https://` | the **system CA bundle** by default | `GSU_API_CA_FILE` to pin it instead |

`broker.ca_pem` in the enrolment response is the **broker's** trust root. The
field is named for what it is. Using it for the API as well works only for as
long as the two share a certificate authority, and breaks the day the API moves
behind a proxy with a public certificate — with a certificate error and no
obvious cause.

**Today the platform serves its own certificate**, so pin the API with
`GSU_API_CA_FILE`. **When the proxy lands**, comment that line out and the
system bundle takes over. That is the whole migration.

Neither setting can disable verification, and neither will fall back to
plaintext. There is no third option and there is deliberately no flag for one.

**`GSU_BROKER_URL` must be an address and nothing else.** Not a username, not a
password. redis-py lets a URL override the credentials passed alongside it, so a
URL carrying `user:pass@` would replace this station's identity with whatever it
names — failing confusingly at best and, at worst, publishing as somebody else.
The agent strips and warns rather than obeying, but do not rely on that.

### Video needs nothing set, and here is what you can set anyway

**The normal case is three variables you have already set.** Snapshots go to
`gsu/{station_id}/video` on the broker you configured, and the live stream goes
to `wss://<the platform's host>/media/ingest`, derived from `GSU_PLATFORM_URL`
and authenticated with the credential the station already holds. There is no
fourth URL to obtain and no second secret.

| | When you need it |
|---|---|
| `GSU_MEDIA_URL` | Only when the media endpoint is **not** on the API's host — the same situation `GSU_BROKER_URL` exists for. `wss://…/media/ingest` |
| `GSU_ENCODER` | `auto` (default), `hardware` or `software`. `auto` probes for a hardware encode block and prefers it; naming one that is not there is **refused with the reason**, not silently swapped, because a quiet fallback hides the fact you set the option to establish. A Pi 5 is believed to need `software` — HARDWARE.md §9 |
| `GSU_STREAM_SINK` | Diagnostics only: writes the live stream to a file instead of to the platform. `python -m gsu stream` sets it for you |

None of these are in the shipped `gsu.env`, deliberately: a variable in an
environment file is a variable somebody will set.

**Bandwidth is the thing to decide here, not connectivity.** Snapshots run
continuously; the live stream runs only while somebody is watching. At the
defaults — 640×480 at 2 fps — snapshots cost around 245 kbit/s of test card, or
**640–1280 kbit/s from a real camera** (HARDWARE.md §8). If the site's link is
metered and small, turn the rate down from the platform with `config.set`
(`video_fps`, or `video_enabled: false`) rather than editing anything on the box.
The station reports what it is actually costing in every health frame, so this
is a number you can check rather than estimate.

---

## 5. Preflight

Before starting anything:

```bash
cd /opt/percepta/station
sudo docker compose -f deploy/docker-compose.yml run --rm gsu preflight --probe
```

`--probe` opens a TLS connection to the platform and the broker and verifies
their certificates. It sends no token and no credential, so it is safe to run
before enrolling.

Every line is `PASS`, `WARN` or `FAIL`. **A `FAIL` is something that will not
work.** Expected on a first run, before enrolment:

```
Clock
  PASS  plausible
  PASS  disciplined by ntp
  WARN  no hardware RTC
Trust
  WARN  broker: no CA pinned yet   (arrives in the enrolment response)
  PASS  platform API: CA pinned    SHA-256 50:03:05:1A:...
  PASS  platform API: https://…    verified …, certificate CN=…
  WARN  broker: no address yet     not enrolled
Identity
  WARN  not enrolled
Serial ports present
  PASS  /dev/serial/by-id/usb-FTDI_…
Devices
  FAIL  adsb: uAvionix ping RX Pro …: no serial port set for this device
  FAIL  radio: RTL-SDR airband …: not supported by this software build
  PASS  camera: Raspberry Pi camera (CSI ribbon)
          Pi CSI camera via picamera2, 640x480, quality 75
```

Once the proxy is in front of the API, the second line reads `platform API:
system CA bundle` instead, and that is also a PASS.

The two FAILs are dealt with in §8 and §10. Anything under **Trust** that fails
must be fixed here, not later.

**Read the camera line carefully — preflight does not take a picture.** It
reports that the driver could be built: that libcamera tooling is installed and
this board can be asked for a frame. A ribbon that is not seated, or a camera
that is not there, still shows `PASS` here and only settles at the first
capture, after which the slot reports `configured_absent` with libcamera's own
message. **`gsu camera` is what proves a picture** (§9), and it takes about a
second. On a box with neither `picamera2` nor `rpicam-jpeg` installed the line
is a `FAIL` naming both.

---

## 6. Start it

```bash
cd /opt/percepta/station
sudo docker compose -f deploy/docker-compose.yml up -d
sudo docker compose -f deploy/docker-compose.yml logs -f
```

It starts whether or not there is a network, whether or not it is enrolled, and
whether or not any sensor answers — **and whether or not the sensors are
plugged in**. That last one is why the device mapping is permissive: with named
device entries, one absent UART would stop the container, and stopping is
exactly what a remote station must not do.

---

## 7. Enrol

The setup page binds to loopback, so from your laptop:

```bash
ssh -L 8088:127.0.0.1:8088 pi@<box>
```

then open <http://127.0.0.1:8088>, type the code into the one field on the page,
and press the button. Watch for:

- **Enrolled** → yes
- **Link to the platform** → up
- **Broker security** → `TLS, CA pinned 50:03:05:1A:…`
- **Platform API security** → `TLS, public certificate`, or `TLS, CA pinned …`
  if you pinned it

Headless alternative, over SSH:

```bash
cd /opt/percepta/station
sudo docker compose -f deploy/docker-compose.yml run --rm gsu enrol --token XXXX-XXXX-XXXX
```

That writes the credential into the shared state directory, and **the running
container notices it on disk within a few seconds** and attaches without a
restart — the state directory is a bind mount, so the one-shot container and
the long-running one are looking at the same files.

**If the code is refused**, the message is deliberately the same for unknown,
expired and already-used: *"This code is not valid. Ask for a new one."* Asking
for another is cheap and audited.

**Enrolment is resumable.** If the link drops halfway, run it again with the
same code. The platform re-issues rather than refusing (`contract/enrolment.md`
§11) — the failure that matters is a technician stranded with a used code.

Where the console *should* live is still an open decision (`DECISIONS.md`, open
decision 2). It has no authentication: an SSH tunnel is the interim control, and
that is a decision to confirm, not a design.

---

## 8. Point the drivers at the right serial ports

**Do this after the box has booted with both adapters plugged in**, so the names
are real.

```bash
ls -l /dev/serial/by-id/
```

Set each device's port on the setup page — the field offers what is plugged in —
or with the values from that listing. **Use the `by-id` names.** `/dev/ttyUSB0`
is a trap here: two adapters enumerate in whichever order the kernel probed
them, that order changes between boots, and when they swap the weather driver
reads the ADS-B stream and vice versa. It presents as both instruments failing
at once, which sends you looking for a power fault.

Which is which: unplug one, re-run `ls`, and see which name disappeared. The
Airmar is 4800 baud, the ping RX Pro 57600.

**The camera is not in this step.** It is a ribbon cable on the CSI connector,
with no port to choose and no credentials to type — that distinction is why the
device registry keeps `csi` and `network` connections apart, and why the setup
page never asks for the password of a camera that is a ribbon. Its settings are
resolution, JPEG quality and rotation, and the defaults are the ones to leave
alone unless §4's bandwidth arithmetic says otherwise.

Then:

```bash
cd /opt/percepta/station
sudo docker compose -f deploy/docker-compose.yml run --rm gsu devices
```

`present` means the driver is constructed **and the device is talking**.
`configured, gone quiet` means it answered once and stopped — a different fault
from `configured, not detected`, and worth different action.

---

## 9. Confirm it is working

**On the box**

```bash
cd /opt/percepta/station
sudo docker compose -f deploy/docker-compose.yml logs --tail 50
```

Look for, in order:

```
Broker TLS trust: pinned CA from enrolment, SHA-256 …
Platform API TLS trust: system CA bundle (public certificate, not pinned)
Station <name> (<uuid>) attached: publishing to gsu/<uuid>/telemetry …
Subscribed to cmd/gsu/<uuid> as gsu:<uuid>.
```

Then `sudo docker ps` should show `percepta-gsu` as `Up`, not `Restarting`. A
container cycling through `Restarting` is a crash loop — `logs --tail 100` says
why, and §14 covers what the updater does about one it caused.

**On the setup page** — the rows that matter: Enrolled *yes*, Link *up*, Broker
security *TLS, CA pinned*, Platform API security *TLS*, Clock kept by *NTP*,
Telemetry sent *increasing*, Dropped *not increasing*.

**From the platform side** the station goes online on its own: the ingest writes
`last_seen_at` from the telemetry itself, and there is no separate heartbeat.

**Conformance**, run from a machine that can reach the broker:

```bash
PERCEPTA_BROKER_URL="rediss://<host>:6380/0" \
PERCEPTA_CA_FILE=/path/to/ca.crt \
python contract/conformance/check_station.py --station <uuid>
```

Streams with no driver are reported as declared-unavailable and skipped, not
failed. A station is not failed for lacking hardware, only for pretending.

### Confirming video, which conformance does not cover

Conformance checks telemetry, audio and commands. Video is a separate channel
and a separate uplink, and it has its own three checks. Do them in this order —
each one needs less of the world than the one after it.

**1. Does the camera produce a picture at all?** No platform, no network, no
enrolment:

```bash
cd /opt/percepta/station
sudo docker compose -f deploy/docker-compose.yml run --rm gsu camera --frames 3 --out /tmp/frame.jpg
```

```
armv7l / Pi CSI camera via picamera2, 640x480, quality 75

  1:   38.4 kB JPEG,   51.4 kB published,   184.2 ms, captured 2026-07-29T…Z
  2:   38.1 kB JPEG,   51.0 kB published,   171.5 ms, captured …
  3:   38.6 kB JPEG,   51.7 kB published,   169.8 ms, captured …

  51.4 kB per published frame; at 2 fps that is 842 kbit/s sustained on the uplink.
```

That last line is the one to read before you leave: it is measured from this
camera at this setting, not estimated. Copy `/tmp/frame.jpg` off and look at it —
a picture of the site means the ribbon, the sensor and the encoder are all
right. **No frame** prints libcamera's own message, which is the diagnosis:
*"no cameras available"* is a ribbon or a camera, *"picamera2 is not installed
and no rpicam-jpeg was found"* is a package.

**2. Does the live encoder keep up?** Still no platform needed:

```bash
sudo docker compose -f deploy/docker-compose.yml run --rm gsu stream --seconds 30
```

It prints which encoders this board has, then measured frames per second,
measured bitrate and dropped frames, and writes fragmented MP4 to
`/dev/shm/gsu-stream.mp4`. Play that back — it is the same container the platform
receives, so it checks the container as well as the camera. It says plainly when
the encoder did not keep up, and **that is a hardware answer to report, not a
setting to nudge quietly**.

**3. Is it reaching the platform?** With the station running and enrolled:

```bash
curl -s http://127.0.0.1:8088/status.json | python3 -m json.tool | sed -n '/"video"/,/^        }/p'
```

or the same block in any `health` frame the platform receives. What each field
means when things are right:

| Field | Right |
|---|---|
| `video.enabled` | `true` |
| `video.frames_published` | increasing |
| `video.frames_dropped` | not increasing |
| `video.bytes_per_frame`, `bitrate_bps` | measured, and roughly what §4 predicted |
| `video.refused` | `false`. `true` means the broker will not accept the video channel — an ACL on the platform, not a fault here |
| `video.reason` | empty. When it is not, it is a sentence, and it is the diagnosis |
| `video.stream.state` | `idle` until somebody watches. That is correct: the stream is on demand |
| `video.stream.uplink` | `websocket:wss://…/media/ingest`. If it says `none (no media URL configured)` the station is encoding into a counter and nobody can see anything |

**On the setup page**, the same numbers in the video row: frames published
increasing, dropped not, and the bitrate. **On the platform's console**, the
station's video panel shows the picture — with the simulated camera fitted it
shows a test card that says SYNTHETIC with the capture time drawn on it, which
is the point of it: if that clock is stale, the age an operator is being shown is
wrong, and it is visible by eye with nothing to set up.

**Then ask for the live stream**, which only the platform can do — a viewer
attaching is what starts it. Within a second or two the log says:

```
Media uplink open to wss://…/media/ingest
Streaming 1920x1080 at 30 fps, 3000 kbit/s target, to websocket:wss://…
Media session started: avc1.640028, 651 byte initialisation segment.
```

and `video.stream.state` becomes `streaming` with `fps_measured`, `bitrate_bps`,
`dropped` and `resyncs` beside it. When the viewer leaves, the platform stops
renewing the lease and the station stops on its own within about thirty seconds —
**silence is the stop signal**, so a console that crashes or a link that drops
does not leave the encoder running on a metered link.

---

## 10. What this build does not do

One of the four fitted devices has **no driver in this build**, and says so:

| Device | What the station does |
|---|---|
| RTL-SDR airband | publishes `radio` as `available: false`, reason *"not supported by this software build"*, on the normal cadence |

It is selected in the inventory anyway, so that the platform and the console can
see the hardware is fitted and that what is missing is software. Nothing is
stubbed to look like it works.

`power` and `light` have no hardware specified for this site, so those slots are
empty and their streams are declared unavailable for that reason instead.

### The camera is driven now, and none of it has met a camera

This row used to say the camera "reports as configured and undriveable" and that
there was no media channel to carry anything it produced. **Both are now false.**
What exists:

| | |
|---|---|
| Snapshots | `gsu/{station_id}/video`, one complete JPEG per message, 640×480 at 2 fps by default. `contract/schemas/video.schema.json` |
| Live video | H.264, fragmented MP4 over a WebSocket to the platform, started only while somebody is watching |
| Driver | `gsu/camera/picsi.py` for stills — `picamera2` if it imports, `rpicam-jpeg` if it does not. `gsu/camera/h264.py` for the stream — a hardware encode block, or libav/x264, probed at start-up |
| No camera fitted | `available: false` with a reason, on a cadence. Never silence, never a black frame |

**What has never happened**, and the owner is about to be the first to find out:

- **No part of this has run against a real camera.** Not `picamera2`, not
  `rpicam-jpeg`, not `rpicam-vid`, not the hardware encoder. There is no Pi and
  no camera on the machine this was written on.
- **Nothing has executed on ARMv7 at all**, so the timings in HARDWARE.md §8 are
  x86 numbers and are labelled as such.
- The **synthetic** camera and the synthetic H.264 source have been run
  continuously and verified — the JPEG against libjpeg, the H.264 and the
  fragmented MP4 against ffmpeg, and the whole uplink end to end against the
  running platform at 1080p30 for fifteen seconds with nothing dropped. That
  proves the *pipe*. It says nothing about the sensor.

So the first two commands in §9 are the ones that matter on the day: `gsu camera`
proves the picture, `gsu stream` proves the encoder. Both work with no platform
and no network, and both print numbers rather than opinions.

One thing that follows from a single camera: **the live stream takes the sensor
while it runs**, so snapshots pause and publish `available: false` with
*"the camera is in use by the live stream"* until it stops. That is deliberate —
a `rpicam-jpeg` competing with `rpicam-vid` for the same device fails with a
device-busy that reads like broken hardware — and it is what a console will show.

---

## 11. Time, and the GPS receiver

**This box has no battery-backed clock.** On every boot its time is whatever the
filesystem suggested until NTP answers. `contract/enrolment.md` §6 is blunt
about the cost: a station with a wrong clock cannot authenticate, and if it
believes its credential has expired it cannot renew either. That is a site
visit for a bad number.

What the station already does: refuses to enrol with an implausible clock,
raises `clock.implausible` as a critical condition, and reports what is
disciplining its clock in every health frame and on the setup page.

**Fit an RTC.** A DS3231 module is a few pounds and removes the class of failure
— see HARDWARE.md §4 for the wiring and the overlay.

**When the GPS receiver arrives**, it goes into chrony, not into this software:

```
# /etc/chrony/chrony.conf
refclock SHM 0 refid GPS precision 1e-1 offset 0.128 delay 0.2
refclock PPS /dev/pps0 refid PPS lock GPS precision 1e-7
```

with `gpsd` feeding SHM. Then:

```bash
sudo apt install gpsd gpsd-clients pps-tools
sudo systemctl restart chrony
chronyc sources          # PPS should reach '*' — the selected source
```

No station code changes. `gsu preflight` will start reporting `disciplined by
gps` on its own, because chrony's reference id becomes `PPS`. The reasoning for
keeping GPS timing out of this process is in `gsu/clock.py` — briefly, PPS is a
kernel device and only a disciplining daemon can use it properly, and a Python
loop calling `settimeofday` from NMEA sentences would be a worse clock than the
NTP it replaced.

---

## 12. Reading the logs

```bash
cd /opt/percepta/station
C="docker compose -f deploy/docker-compose.yml"

sudo $C logs -f                          # follow the agent
sudo $C logs --since 1h                  # the last hour
sudo $C logs --tail 200 | grep -i warn   # just the problems

journalctl -u gsu-update -n 50           # what the updater last decided
sudo /opt/percepta/station/deploy/gsu-update.sh --status
```

Container logs are rotated at 10 MB × 3 by the compose file. Docker's default
is **no rotation at all**, which on a station that logs for months fills the SD
card and takes the site down — a slow failure with an annoying cause, and the
one thing the container path needs configured that the systemd path gets free
from journald.

The lines that matter are health conditions. Each is raised once with a
severity, kept while it persists — the age of a problem is the useful number —
and cleared when it goes away.

| Condition | Means |
|---|---|
| `uplink.refused` | The station will not connect on these terms and is publishing nothing. A plaintext URL on a pinned box, or no CA. Not a network fault |
| `uplink.tls_failed` | The broker's certificate did not verify against the pinned CA |
| `tls.api_trust_unusable` | `GSU_API_CA_FILE` is set to something unreadable. It does **not** fall back to the system bundle — you asked for pinning |
| `tls.broker_trust_unusable` | `GSU_CA_FILE` is set to something unreadable |
| `uplink.down` | No route to the broker. Weather, an obstruction, a dead link |
| `credential.renewal_failing` | Renewal is failing. Warning, escalating to critical inside six hours of expiry |
| `credential.revoked` | The platform no longer accepts this station. Needs a new code |
| `clock.implausible` | The clock is not credible. Enrolment is refused until it is |
| `clock.unsynchronised` | Nothing is disciplining the clock |
| `devices.absent` | Something configured is not answering |
| `telemetry.unsourced` | Streams with no source at all. Expected on this build for `radio` |
| `video.topic_refused` | The broker will not accept `gsu/{station_id}/video`. An ACL on the platform, not a fault here — the station retries every five minutes and nothing else is affected |

The same list is on the setup page under **Needs attention**, which works with
no link at all.

---

## 13. When something is wrong

**The container will not start.**
`sudo docker compose -f deploy/docker-compose.yml logs --tail 50`. Most likely
`/etc/percepta/gsu.env` has a syntax error (it is shell-ish: `KEY=value`, no
spaces around `=`), or the image is missing because the build failed — re-run
`docker compose ... build`.

If it fails with a *device* error, that should no longer be possible: the
compose file bind-mounts `/dev` wholesale rather than naming nodes, precisely so
that a missing sensor cannot stop the station. If somebody has reintroduced a
`devices:` list, that is the cause and `DECISIONS.md` item 35c is why it was
removed.

**The container is in a restart loop.**
`sudo docker ps` shows `Restarting`. If it started after an update window, the
updater should already have rolled it back — check `sudo
/opt/percepta/station/deploy/gsu-update.sh --status` and `journalctl -u
gsu-update -n 50`. If it did not (the timer was disabled, or the update was
applied by hand), roll back with `sudo ... gsu-update.sh --rollback`.

**A sensor is not visible inside the container.**
Check it exists on the host first (`ls -l /dev/serial/by-id/`). If it is there
and the container cannot open it, the cause is the device cgroup rather than the
mount: `device_cgroup_rules` in the compose file needs the major for that device
class. `ls -l /dev/<node>` prints the major as the first of the two numbers.

**"Another agent is already running."**
Two copies. Almost always both deployment paths at once — check `systemctl
is-enabled gsu` and stop it: the container path leaves that unit installed but
disabled deliberately. Two agents on one station publish two independent worlds
onto one channel and the console alternates between them.

**Nothing is being published, and the link looks fine.**
Check **Broker security** on the setup page. `uplink.refused` means the station
decided not to connect — which is a configuration fault, not a network one. The
message names the fix.

**The certificate does not verify — first work out which link.** They have
different roots and different fixes, and the message says which one it is.

*The broker.* Compare fingerprints; the station prints the one it has pinned:

```bash
sudo -u gsu ... python -m gsu preflight        # prints both pinned fingerprints
openssl s_client -connect <host>:6380 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -fingerprint -sha256
```

If the broker's CA has been rotated, re-enrol: the new CA arrives in the
enrolment response and is pinned from then on.

*The platform API.* Almost always one of two things. Either the platform still
serves its own certificate and `GSU_API_CA_FILE` is not set — set it. Or a proxy
with a public certificate has landed and `GSU_API_CA_FILE` is *still* set to the
old private CA — comment it out and let the system bundle verify.

**There is no setting that skips verification, and adding one would be the wrong
fix.** A station that accepts any certificate hides exactly the fault you are
looking at, and hides it everywhere, for ever.

A CA that `redis-cli --cacert` accepts may still be rejected by Python, which
requires `basicConstraints` and `keyUsage` on a CA certificate. If `redis-cli`
works and the station does not, that is the first thing to check on the
platform side.

**A device is configured and not detected.**
The reason is in `gsu devices`, on the setup page, and in the `unavailable_reason`
the platform receives. For serial devices it names the ports that *are* present.
Check the user is in `dialout` (`id gsu`) and that you used a `by-id` path.

**There is no picture, and everything else is fine.**
Read `video.reason` in `status.json` (§9) — it is a sentence, and it is the
answer. The four you will actually see:

| Reason | What it is |
|---|---|
| *"no camera fitted"* | the camera slot is empty in the inventory. Set it on the setup page |
| libcamera's own words — *"no cameras available"* | the ribbon or the camera. `gsu camera` reproduces it in a second, and the message comes from libcamera rather than from this software |
| *"picamera2 is not installed and no rpicam-jpeg was found"* | packages: `sudo apt install python3-picamera2 rpicam-apps` |
| *"the camera is in use by the live stream"* | not a fault. One sensor, one user; snapshots resume when the viewer leaves |

If `video.refused` is `true`, the broker is rejecting the video channel. That is
an ACL on the platform, not a fault on the box — the station retries every five
minutes and needs nobody on site once it is fixed.

If the picture is there but the *stream* never starts, the station is waiting to
be asked: `video.stream.state` stays `idle` until a viewer attaches, and that is
correct. Check `video.stream.uplink` — `none (no media URL configured)` means it
would encode into a counter, and `GSU_PLATFORM_URL` is what it derives from.

**The stream starts and the picture stutters or goes blocky.**
`video.stream.dropped` and `resyncs` in the health frame. Rising `resyncs` is a
link that cannot carry the bitrate: the station drops whole fragments and waits
for the next keyframe rather than queueing, so what an operator sees is a gap
rather than a smear. Lower `stream_bitrate_kbps` or `stream_fps` with
`config.set` from the platform — not on the box, and not by restarting anything.

**It enrolled and now the credential is refused.**
`credential.revoked` means an admin revoked it, or another box claimed this
station's enrolment — a re-claim cuts off the box that had already succeeded
(`contract/enrolment.md` §11). Get a new code and re-enrol. The station keeps
recording throughout; it is cut off, not disabled.

**Start again from nothing.**

```bash
cd /opt/percepta/station
C="docker compose -f deploy/docker-compose.yml"
sudo $C down
sudo rm /var/lib/percepta-gsu/credential.json /var/lib/percepta-gsu/broker-ca.pem
sudo $C up -d
```

Keeps the device inventory, the events and the recordings; drops the identity.
Then enrol with a fresh code.

---

## 14. Updates, and what happens when one is bad

**This is the section that justifies the container.** Everything else is
roughly a wash between the two paths; this is not.

The failure being designed against is not "the update did not arrive". It is
**"the update arrived, the station stopped working, and now somebody has to
drive there"** — which on these sites is the most expensive thing that can
happen, and an update is the routine operation most likely to cause it.

### How a station is told

It is not told. **Nothing can reach inward** — Starlink is CGNAT and the
platform can never initiate a connection to a station
(`contract/enrolment.md` §1) — so the station asks, on a timer:

```
gsu-update.timer  →  gsu-update.service  →  deploy/gsu-update.sh
```

Every 6 hours, **plus up to 2 hours of random delay**. The jitter is the
important part: without it every station in a fleet checks in the same minute,
so a bad image reaches all of them at once and they fail together. Spread out,
the first station fails long before the last one has looked — the difference
between one site visit and all of them.

A check that finds nothing new costs **about 6 KB** (an auth token plus a
manifest HEAD). Four a day is ~24 KB against roughly 113 MB/day of telemetry
from this station: 0.02%. That measurement is why polling is reasonable here at
all, and it is in `DECISIONS.md` item 40 with how it was taken.

Set what to track in `/etc/percepta/gsu.env`. **Prefer a digest** — a tag can be
moved under you, and on a box you cannot reach you want to know exactly what
will arrive:

```sh
GSU_UPDATE_REF=registry.example.net/percepta/gsu@sha256:abc123…
```

Unset, the station never updates. That is a safe default, not an oversight.

### What happens when one arrives

```
pull  →  tag the running image as `previous`  →  start the new one
      →  GATE  →  keep it, or put `previous` back
```

**The gate is the whole design.** Within `GSU_GATE_SECONDS` (180 by default) the
new container must:

1. be running — not restarting, not exited;
2. answer on its own console;
3. report itself **enrolled** — the credential survived the swap;
4. report the **uplink up**;
5. **increase its published-frame counter.**

Point 5 is the one that earns its keep. A container can start, log cheerfully
and publish nothing at all — and that failure is invisible to a "did it start?"
check and indistinguishable from a healthy station until somebody looks at a
console days later. The gate insists the station is doing its job, not that it
launched.

If the gate fails, the updater retags `previous` back to `current`, recreates
the container, **and gates the restored image too** — so "the rollback worked"
is a fact rather than an assumption. If even the old image cannot pass, it says
so specifically: that is not an update fault and sending someone after the
update would waste the trip.

The rejected digest is recorded, and **not retried**. Without that, a bad image
is re-pulled every 6 hours for ever, spending metered bandwidth and flapping the
station in and out of service each time.

### If the link drops mid-pull

Nothing happens, which is the point. `docker pull` is atomic at the image level:
layers are content-addressed and verified as they arrive, and the local
reference only moves once every layer is present. **A half-completed pull cannot
produce a half-built image** — it fails, the running container is untouched, and
the next check resumes from the layers already in the content store. The
updater treats a failed pull as a non-event and exits 0.

*(Documented Docker behaviour. Not verified on this hardware — see "what was
never tested" below.)*

### Doing it by hand

```bash
U=/opt/percepta/station/deploy/gsu-update.sh

sudo $U --status      # what is running, what the rollback target is
sudo $U               # check and apply now, with the gate
sudo $U --rollback    # go back to `previous` deliberately
sudo $U --force       # retry a digest that previously failed its gate
```

`--status` is the first thing to run when a station is behaving oddly after an
update window.

### Disk

Two images, sharing every layer they have in common — the base and the pip layer
are identical between builds, so keeping `previous` costs only its own code
layer, about 91 KB. Keeping a rollback target on disk is close to free, which is
what makes this design possible on an SD card.

### Building and publishing an update

```bash
# on a build machine
docker buildx build --platform linux/arm/v7 \
  -f deploy/Dockerfile -t registry.example.net/percepta/gsu:0.1.1 .
docker push registry.example.net/percepta/gsu:0.1.1
docker inspect --format '{{index .RepoDigests 0}}' registry.example.net/percepta/gsu:0.1.1
```

Put that digest in `GSU_UPDATE_REF` on **one** station first, watch it, then the
rest. The jitter means a fleet staggers itself, but deliberately staging one
box is better than relying on luck.

### What this does not do

- **No signature verification.** A digest pin means the image cannot change
  under you, but it does not prove who built it. Proper signing is part of the
  §9.5 answer that is still open — see `DECISIONS.md` item 39.
- **No way to trigger an update from the platform.** The station checks when its
  timer says so, up to ~8 hours after a release. A command-channel trigger would
  need a contract change; it is written up as `CONTRACT-QUESTIONS.md` item 11
  rather than invented here.
- **No host OS updates.** This updates the agent, not Raspberry Pi OS.

---

## 15. What is exposed

| Port | Bound to | What |
|---|---|---|
| 8088 | `127.0.0.1` | the setup console. **No authentication** — reach it over an SSH tunnel |
| 22 | everything | SSH. Key-only, please: this box is on the public internet |

The station makes **outbound** connections only: to the broker (6380/TLS), to
the platform API (8000/TLS), and — **only while somebody is watching** — a
WebSocket to the platform's media endpoint for the live video. Nothing reaches
inward: Starlink is CGNAT and the platform can never initiate a connection to a
station. That is also why a viewer asks for video through the *command* channel
rather than by connecting to the box.

The media socket carries the same credential as the broker, verified against the
same pinned trust, and is closed when the stream stops. There is no third secret
and no port to open.

If the setup console is ever moved off loopback, it belongs on a private setup
network and nowhere else — it has no authentication, and physical presence is
the only control it has (`DECISIONS.md`, open decision 2).

---

## 16. Backups

**Scheduled on the platform, not here.** Nothing in this runbook backs anything
up and nothing should be read as implying otherwise.

What lives only on the station, and what it costs to lose:

| | If the SD card dies |
|---|---|
| Credential and pinned broker CA | re-enrol with a new code. Minutes |
| Device inventory | re-enter the serial ports on the setup page. Minutes |
| Event database | **lost.** Proximity alerts and outage records for the retention window |
| Audio recordings | **lost.** Up to 24 h / 200 MB |
| Video | **nothing to lose.** No frame is stored on the box: snapshots are published and dropped, and the live stream is never written to disk except by `gsu stream`, which is a diagnostic and is capped |

Neither of the last two has a channel to the platform yet — that is
`CONTRACT-QUESTIONS.md` item 4, still open — so a card failure loses them. That
is an argument for the event channel, not for backing up an SD card in the
field.

---

## Appendix A: everything in one place

```bash
# on the box, as root
/opt/percepta/station                    code and deploy/ (root-owned)
/etc/percepta/gsu.env                    configuration        0640 root:gsu
/etc/percepta/platform-api-ca.pem        the API's CA, if pinned
/var/lib/percepta-gsu/                   state                0700 gsu
  credential.json                        the station's identity   0600
  broker-ca.pem                          the pinned broker CA     0600
  devices.json                           what is fitted
  station.db                             events
  recordings/                            audio
  update/rejected                        digests that failed their gate
/etc/systemd/system/gsu-update.timer     the update check (container path)
/etc/systemd/system/gsu.service          the systemd path (disabled by default)

# images
percepta/gsu:current                     what runs. The updater moves this tag
percepta/gsu:previous                    the rollback target, already on disk

# everything, via compose
C="docker compose -f /opt/percepta/station/deploy/docker-compose.yml"
sudo $C up -d                            start
sudo $C logs -f                          follow
sudo $C run --rm gsu preflight --probe   everything that must be true
sudo $C run --rm gsu devices             intent against fact
sudo $C run --rm gsu whoami              what this box thinks it is, offline
sudo $C run --rm gsu status              what the platform thinks of it
sudo $C run --rm gsu bench               what a tick costs on this hardware
sudo $C run --rm gsu camera --frames 3   one picture, and what it costs to send
sudo $C run --rm gsu stream --seconds 30 the live encoder, measured and playable

# updates
U=/opt/percepta/station/deploy/gsu-update.sh
sudo $U --status                         running / previous / tracking
sudo $U                                  check and apply, gated
sudo $U --rollback                       go back deliberately
journalctl -u gsu-update -n 50           what it last decided
```

---

## Appendix B: running it as a plain systemd service instead

Fully supported, and the right choice if you would rather not have Docker on the
box. What you give up is §14 — atomic updates and a rollback that is already on
disk — which is the reason it is not the default, not a defect in this path.

Everything from §4 (configuration) onwards is identical. The differences:

```bash
sudo apt install -y python3-venv chrony          # instead of docker
sudo /tmp/station/deploy/install.sh --path systemd --api-ca /tmp/platform-api-ca.pem

# preflight, start, logs
cd /opt/percepta/station
sudo -u gsu env $(grep -v '^#' /etc/percepta/gsu.env | xargs) \
  .venv/bin/python -m gsu preflight --probe
sudo systemctl start gsu
journalctl -u gsu -f

# enrol headless
sudo -u gsu env $(grep -v '^#' /etc/percepta/gsu.env | xargs) \
  .venv/bin/python -m gsu enrol --token XXXX-XXXX-XXXX
```

`--path systemd` disables the update timer, because it updates a container and
there is not one.

**Upgrading is manual**, and this is the trade:

```bash
rsync -a --exclude var/ --exclude .venv/ ~/percepta/station/ pi@<box>:/tmp/station/
sudo /tmp/station/deploy/install.sh --path systemd
sudo systemctl restart gsu
```

Configuration, state and the device inventory survive. There is **no health gate
and no automatic rollback** — if the new code does not work, somebody has to
notice and put the old code back by hand, over a link that may be why they
noticed. On a site that is hours away, that is the risk §14 exists to remove.

### What this path is better at

Two things, honestly:

- **A missing device was never a problem here.** The unit has no device list to
  be wrong; the agent opens what it finds and reports what it cannot. The
  container path only matches this because its device mapping was made
  permissive (`DECISIONS.md` item 35c).
- **Tighter sandboxing.** `ProtectSystem=strict`, an empty
  `CapabilityBoundingSet`, a `@system-service` syscall filter and a
  `DeviceAllow` list, against a container that has the host's whole `/dev`. That
  difference does not matter on a box where nothing else runs — which is the
  owner's position and the reason the trade was made — but it is real and it
  should be stated rather than glossed.

### The hardening in the unit, and two lines that could bite

- **`MemoryDenyWriteExecute=yes`** — CPython and the one pure-Python dependency
  do not need writable-executable pages. If a native extension is ever added and
  the service will not start, this is the first line to remove, and the reason
  should be written down when it is.
- **`RestrictAddressFamilies` includes `AF_NETLINK`**, which looks removable and
  is not: glibc's `getaddrinfo()` uses it to enumerate local addresses, so
  dropping it breaks DNS and the station cannot find the broker.

`PrivateDevices` is deliberately not set — it would take away the UARTs and the
SDR. A `DeviceAllow` list is used instead.

---

## Appendix C: what has actually been run, and what has not

The distinction matters more than usual here, because the deployment path
changed twice and none of it has touched the target hardware.

**Verified, on x86-64:**

- TLS to the broker with a pinned private CA, refusal of the wrong CA, refusal
  of plaintext, and refusal to downgrade — against a real TLS-only Redis with a
  per-station ACL.
- Enrolment over HTTPS, CA persistence at 0600, reconnection from the persisted
  CA alone, and the broker/API trust split in both configurations.
- Full contract conformance over `rediss://` with no `--insecure`.
- **The updater's decision logic** — accept, gate-fail, roll back, verify the
  rollback, refuse to retry a rejected digest, survive a failed pull, no-op on
  an unchanged digest — driven against a stubbed Docker and a real HTTP console.
  21 scenarios.
- The ARMv7 base image, verified at the registry.
- Update bandwidth: the code layer and the cost of a no-op check, both measured.
- **Video, end to end against the running platform**: snapshots on
  `gsu/{station_id}/video`, and the live stream as fragmented MP4 over a
  WebSocket — 1080p30 for fifteen seconds, 459 fragments, none dropped, and it
  played in a browser. The synthetic JPEG was checked against libjpeg and the
  synthetic H.264 and fMP4 against ffmpeg, including a late joiner given only
  the initialisation segment and a later keyframe.
- The on-demand lease: a repeated `video.start` renews rather than restarting,
  and a lease left to expire stops the encoder on its own.
- Congestion: against a server that accepts the connection and then stops
  reading, fragments are dropped whole and the picture resynchronises on the
  next keyframe. Nothing is ever queued and no partial frame is ever written.

**Never run, anywhere:**

- The image has **never been built**, on any architecture. There is no Docker
  daemon on the machine this was written on.
- The container has **never been started**, so the `/dev` bind mount, the
  `device_cgroup_rules` majors, `group_add`, `read_only` and the tmpfs are all
  reasoned from documentation.
- `gsu-update.sh` has **never driven a real container** — only the stub.
- `install.sh` has never been run end to end.
- Nothing has executed on ARMv7, a real UART, the camera or the SDR.
- **No camera code has met a camera.** `picamera2`, `rpicam-jpeg`, `rpicam-vid`
  and the hardware H.264 encoder are all reasoned from documentation. Everything
  verified above ran against the synthetic camera and the synthetic encoder,
  which exercise the publisher, the muxer, the uplink and the on-demand logic —
  and none of the sensor.

The first person with the hardware should expect the device cgroup majors and
the `group_add` gids to need adjusting, should run `gsu-update.sh` by hand once
before letting the timer near it, and should run `gsu camera` and `gsu stream`
(§9) before trusting anything this document says about video.
