# Deploying a ground station

## The short way

```bash
# The platform's CA, copied from the platform host. Nothing can fetch this for
# you: the platform sends only its leaf certificate, so the CA is not on the
# wire, and one pulled from the thing it authenticates would not be worth
# pinning. Check the fingerprint against the platform host before trusting it.
scp <you>@192.168.2.49:~/percepta/server/certs/ca.crt /tmp/platform-api-ca.pem

git clone <repo> ~/percepta && cd ~/percepta
sudo station/deploy/bootstrap.sh --platform 192.168.2.49 --ca /tmp/platform-api-ca.pem
```

Run it from anywhere — it finds the checkout from its own path, not from the
working directory.

That is the whole thing on a box with no CSI camera. It checks the hardware and
the OS, installs what the chosen path needs, blacklists the DVB driver if it
sees an SDR, runs the installer, writes the configuration, generates and prints
a setup password, publishes the setup page on the LAN, runs preflight and starts
the station. It prints the URL to enrol from. It is idempotent — run it again to
change your mind about a flag.

`--demo` provisions a box whose every slot is simulated. `--loopback` keeps the
setup page off the LAN. `--help` lists the rest.

**A station with a CSI camera takes the systemd path**, and bootstrap detects
one and switches by itself. §3 has the measurements behind that.

The rest of this document is what bootstrap does and why, which is what you
want when one of the steps does not work — or when you are doing it by hand,
which remains entirely supported.


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
| Pi camera | CSI ribbon. Driven and **proven on real hardware**: snapshots on the video channel, and live 1080p30 H.264 on demand through the hardware encoder — see §10 |
| Network | Ethernet or a Starlink terminal. The Pi 2B has no onboard Wi-Fi |

**Software**

Raspberry Pi OS **Bookworm** (12) or **Trixie** (13). The agent needs Python
3.11 or later because it uses `datetime.UTC`: Bookworm ships 3.11, Trixie ships
3.13, and both are fine. Bullseye ships 3.9 and **will not run this** — that is
an OS upgrade, not a patch, and the installer refuses rather than half-working.

Two things Trixie changes that are worth knowing before you start:

- **`pip install` into the system Python is refused** (PEP 668,
  `externally-managed-environment`). That is why both paths use a virtual
  environment, and it is not a thing to work around with `--break-system-packages`.
- **The container's Python is older than the host's.** The slim image is
  `python:3.11-slim-bookworm` while a Trixie host runs 3.13. Both are above the
  floor and the agent behaves the same on either, but the two deployment paths no
  longer run the same interpreter — worth remembering when a bug appears on one
  and not the other.

Package names, which changed once and are stable now: **`rpicam-apps`** (the
`libcamera-*` binaries were renamed to `rpicam-*` in Bookworm; both names exist
on current images). On Bullseye they were `libcamera-apps`, which is one more
reason not to be on Bullseye.

**`python3-picamera2` is no longer needed and should not be relied on.** The
station used to prefer it — it was the only way to make a 2 fps snapshot
channel affordable on a Pi 2B — and it was also the only thing that put a
libcamera `CameraManager` inside the agent's own process, where a leaked
acquisition cannot be released for the life of the run. The snapshot channel is
gone (CONTRACT-QUESTIONS.md item 17) and capture is `rpicam-jpeg`, one
subprocess per frame. Installing picamera2 does no harm; the station will not
import it.

One more, only for a **network (RTSP) camera**: **`ffmpeg`**. It is what the
station reads an RTSP camera with — one decoded frame per snapshot, and the
live stream remuxed from the camera's own H.264 without re-encoding
(`gsu/camera/rtsp.py`). The camera container image installs it
unconditionally; on the systemd path `apt install ffmpeg`. Without it the
camera slot reports exactly that sentence rather than a hardware fault. A
CSI-only station does not need it.

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

> **If this station has a camera, use the systemd path (Appendix B).** Measured
> on the first real Pi, not reasoned: the slim image has no camera stack at
> all, and the camera image gets snapshots but its `rpicam-vid` dies with a
> bus error before the first live-stream frame. The host runs the full
> pipeline. §3 has the measurements, and the route to a camera-capable image
> if one is ever worth building. Everything else in this runbook applies to
> both paths.

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
sudo apt install -y docker.io chrony
# Compose v2, whichever name this release uses. `docker-compose-v2` is Trixie's
# and Ubuntu 24.04's; on Bookworm it is in backports, and Docker's own
# repository calls it docker-compose-plugin. Not `docker-compose`, which is v1
# and has no `docker compose` subcommand.
sudo apt install -y docker-compose-v2 \
  || sudo apt install -y docker-compose-plugin \
  || sudo apt install -y -t bookworm-backports docker-compose-v2
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

### Reaching the setup page from another machine

The base compose file binds the setup page to `127.0.0.1`, which is right for a
field station and wrong for a bench. `deploy/docker-compose.lan.yml` is an
overlay that publishes it on every interface, and Compose picks it up from a
`.env` in the project directory:

```bash
echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.lan.yml' \
  | sudo tee /opt/percepta/station/deploy/.env
```

`bootstrap.sh` writes that by default; `--loopback` stops it. Without it, and
without a tunnel, the page answers only on the box itself — and nothing says
so, which is a slow way to find out.

Either way the page still needs `GSU_SETUP_PASSWORD_HASH`. Without one the
agent demotes itself to loopback *inside* the container, and this mapping then
publishes a socket that answers nothing useful.

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
| user `gsu` | system account, **uid 10001** — the same number the image pins, so state and configuration read correctly on either path and flipping paths moves no files. An older install's floating-uid `gsu` is migrated on re-run. No login; in `dialout`, `video`, `plugdev` |
| `/etc/systemd/system/gsu-update.{service,timer}` | the update check, timer enabled |
| `/etc/systemd/system/gsu.service` | the systemd path, installed but **disabled** |
| `/etc/udev/rules.d/99-percepta-sdr.rules` | so the SDR is readable without root |

**Check any CA fingerprint it prints against the one you were told.** This is
the only step in the whole procedure that a person has to verify by eye, and
what follows rests on it.

No network at the site? `pip download -r requirements.txt -d deploy/wheels` on a
machine that has one, copy `deploy/wheels` across, and use `--offline`.

### The camera decides which path this station takes

**A camera-equipped station takes the systemd path.** This used to be a
recommendation reasoned from a desk; it is now a measurement. The first real
station — a Pi 2B rev 1.1 on Raspberry Pi OS 13 (Trixie), armhf, with an
ov5647 on the ribbon — ran both paths, and the result splits cleanly: **the
host runs the full pipeline** (804 KB of 1080p30 H.264 in a 3-second test, and
the live stream verified end to end in a real browser), while **the camera
container gets snapshots but not the stream**.

**Why the slim image cannot see a camera.** It is `python:3.11-slim-bookworm`
plus one pip dependency. There is no libcamera in it, no `rpicam-jpeg` and no
`rpicam-vid`. `gsu/camera/picsi.py` needs the first and the live stream needs
the second, so the camera is reported unavailable with *"no CSI camera support
on this box: no rpicam-jpeg was found"* — accurate, and still no picture. That is not a bug in the image; the image was sized for an
update layer of 91 KB over a metered link, and a camera stack is hundreds of
megabytes.

**What the camera image actually does on real hardware.**
`deploy/Dockerfile.camera` and `deploy/docker-compose.camera.yml` have now been
built and run on the Pi 2B (the image builds at 271 MB):

| | Measured |
|---|---|
| Sensor enumeration | **Works.** The ov5647 enumerates inside the container — under exactly the overlay's constraints: `/run/udev` mounted read-only, the device cgroup widened to all character devices, the `render` gid added. Every one of those was needed |
| Stills | **Was working, now unverified.** picamera2 inside the container captured frames from the real sensor — but the station no longer uses picamera2, and capture is now `rpicam-jpeg`, whose sibling `rpicam-vid` is the binary that bus-errors in this image. The bus error was in the *encoder* path, which a still capture does not use, so it may well be fine. Nobody has run it. `deploy/Dockerfile.camera` carries the one command that settles it |
| The live stream | **Fails.** `rpicam-vid` takes a **bus error in the encoder path** and dies before producing one frame — Debian armhf userland running against a Raspbian host. It spawns cleanly and dies asynchronously, so retrying the spawn never helps (`gsu/stream.py` says the same thing in code). The identical command on the host encodes 1080p30 through `/dev/video11`, so the fault is the image's userland — not the kernel, not device access, not the overlay |
| The stack | `rpicam-apps` from the Raspberry Pi archive, which is not Debian proper (`python3-picamera2` was dropped from the image along with the station's use of it). Build with `--build-arg SUITE=` matching the host's release. The archive keyring is vendored at `deploy/raspberrypi-archive-keyring.pgp` — the key on the website carries a SHA-1 binding signature that Trixie's Sequoia policy rejects outright, so fetching it at build time reads as an unsigned repository |

**Conclusion, as deployed: camera stations run systemd; the container path is
fine for everything else.** On the systemd path the coupling that produced the
bus error does not exist — the host's own libcamera and `rpicam-apps` are
built for the kernel they are running on, and they are updated with it.

**The route past the bus error is a Raspbian-based image, and it has not been
attempted.** There is no official Raspbian Trixie Docker base image, so the
plausible routes are these, written down so the option is a plan rather than a
hunch:

1. **debootstrap a Raspbian rootfs** (from the Raspbian mirror plus
   `archive.raspberrypi.com`) and import it as the base. Costs: you become the
   maintainer of a base image nobody publishes — its security update cadence,
   its provenance (nothing vouches for the rootfs beyond the archive keyrings),
   and keeping its suite in lockstep with the host's release.
2. **Use the host's own rootfs as the base** — a pristine Raspberry Pi OS Lite
   image's root filesystem, `docker import`ed. This guarantees the exact
   userland the host already proved works. Costs: the image is per-OS-release
   and must be rebuilt on every host upgrade or the coupling returns as
   staleness; and a rootfs taken from a *live* box rather than a pristine
   image would carry that box's identity (host keys, machine-id) into an
   image, which must not happen.

Either route keeps the overlay unchanged — udev, the cgroup widening and the
`render` gid were proven right by the snapshots working. Both break the
91 KB-update-layer economics (DECISIONS.md item 40) as thoroughly as the Debian
camera image does. Until a second camera station makes that maintenance worth
buying, the answer stays systemd.

**Superseded:** the installer creates the venv with `--system-site-packages`,
and that used to matter a great deal — `python3-picamera2` is a Debian package
bound to the system's libcamera build, so a venv without the flag could not
import it and the driver silently fell back to a subprocess per frame. **The
station no longer imports picamera2 at all**, so the flag no longer changes
what the camera does. It is kept because other system packages may want it and
because changing the installer to save a flag is not worth a re-run on every
box. Nothing is lost by leaving an existing install alone.

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
          Pi CSI camera via rpicam-jpeg, 640x480, quality 75
```

Once the proxy is in front of the API, the second line reads `platform API:
system CA bundle` instead, and that is also a PASS.

The two FAILs are dealt with in §8 and §10. Anything under **Trust** that fails
must be fixed here, not later.

**In the slim container that camera line is a `FAIL`**, reading *"no CSI
camera support on this box: no rpicam-jpeg was found"*. That is the expected
answer there, not a fault to chase. In the camera image it is a `PASS` — and the live
stream still is not (§3, the bus error). A station with a camera runs systemd.

**Read the camera line carefully — preflight does not take a picture.** It
reports that the driver could be built: that libcamera tooling is installed and
this board can be asked for a frame. A ribbon that is not seated, or a camera
that is not there, still shows `PASS` here and only settles at the first
capture, after which the slot reports `configured_absent` with libcamera's own
message. **`gsu camera` is what proves a picture** (§9), and it takes about a
second — but only with the service stopped: it opens the same sensor, and it
refuses rather than fighting the running station for it.

On a box with no `rpicam-jpeg` installed the line is a `FAIL` naming it.

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

### From a laptop or a phone, with no terminal

This is the path an installer takes, and it is the reason the setup page exists.
It needs the box provisioned with a setup password first — **without one the
page will not bind anywhere but loopback**, deliberately, so that a box nobody
gave a password to cannot end up serving an open form on a public address.

On the box, once, when it is built:

```bash
sudo -u gsu /opt/percepta/station/.venv/bin/python -m gsu setup-password
```

Paste the `GSU_SETUP_PASSWORD_HASH=…` line it prints into
`/etc/percepta/gsu.env`, set `GSU_SETUP_HOST` to the setup interface's address
(or `0.0.0.0` if it comes from DHCP), and `systemctl restart gsu`. **Write the
password on the box**, the way a router's is on a label. It is not the enrolment
code.

`python -m gsu preflight` reports what the page will actually do — the address,
whether a password is set, and whether the host setting is being ignored. Run it
before you drive out: "the page will be on loopback only" is cheap to learn at a
desk and expensive to learn standing at an enclosure.

Then, on site: join the setup network, open `http://<box>:8088`, enter the setup
password, and the page does the rest — enrolment code, then a card per slot for
what is fitted. The platform's address is shown but not editable; there is one
platform and it comes from `/etc/percepta/gsu.env`.

**The page closes behind you.** Thirty minutes after the last authenticated
action on an enrolled station, the LAN socket is closed and rebound to loopback
— the port stops answering rather than starting to refuse. To open it again,
reboot the station, or `touch /var/lib/percepta-gsu/setup-open` over SSH. While
the station is still *unenrolled* the window does not run down, so an installer
is never locked out mid-job.

### From a terminal, over an SSH tunnel

Loopback always answers, needs no password, and is unaffected by the window:

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

Which *interface* the setup page belongs on is still an open decision
(`DECISIONS.md`, open decision 2) — a dedicated Ethernet port and a laptop, or a
soft-AP and a USB Wi-Fi adapter on an already-contended USB bus. What it is
protected *by* is no longer open: a per-box password, a source-address check, a
window that closes, and CSRF on every form (`gsu/setup_access.py`, DECISIONS
item 41).

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

### Confirming ADS-B, and reading the nulls

Conformance checks that the ADS-B payload is *shaped* right. It cannot check
that the fields are populated, because on a station with no traffic overhead
an empty sky is a valid answer. `gsu adsb` is the check that needs an aircraft.

The receiver is on a serial port, so the service has to be stopped first —
two processes cannot read one UART, and the command refuses rather than
competing:

```bash
sudo systemctl stop gsu
sudo -u gsu /opt/percepta/station/.venv/bin/python -m gsu adsb --seconds 20
sudo systemctl start gsu
```

It prints one JSON object per contact, exactly as it would be published, plus
the receiver's own state and whether the barometric altitude correction is
running. **Read the nulls, not the numbers.** A null is one of two things and
they are worth telling apart:

* **the aircraft's** — a validity flag the transmitting aircraft left clear.
  `squawk`, `callsign`, `vertical_speed` and `track` are routinely null on real
  traffic and that is correct behaviour, not a decode fault. A *zero* in one of
  those would be the bug.
* **this station's** — `on_ground` is null for everything except surface
  emitter types 17, 18 and 19, because `ADSB_VEHICLE` carries no
  airborne/surface bit (CONTRACT-QUESTIONS.md item 19). `altitude_corrected_m`
  is null unless the correction is switched on and working, and the header line
  says which.

On live traffic, expect `icao`, `latitude`, `longitude`, `altitude`,
`altitude_type`, `emitter_type`, `seconds_since_contact`, `source`, `range_km`
and `bearing` populated on essentially every contact; `callsign`, `squawk`,
`track`, `speed` and `vertical_speed` populated on most; `simulated` false; and
`on_ground` null. An `emitter_type` of 0 is the receiver saying it was not told,
not a failure to decode.

`--out contacts.json` writes the same objects to a file to send on.

### Confirming video, which conformance does not cover

Conformance checks telemetry, audio and commands. Video is a separate channel
and a separate uplink, and it has its own three checks. Do them in this order —
each one needs less of the world than the one after it.

**1. Does the camera produce a picture at all?** No platform, no network, no
enrolment. On a camera station — which runs systemd, §3:

```bash
sudo -u gsu /opt/percepta/station/.venv/bin/python -m gsu camera --frames 3 --out /tmp/frame.jpg
```

or, on the container path (where snapshots work; the live stream does not — §3):

```bash
cd /opt/percepta/station
sudo docker compose -f deploy/docker-compose.yml run --rm gsu camera --frames 3 --out /tmp/frame.jpg
```

```
armv7l / Pi CSI camera via rpicam-jpeg, 640x480, quality 75

  1:   38.4 kB JPEG, 640x480,   184.2 ms, captured 2026-07-29T…Z
  2:   38.1 kB JPEG, 640x480,   171.5 ms, captured …
  3:   38.6 kB JPEG, 640x480,   169.8 ms, captured …

  38.4 kB per frame, mean over 3 frame(s). Nothing was published: these are
  preview captures.
```

There is no sustained-bitrate line any more, and its absence is the point:
these frames go nowhere. The periodic snapshot channel was removed
(CONTRACT-QUESTIONS.md item 17), so what costs bandwidth is the live stream,
which `gsu stream` measures.

That last line is the one to read before you leave: it is measured from this
camera at this setting, not estimated. Copy `/tmp/frame.jpg` off and look at it —
a picture of the site means the ribbon, the sensor and the encoder are all
right. **No frame** prints libcamera's own message, which is the diagnosis:
*"no cameras available"* is a ribbon or a camera, *"no rpicam-jpeg was found"*
is a package, and *"the camera is in use by …"* names whatever else has it.

**2. Does the live encoder keep up?** Still no platform needed. This one must
run on the host — inside the container `rpicam-vid` takes a bus error before
its first frame (§3):

```bash
sudo -u gsu /opt/percepta/station/.venv/bin/python -m gsu stream --seconds 30
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

### The camera is driven now, and has met a camera

This section has been rewritten twice, each time to retire a claim: first "the
camera is configured and undriveable", then "none of it has met a camera".
**Both retirements held.** What exists, all of it now running on the first real
station (a Pi 2B with an ov5647, live as of July 2026):

| | |
|---|---|
| Stills | **No longer published anywhere.** The `gsu/{station_id}/video` channel was removed — two readers of one sensor was the camera wedge (DECISIONS.md item 45, CONTRACT-QUESTIONS.md item 17). What remains is the setup page's preview: one frame, taken only while somebody has the page open, served at `/frame.jpg` and sent nowhere |
| Live video | H.264, fragmented MP4 over a WebSocket to the platform, started only while somebody is watching. 1080p30 through the Pi's hardware encode block (`/dev/video11`, chosen by `GSU_ENCODER=auto`), decoded in a real browser |
| Driver | `gsu/camera/picsi.py` for stills — `rpicam-jpeg`, one subprocess per frame, holding the sensor only while a frame is taken. `gsu/camera/h264.py` for the stream — a hardware encode block, or libav/x264, probed at start-up. Who owns the sensor at any moment is `gsu/camera/ownership.py`, and it is reported as `video.sensor` in the health frame |
| No camera fitted | `available: false` with a reason, on a cadence. Never silence, never a black frame |

**What the first camera taught it** — five fixes (the sensor contention counts
twice: it held in both directions), none reproducible without hardware, all
made and all pinned by tests (`tests/test_video.py`, `StartupContentionTests`;
HARDWARE.md §7 has the register):

- The systemd unit's `DeviceAllow` list was missing every node libcamera opens
  (`char-video4linux`, `char-media`, `char-dma_heap`) — an allow-list with the
  camera missing reads as *"no cameras available"* from a service whose own
  user can see the camera perfectly well from a shell.
- The snapshot path held the sensor against the encoder, both directions. Four
  fixes were made for this and none of them closed it; it was finally closed by
  **removing the second reader** and giving the sensor a single named owner —
  DECISIONS.md item 45 is the whole account, and it is worth reading before
  touching anything in this area.
- `stream.start` read state the monitor thread could null mid-start, turning a
  dead encoder into an `AttributeError` on top of a dead stream.
- The camera image's keyring fetch fell foul of Trixie's Sequoia policy (§3).

The first two commands in §9 remain the ones that matter on install day:
`gsu camera` proves the picture, `gsu stream` proves the encoder. Both work
with no platform and no network, and both print numbers rather than opinions.

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
| libcamera's own words — *"no cameras available"* | the ribbon or the camera. `gsu camera` reproduces it in a second, and the message comes from libcamera rather than from this software. **In a container** it is also what a missing `/run/udev` mount and a too-narrow device cgroup produce — §3 |
| *"no CSI camera support on this box: no rpicam-jpeg was found"* | packages: `sudo apt install rpicam-apps`. **On the slim container path this is the expected answer** and the fix is §3, not a package |
| *"the camera is in use by the live stream"* | not a fault. One sensor, one owner; the preview resumes when the viewer leaves, and `video.sensor.holder` in the health frame says who has it right now |
| *"the camera is held by … and did not come free within 10s"* | a stream that could not start because something else had the sensor. Also not a fault, and it names the holder. If the holder is never `null`, something is not releasing — that is a bug, and `video.sensor.held_for_s` is the evidence |

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
| 8088 | `127.0.0.1` | the setup page, over an SSH tunnel. No password: SSH already authenticated you |
| 8088 | `$GSU_SETUP_HOST` | the setup page on the setup network — **only** if a setup password is set, **only** from a private source address, and **only** while the window is open |
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

If the setup page is moved off loopback it belongs on a private setup network
and nowhere else. Four things have to be true before it is served there at all,
and the reasoning for each is in `gsu/setup_access.py`:

1. `GSU_SETUP_HOST` names something other than loopback — an edit somebody made;
2. a setup password is configured. **Without one the host setting is ignored and
   the page binds to loopback anyway.** There is no code path that opens this
   listener on a routable interface without a secret in front of it;
3. the request comes from 10/8, 172.16/12, 192.168/16 or link-local. Carrier-
   grade NAT (100.64/10) is **not** in that list — on a Starlink site that range
   is the carrier's shared network, not this site's LAN;
4. the window is open: unenrolled, or within `GSU_SETUP_WINDOW_MINUTES` of the
   last authenticated action.

Every form carries a CSRF token bound to the session cookie, every response is
`no-store` under a CSP that permits no script and no framing, and the `Host`
header must be an address or a `.local` name — which is what stops a public page
rebinding its own name to this box and driving the form from inside a
technician's browser.

**One caveat, and it is a real one: control 3 does not work under Docker.**
Every request then arrives from the bridge gateway, which is inside 172.16/12,
so the source-address check passes for everyone. On the container path the
password and the window are the only two controls left. Publish the port to a
specific LAN address rather than `0.0.0.0` if you use it, or use the systemd
path, which is the recommended one for camera-equipped stations anyway.

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

Three things, honestly:

- **The camera works fully here and only partly in the container — measured,
  not reasoned (§3).** The host has libcamera and `rpicam-apps` built for the
  kernel and firmware they are running on and updated with them; on the first
  real Pi this path ran 1080p30 through the hardware encoder end to end. The
  camera image enumerates the sensor, but `rpicam-vid` dies with a bus error
  before its first frame, and whether `rpicam-jpeg` shares the fault is now an
  open question rather than a settled one — see §3. **For a station with a
  camera, this is the path.**
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
changed twice — and because the register below was written before any of it
had touched the target hardware, and has now been corrected against the first
real station: **Station1**, a Pi 2B rev 1.1 on Raspberry Pi OS 13 (Trixie),
armhf, enrolled and live as of 2026-07-29.

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

**Verified on the first real station (Pi 2B, Raspbian 13 Trixie, armhf):**

- `install.sh`, end to end — and it surfaced the uid convention now pinned:
  the host account and the image share uid 10001, because the first install's
  floating uid left the docker path unable to read its own state.
- The systemd unit runs the live station, after its `DeviceAllow` list gained
  the three entries libcamera actually opens (`char-video4linux`,
  `char-media`, `char-dma_heap`).
- ARMv7, a real UART (the Airmar 110WX, publishing real weather), and the
  camera. Enrolment, telemetry, snapshots at 2 fps, and the on-demand 1080p30
  stream — 804 KB of H.264 in a 3-second test, and the live stream decoded in
  a real browser (208 fragments, `avc1.640028`).
- The slim image builds and the container runs; the camera image builds
  (271 MB) and enumerates the sensor and captures snapshots under exactly the
  overlay's constraints. `rpicam-vid` in that container takes a bus error
  before its first frame — §3 has the full account and the route past it.

**Never run, anywhere:**

- `gsu-update.sh` has **never driven a real container** — only the stub. Run
  it by hand once before letting the timer near it.
- The SDR has no driver, so nothing has exercised it beyond enumeration.
- No stream has crossed a real satellite link; every live-stream measurement
  so far is LAN.
