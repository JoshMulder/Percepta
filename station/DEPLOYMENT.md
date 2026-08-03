# Deploying a ground station

From a blank SD card to a station the platform accepts data from.

**The station runs as a container.** That is the only deployment path. The host
needs Docker and nothing else: no systemd unit, no installer, no udev rule
except the one for the SDR, and nothing under `/opt`.

Nothing here uses `--insecure` and nothing disables certificate verification.
If a step fails on a certificate, the certificate is the problem. A documented
step that skips verification is how skipping becomes normal.

---

## The short way

On the box:

```bash
git clone <repo> ~/percepta
cd ~/percepta/station && ./bootstrap.sh
```

It asks three things — the platform URL, a name for the site, and a password
for the setup page — writes `.env`, builds the image and starts the container.
The first build takes a few minutes on a Pi.

Then enrol it with a code from the console (Settings → Enrolment):

```bash
cd ~/percepta/station && docker compose run --rm gsu enrol --token XXXX-XXXX-XXXX
```

That is the whole thing on a box with no CSI camera. Re-running `bootstrap.sh`
is the supported way to change your mind: it reads the existing `.env` for its
defaults and rewrites it.

---

## What you need before you start

**Hardware**

| | |
|---|---|
| Raspberry Pi 2B | ARMv7, 1 GB RAM. See HARDWARE.md for what it will and will not carry |
| SD card | 8 GB or more, Class 10. This is the part that wears out |
| Airmar 110WX | on a USB-UART |
| uAvionix ping RX Pro | on a USB-UART |
| RTL2838 | airband |
| Network camera | ONVIF/RTSP. **Not a CSI camera** — see below |
| Network | Ethernet or a Starlink terminal. The Pi 2B has no onboard Wi-Fi |

**A CSI camera will not stream — fit a network camera.** Measured, not
reasoned: on the first real Pi, `rpicam-vid` in the container takes a bus error
in the encoder path and dies before one frame (Debian armhf userland against a
Raspbian host). The station carried a whole second deployment shape — the agent
as a systemd service on the host — for that one case, and two deployment shapes
for a camera nobody fits cost more than the camera was worth. HARDWARE.md has
the measurements.

**Software.** Raspberry Pi OS Bookworm (12) or Trixie (13). The image pins its
own Python, so the host's version does not matter; what matters is Docker.

**From the platform admin**

1. **The platform URL**, e.g. `https://percepta.example.com`. The broker
   address is *not* something you configure — the platform states it at
   enrolment, and since the relay is served by the platform itself it is
   `wss://<the host you enrolled against>/broker`.
2. **A station record** in the right organisation, and **an enrolment code** for
   it. The code is short-lived — 24 hours — so get it when you are ready to use
   it, not a fortnight before.

You do not need to carry any CA. You never type the station's UUID; it comes
back in the enrolment response.

---

## 1. Prepare the Pi

Flash Raspberry Pi OS Lite (64-bit will not run on a 2B; use the 32-bit image).
Enable SSH and set a user in Raspberry Pi Imager's advanced options.

Then, on the box:

```bash
sudo apt update && sudo apt full-upgrade -y
```

```bash
sudo apt install -y docker.io chrony
```

Compose v2, whichever name this release uses. Not `docker-compose`, which is v1
and has no `docker compose` subcommand:

```bash
sudo apt install -y docker-compose-v2 || sudo apt install -y docker-compose-plugin
```

```bash
sudo systemctl enable --now docker
```

**Time.** Install `chrony` even though `systemd-timesyncd` is present — chrony
is what a GPS time source later plugs into, so the GPS upgrade becomes a config
file rather than a change of daemon.

**The SDR.** The kernel's DVB driver grabs RTL2832U devices on sight and then
nothing else can open them. The symptom if you skip this is "device busy" on a
dongle nothing else is using:

```bash
echo -e 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830' | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
```

And so the container can open it without root — the container runs as `gsu`
with the `plugdev` group added, and the raw USB node is root-only otherwise:

```bash
sudo cp ~/percepta/station/deploy/99-percepta-sdr.rules /etc/udev/rules.d/
```

Then reboot.

**Reduce SD card wear.** This box writes an event database and audio recordings
continuously, and the SD card is the most likely hardware failure on a remote
site:

```bash
sudo systemctl disable --now man-db.timer apt-daily.timer apt-daily-upgrade.timer
```

---

## 2. Stand it up

```bash
cd ~/percepta/station && ./bootstrap.sh
```

It writes `.env` beside `docker-compose.yml` and brings the container up.
`.env` is gitignored, so `git pull` cannot clobber the answers somebody gave
when the box was commissioned, and the setup password hash it holds never
reaches the repository.

`deploy/gsu.env.example` documents every other key the agent understands. It is
a **reference, not a template** — do not copy it over `.env`.

### Reaching the setup page

It is on the site LAN, on port 80, from the moment the container starts. Open
`http://<box>/` and enter the setup password.

**The password is the control, not the interface.** The agent refuses to serve
the page at all without `GSU_SETUP_PASSWORD_HASH` — it demotes itself to the
container's own loopback, which no port mapping reaches — so forgetting the
password gives an unreachable page rather than an open one. `bootstrap.sh` will
not finish without collecting one.

Port 80 and not 443 because the console speaks plain HTTP and has no
certificate. On 443, every browser would try HTTPS first and fail. **So the
password and the enrolment code cross the LAN in clear text**, which is the
trade for a page an installer can actually open, and the reason the window
below matters.

To put it back on loopback for a box whose LAN you do not trust, set
`GSU_SETUP_BIND=127.0.0.1` in `.env`, `docker compose up -d`, and tunnel:

```bash
ssh -L 8080:127.0.0.1:80 <user>@<box>
```

`GSU_SETUP_HOST_PORT` moves it off 80 if something else on the box wants that
port. Both are host-side; the container always listens on 8088.

---

## 3. Preflight

Before enrolling:

```bash
cd ~/percepta/station && docker compose run --rm gsu preflight --probe
```

`--probe` opens a TLS connection to the platform and verifies its certificate.
It sends no token and no credential, so it is safe to run before enrolling.

Every line is `PASS`, `WARN` or `FAIL`. **A `FAIL` is something that will not
work.** Expected on a first run:

```
Clock
  PASS  plausible
  PASS  disciplined by ntp
  WARN  no hardware RTC
Trust
  PASS  platform API: system CA bundle
  WARN  broker: no CA pinned yet   (arrives in the enrolment response)
  WARN  broker: no address yet     not enrolled
Identity
  WARN  not enrolled
Devices
  FAIL  adsb: uAvionix ping RX Pro …: no serial port set for this device
  FAIL  radio: RTL-SDR airband …: needs a rtlsdr assigned to it
```

The device FAILs are dealt with in §5. **Anything under Trust that fails must
be fixed here, not later.**

### The two trust roots, because they are not the same root

This trips people up once and then never again.

| | Verified against | You configure |
|---|---|---|
| **Broker** `wss://…/broker` | a **pinned private CA**, always | nothing — it arrives in the enrolment response and is persisted at `$GSU_HOME/broker-ca.pem`, 0600 |
| **Platform API** `https://` | the **system CA bundle** by default | `GSU_API_CA_FILE` to pin it instead |

`broker.ca_pem` in the enrolment response is the **broker's** trust root. The
field is named for what it is. Using it for the API as well works only for as
long as the two share a certificate authority.

**Behind a reverse proxy with a public certificate — which is the normal
arrangement — there is nothing to configure.** Leave `GSU_API_CA_FILE` unset.
Setting it pins the API to a CA that will not be the one answering, which is not
security; it is an outage with a certificate error attached, and it reads as
*"unable to get local issuer certificate"* against a CA that was correct last
week.

Set it only while the platform serves its own certificate, and unset it the day
a proxy lands in front.

Neither setting can disable verification and neither falls back to plaintext.
There is no third option and deliberately no flag for one.

---

## 4. Enrol

### From a laptop or a phone, with no terminal

Join the setup network, open `http://<box>/`, enter the setup password, and
the page does the rest — enrolment code first, then a card per slot for what is
fitted. The platform's address is shown but not editable.

**The page closes behind you.** Thirty minutes after the last authenticated
action on an enrolled station, the LAN socket is closed and rebound to loopback
— the port stops answering rather than starting to refuse. To open it again,
restart the container, or `touch` `setup-open` in the state volume. While the
station is still *unenrolled* the window does not run down, so an installer is
never locked out mid-job.

### From a terminal

```bash
cd ~/percepta/station && docker compose run --rm gsu enrol --token XXXX-XXXX-XXXX
```

That writes the credential into the shared state volume, and **the running
container notices it within a few seconds** and attaches without a restart —
the one-shot container and the long-running one are looking at the same volume.

**If the code is refused**, the message is deliberately the same for unknown,
expired and already-used: *"This code is not valid. Ask for a new one."* Asking
for another is cheap and audited.

**Enrolment is resumable.** If the link drops halfway, run it again with the
same code. The platform re-issues rather than refusing — the failure that
matters is a technician stranded with a used code.

### If the platform is behind a reverse proxy

**Enable WebSocket support for the proxy host.** The broker is a WebSocket at
`/broker` and the live video uplink is one at `/media/ingest`. Without it,
enrolment succeeds over plain HTTPS and looks perfect, and then the station
publishes nothing at all — which reads as a broken station rather than a proxy
setting. The proxy must pass `/api/`, `/broker` and `/media/ingest` through, and
must not require a client certificate.

---

## 5. Point the drivers at what is fitted

**Do this after the box has booted with both adapters plugged in**, so the names
are real:

```bash
ls -l /dev/serial/by-id/
```

Set each device's port on the setup page — the field offers what is plugged in.
**Use the `by-id` names.** `/dev/ttyUSB0` is a trap: two adapters enumerate in
whichever order the kernel probed them, that order changes between boots, and
when they swap, the weather driver reads the ADS-B stream and vice versa. It
presents as both instruments failing at once, which sends you looking for a
power fault.

Which is which: unplug one, re-run `ls`, and see which name disappeared. The
Airmar is 4800 baud, the ping RX Pro 57600.

The SDR is claimed by serial number rather than by port, and the camera is a URL
with no device node at all.

Then:

```bash
cd ~/percepta/station && docker compose run --rm gsu devices
```

`present` means the driver is constructed **and the device is talking**.
`configured, gone quiet` means it answered once and stopped — a different fault
from `configured, not detected`, and worth different action.

---

## 6. Confirm it is working

```bash
cd ~/percepta/station && docker compose logs -f
```

Look for, in order:

```
Broker TLS trust: pinned CA from enrolment, SHA-256 …
Platform API TLS trust: system CA bundle (public certificate, not pinned)
Station <name> (<uuid>) attached: publishing to gsu/<uuid>/telemetry …
```

`docker ps` should show `percepta-gsu` as `Up`, not `Restarting`. A container
cycling through `Restarting` is a crash loop, and `logs --tail 100` says why.

**On the setup page** — the rows that matter: Enrolled *yes*, Link *up*, Broker
security *TLS, CA pinned*, Clock kept by *NTP*, Telemetry sent *increasing*,
Dropped *not increasing*.

**From the platform side** the station goes online on its own: the ingest writes
`last_seen_at` from the telemetry itself, and there is no separate heartbeat.

**Conformance**, from a machine that can reach the broker:

```bash
python contract/conformance/check_station.py --station <uuid>
```

Streams with no driver are reported as declared-unavailable and skipped, not
failed. A station is not failed for lacking hardware, only for pretending.

### The camera, which conformance does not cover

Video is a separate channel and a separate uplink. `video.stream.state` stays
`idle` until a viewer attaches, and that is correct — the stream is on demand.
When somebody watches, the log says:

```
Media uplink open to wss://…/media/ingest
Streaming 1920x1080 at 30 fps, 3000 kbit/s target
```

If the picture is there but the stream never starts, check
`video.stream.uplink`. `none (no media URL configured)` means it would encode
into a counter, and `GSU_PLATFORM_URL` is what it derives from.

---

## 7. Updating, and going back

```bash
cd ~/percepta/station && git pull && docker compose up -d --build
```

Rolling back is `git checkout` of a tag already on the disk, then the same
command. **Nothing has to be downloaded to roll back**, which matters on the
link that may be the reason you are rolling back.

There is no updater daemon, no image registry and no gating. There was: a
jittered timer pulled an image, tagged the running one `previous`, gated the new
one on whether it enrolled and published, and rolled back if it did not. It went
when the agent stopped running on the host. What it bought — an atomic update
with a rollback target already on disk — the checkout gives for free.

State is a **named Docker volume**, so `up -d --build` never touches the
credential, the device inventory, the events or the recordings.

---

## 8. Reading the logs

```bash
cd ~/percepta/station && docker compose logs --since 1h
```

Container logs are rotated at 10 MB × 3 by the compose file. Docker's default is
**no rotation at all**, which on a station that logs for months fills the SD
card and takes the site down.

The lines that matter are health conditions. Each is raised once with a
severity, kept while it persists — the age of a problem is the useful number —
and cleared when it goes away.

| Condition | Means |
|---|---|
| `uplink.refused` | The station will not connect on these terms and is publishing nothing. A plaintext URL, or no CA. Not a network fault |
| `uplink.tls_failed` | The broker's certificate did not verify against the pinned CA |
| `tls.api_trust_unusable` | `GSU_API_CA_FILE` is set to something unreadable. It does **not** fall back to the system bundle — you asked for pinning |
| `uplink.down` | No route to the broker. Weather, an obstruction, a dead link |
| `credential.renewal_failing` | Renewal is failing. Warning, escalating to critical inside six hours of expiry |
| `credential.revoked` | The platform no longer accepts this station. Needs a new code |
| `clock.implausible` | The clock is not credible. Enrolment is refused until it is |
| `devices.absent` | Something configured is not answering |

The same list is on the setup page under **Needs attention**, which works with
no link at all.

---

## 9. When something is wrong

**The container will not start.** `docker compose logs --tail 50`. Most likely
`.env` has a syntax error — it is shell-ish: `KEY=value`, no spaces around `=`.

If it fails with a *device* error, that should not be possible: the compose file
bind-mounts `/dev` wholesale rather than naming nodes, precisely so a missing
sensor cannot stop the station. If somebody has reintroduced a `devices:` list,
that is the cause, and DECISIONS.md item 35c is why it was removed.

**The certificate does not verify.** Work out which link first — they have
different roots and different fixes, and the message says which one it is.

*The platform API.* Almost always one of two things. Either a proxy with a
public certificate is in front and `GSU_API_CA_FILE` is still set to an old
private CA — unset it. Or the platform serves its own certificate and the pin is
missing or wrong.

*The broker.* If its CA has been rotated, re-enrol: the new CA arrives in the
enrolment response and is pinned from then on.

**There is no setting that skips verification, and adding one would be the wrong
fix.** A station that accepts any certificate hides exactly the fault you are
looking at, and hides it everywhere, for ever.

**Enrolment is refused with a 422.** The station now appends the platform's own
explanation, which names the field. Without that it read as "the box sent
something the platform could not read. This is a bug." and nothing else.

**A device is configured and not detected.** The reason is in `gsu devices`, on
the setup page, and in the `unavailable_reason` the platform receives. For
serial devices it names the ports that *are* present.

**"Another agent is already running."** Two copies. Check nothing is left from
an older install — `systemctl is-enabled gsu` — and stop it. Two agents on one
station publish two independent worlds onto one channel, and the console
alternates between them.

**Start again from nothing:**

```bash
cd ~/percepta/station && docker compose down -v
```

`-v` is what drops the state volume: credential, pinned broker CA, device
inventory, events and recordings. Then `./bootstrap.sh` and a fresh code.

---

## 10. Time, and the GPS receiver

**This box has no battery-backed clock.** On every boot its time is whatever the
filesystem suggested until NTP answers. `contract/enrolment.md` §6 is blunt
about the cost: a station with a wrong clock cannot authenticate, and if it
believes its credential has expired it cannot renew either. That is a site visit
for a bad number.

The station refuses to enrol with an implausible clock, raises
`clock.implausible` as a critical condition, and reports what is disciplining
its clock in every health frame.

**Fit an RTC.** A DS3231 module is a few pounds and removes the class of failure
— HARDWARE.md §4 has the wiring.

**When the GPS receiver arrives**, it goes into chrony, not into this software:

```
refclock SHM 0 refid GPS precision 1e-1 offset 0.128 delay 0.2
refclock PPS /dev/pps0 refid PPS lock GPS precision 1e-7
```

with `gpsd` feeding SHM. No station code changes: `gsu preflight` starts
reporting `disciplined by gps` on its own, because chrony's reference id becomes
`PPS`.

---

## 11. What is exposed

| Port | Bound to | What |
|---|---|---|
| 80 | `$GSU_SETUP_BIND`, `0.0.0.0` by default | the setup page on the site LAN — **only** with a password set, and **only** while the window is open. Plain HTTP, so the password and the enrolment code cross the LAN in clear |
| 80 | `127.0.0.1`, if you set `GSU_SETUP_BIND` back | the setup page over an SSH tunnel instead |
| 22 | everything | SSH. Key-only, please: this box is on the public internet |

The station makes **outbound** connections only: to the platform API, to the
broker, and — only while somebody is watching — a WebSocket for the live video.
Nothing reaches inward: Starlink is CGNAT and the platform can never initiate a
connection to a station. That is also why a viewer asks for video through the
*command* channel rather than by connecting to the box.

**Isolation was traded away deliberately**, on the owner's instruction, because
nothing else runs on this box. The container gets the host's whole `/dev` and
broad device permissions so that a missing sensor cannot stop the station
starting and a replugged one is picked up without anybody touching it. See
DECISIONS.md item 35c. `privileged: true` is still not used.

**One caveat, and it matters more now the page is on the LAN by default: do not
count on the source-address check under Docker.** It refuses anything outside
10/8, 172.16/12, 192.168/16 and link-local, but what the container sees as the
client address depends on how the daemon publishes the port — iptables DNAT
preserves it, the userland proxy replaces it with the bridge gateway, which is
itself inside 172.16/12 and so passes for everyone. **This has not been measured
on the box.** Assume the password and the window are the only two controls that
are certainly working.

Two things follow. Set `GSU_SETUP_BIND` to the setup interface's own address
rather than leaving it at `0.0.0.0` wherever you know that address. And if the
box has a public IP rather than sitting behind CGNAT, treat port 80 as reachable
from the internet and put it back on loopback.

---

## 12. Backups

**Scheduled on the platform, not here.** Nothing in this runbook backs anything
up.

What lives only on the station, and what it costs to lose:

| | If the SD card dies |
|---|---|
| Credential and pinned broker CA | re-enrol with a new code. Minutes |
| Device inventory | re-enter the ports on the setup page. Minutes |
| Event database | **lost.** Proximity alerts and outage records for the retention window |
| Audio recordings | **lost.** Up to 24 h / 200 MB |
| Video | **nothing to lose.** No frame is stored on the box |

Neither of the last two has a channel to the platform yet — CONTRACT-QUESTIONS.md
item 4, still open — so a card failure loses them. That is an argument for the
event channel, not for backing up an SD card in the field.

---

## Appendix: everything in one place

```bash
cd ~/percepta/station

./bootstrap.sh                           stand it up, or change your mind
docker compose up -d --build             start, or apply a git pull
docker compose logs -f                   follow
docker compose down -v                   START AGAIN: drops the state volume
docker compose run --rm gsu preflight --probe   everything that must be true
docker compose run --rm gsu enrol --token …     claim a code
docker compose run --rm gsu devices      intent against fact
docker compose run --rm gsu whoami       what this box thinks it is, offline
docker compose run --rm gsu status       what the platform thinks of it
docker compose run --rm gsu bench        what a tick costs on this hardware
docker compose run --rm gsu camera --frames 3   one picture, and what it costs
docker compose run --rm gsu stream --seconds 30 the live encoder, measured
docker compose run --rm gsu setup-password      hash a new page password
```

| | |
|---|---|
| `station/.env` | this site's answers. Gitignored, holds the page password hash |
| `station/docker-compose.yml` | how it runs |
| `station/deploy/Dockerfile` | what it runs |
| `gsu-state` volume | credential, pinned broker CA, inventory, events, recordings |
| `percepta/gsu:local` | the image, built from this checkout. Never pulled |

---

## Appendix B: what has actually been run, and what has not

The distinction matters more than usual here, because the deployment path
changed twice. The register below has been corrected against the first real
station: **Station1**, a Pi 2B rev 1.1 on Raspberry Pi OS 13 (Trixie), armhf.

**Verified on x86-64:**

- TLS to the broker with a pinned private CA, refusal of the wrong CA, refusal
  of plaintext, and refusal to downgrade.
- Enrolment over HTTPS, CA persistence at 0600, reconnection from the persisted
  CA alone, and the broker/API trust split in both configurations.
- Full contract conformance with no `--insecure`.
- Video end to end against the running platform: the live stream as fragmented
  MP4 over a WebSocket, 1080p30, played back in a browser.
- The on-demand lease: a repeated `video.start` renews rather than restarting,
  and a lease left to expire stops the encoder on its own.

**Verified on the first real station (Pi 2B, Trixie, armhf):**

- ARMv7, a real UART (the Airmar 110WX, publishing real weather), and a camera.
  Enrolment, telemetry, and the on-demand 1080p30 stream decoded in a browser.
- The container runs and the device reasoning held.
- `rpicam-vid` in a container takes a bus error before its first frame, which is
  why CSI cameras are out of scope.

**Never run, anywhere:**

- No stream has crossed a real satellite link; every measurement so far is LAN.
- The SDR has been exercised on the bench, not at a site.
