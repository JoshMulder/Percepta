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
| Pi camera | CSI ribbon. **No driver in this build** — see §10 |
| Network | Ethernet or a Starlink terminal. The Pi 2B has no onboard Wi-Fi |

**Software**

Raspberry Pi OS **Bookworm** (32-bit, armhf) or newer. Bookworm ships Python
3.11; the agent needs 3.11 or later because it uses `datetime.UTC`. Bullseye
ships Python 3.9 and **will not run this** — that is an OS upgrade, not a patch,
and the installer refuses rather than half-working.

**From the platform admin, before you go anywhere**

1. **The platform CA**, as a PEM file. This is the one thing you must carry with
   you: the first enrolment call happens before the station has pinned anything,
   so it can only be verified against a CA installed out of band.
2. **Its SHA-256 fingerprint**, told to you separately from the file itself —
   read out, or from a message you already trust. Checking the file against
   itself proves nothing.
3. **The platform URL and the broker URL**, with ports. For example
   `https://platform.example.net:8000` and `rediss://platform.example.net:6380/0`.
4. **A station record** created in the right organisation, and **an enrolment
   code** for it. The code is short-lived — 24 hours by default — so get it when
   you are ready to use it, not a fortnight before (`DECISIONS.md`, open
   decision 3).

You never type the station's UUID. It comes back in the enrolment response.

---

## 1. Prepare the Pi

Flash Raspberry Pi OS Lite (64-bit will not run on a 2B; use the 32-bit image).
Enable SSH and set a user in Raspberry Pi Imager's advanced options, or put an
empty `ssh` file on the boot partition.

Then, on the box:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv chrony
sudo raspi-config     # expand the filesystem; set the hostname and timezone
```

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
sudo /tmp/station/deploy/install.sh --ca /tmp/platform-ca.pem
```

It is idempotent — re-run it to upgrade — and it never overwrites
`/etc/percepta/gsu.env`, the state directory, or an existing device inventory.

What it does:

| | |
|---|---|
| `/opt/percepta/station` | the code, owned by root and **not writable by the agent** |
| `/opt/percepta/station/.venv` | Python environment with one dependency, `redis` |
| `/etc/percepta/gsu.env` | configuration, `0640 root:gsu` |
| `/etc/percepta/platform-ca.pem` | the CA you carried in |
| `/var/lib/percepta-gsu` | state: credential, pinned CA, inventory, events, recordings. `0700 gsu` |
| user `gsu` | system account, no login, in `dialout`, `video`, `plugdev` |
| `/etc/systemd/system/gsu.service` | the unit, enabled but not started |
| `/etc/udev/rules.d/99-percepta-sdr.rules` | so the SDR is readable without root |

**Check the CA fingerprint it prints against the one you were told.** This is
the only step in the whole procedure that a person has to verify by eye, and
everything else rests on it.

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
GSU_CA_FILE=/etc/percepta/platform-ca.pem
GSU_REQUIRE_TLS=1
```

Host and port are both yours to set on both URLs.

**`GSU_BROKER_URL` must be an address and nothing else.** Not a username, not a
password. redis-py lets a URL override the credentials passed alongside it, so a
URL carrying `user:pass@` would replace this station's identity with whatever it
names — failing confusingly at best and, at worst, publishing as somebody else.
The agent strips and warns rather than obeying, but do not rely on that.

---

## 5. Preflight

Before starting anything:

```bash
cd /opt/percepta/station
sudo -u gsu env $(grep -v '^#' /etc/percepta/gsu.env | xargs) \
  .venv/bin/python -m gsu preflight --probe
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
  PASS  CA pinned from installed
        SHA-256 50:03:05:1A:...
  PASS  platform API: https://…    verified …, certificate CN=…
  WARN  broker: no address yet     not enrolled
Identity
  WARN  not enrolled
Serial ports present
  PASS  /dev/serial/by-id/usb-FTDI_…
Devices
  FAIL  adsb: uAvionix ping RX Pro …: no serial port set for this device
  FAIL  radio: RTL-SDR airband …: not supported by this software build
```

The last two are dealt with in §8 and §10. Anything under **Trust** that fails
must be fixed here, not later.

---

## 6. Start it

```bash
sudo systemctl start gsu
journalctl -u gsu -f
```

It starts whether or not there is a network, whether or not it is enrolled, and
whether or not any sensor answers. That is deliberate: sensing, recording and
local alerting must not depend on anything remote.

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
- **Uplink security** → `TLS, CA pinned 50:03:05:1A:…`

Headless alternative, over SSH:

```bash
cd /opt/percepta/station
sudo -u gsu env $(grep -v '^#' /etc/percepta/gsu.env | xargs) \
  .venv/bin/python -m gsu enrol --token XXXX-XXXX-XXXX
```

The running service notices the new credential on disk within a few seconds and
attaches without a restart.

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

Then:

```bash
sudo -u gsu env $(grep -v '^#' /etc/percepta/gsu.env | xargs) \
  /opt/percepta/station/.venv/bin/python -m gsu devices
```

`present` means the driver is constructed **and the device is talking**.
`configured, gone quiet` means it answered once and stopped — a different fault
from `configured, not detected`, and worth different action.

---

## 9. Confirm it is working

**On the box**

```bash
journalctl -u gsu -n 50
```

Look for, in order:

```
TLS trust: pinned CA from installed, SHA-256 …
Station <name> (<uuid>) attached: publishing to gsu/<uuid>/telemetry …
Subscribed to cmd/gsu/<uuid> as gsu:<uuid>.
```

Then `systemctl status gsu` should show `active (running)` with a low restart
count.

**On the setup page** — the six rows that matter: Enrolled *yes*, Link *up*,
Uplink security *TLS, CA pinned*, Clock kept by *NTP*, Telemetry sent
*increasing*, Dropped *not increasing*.

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

---

## 10. What this build does not do

Two of the four fitted devices have **no driver in this build**, and say so:

| Device | What the station does |
|---|---|
| RTL-SDR airband | publishes `radio` as `available: false`, reason *"not supported by this software build"*, on the normal cadence |
| Pi camera | reports as configured and undriveable. There is no media channel in the contract either (`CONTRACT-QUESTIONS.md` item 7), so there is nowhere for a frame to go |

They are selected in the inventory anyway, so that the platform and the console
can see the hardware is fitted and that what is missing is software. Nothing is
stubbed to look like it works.

`power` and `light` have no hardware specified for this site, so those slots are
empty and their streams are declared unavailable for that reason instead.

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
journalctl -u gsu -f                     # follow
journalctl -u gsu -p warning --since -1h # just the problems
journalctl -u gsu --since "2026-07-28"   # a day
```

The lines that matter are health conditions. Each is raised once with a
severity, kept while it persists — the age of a problem is the useful number —
and cleared when it goes away.

| Condition | Means |
|---|---|
| `uplink.refused` | The station will not connect on these terms and is publishing nothing. A plaintext URL on a pinned box, or no CA. Not a network fault |
| `uplink.tls_failed` | The broker's certificate did not verify against the pinned CA |
| `uplink.down` | No route to the broker. Weather, an obstruction, a dead link |
| `credential.renewal_failing` | Renewal is failing. Warning, escalating to critical inside six hours of expiry |
| `credential.revoked` | The platform no longer accepts this station. Needs a new code |
| `clock.implausible` | The clock is not credible. Enrolment is refused until it is |
| `clock.unsynchronised` | Nothing is disciplining the clock |
| `devices.absent` | Something configured is not answering |
| `telemetry.unsourced` | Streams with no source at all. Expected on this build for `radio` |
| `tls.not_pinned` | Verifying against the OS CA bundle rather than the platform's |

The same list is on the setup page under **Needs attention**, which works with
no link at all.

---

## 13. When something is wrong

**The service will not start.**
`systemctl status gsu` and `journalctl -u gsu -n 50`. Most likely:
`/etc/percepta/gsu.env` has a syntax error (it is shell-ish: `KEY=value`, no
spaces around `=`), or the venv is missing — re-run the installer.

If it fails with a memory-protection error after someone has added a dependency
with a native extension, remove `MemoryDenyWriteExecute=yes` from the unit —
and write down why.

**"Another agent is already running."**
A stale lock, or genuinely two copies. `systemctl stop gsu`, check for strays
with `pgrep -af "gsu run"`, then start again. Two agents on one station publish
two independent worlds onto one channel and the console alternates between them.

**Nothing is being published, and the link looks fine.**
Check **Uplink security** on the setup page. `uplink.refused` means the station
decided not to connect — which is a configuration fault, not a network one. The
message names the fix.

**The certificate does not verify.**
Compare fingerprints; the station prints the one it has pinned:

```bash
sudo -u gsu ... python -m gsu preflight        # prints the pinned fingerprint
openssl s_client -connect <host>:6380 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -fingerprint -sha256
```

If the platform's CA has been rotated, re-enrol: the new CA arrives in the
enrolment response and is pinned from then on. **Do not** work around it with
`GSU_TLS_TRUST=system` — that accepts any CA the OS trusts and hides exactly the
fault you are looking at.

A CA that `redis-cli --cacert` accepts may still be rejected by Python, which
requires `basicConstraints` and `keyUsage` on a CA certificate. If `redis-cli`
works and the station does not, that is the first thing to check on the
platform side.

**A device is configured and not detected.**
The reason is in `gsu devices`, on the setup page, and in the `unavailable_reason`
the platform receives. For serial devices it names the ports that *are* present.
Check the user is in `dialout` (`id gsu`) and that you used a `by-id` path.

**It enrolled and now the credential is refused.**
`credential.revoked` means an admin revoked it, or another box claimed this
station's enrolment — a re-claim cuts off the box that had already succeeded
(`contract/enrolment.md` §11). Get a new code and re-enrol. The station keeps
recording throughout; it is cut off, not disabled.

**Start again from nothing.**

```bash
sudo systemctl stop gsu
sudo rm /var/lib/percepta-gsu/credential.json /var/lib/percepta-gsu/platform-ca.pem
sudo systemctl start gsu
```

Keeps the device inventory, the events and the recordings; drops the identity.
Then enrol with a fresh code.

---

## 14. Upgrading

```bash
rsync -a --exclude var/ --exclude .venv/ ~/percepta/station/ pi@<box>:/tmp/station/
sudo /tmp/station/deploy/install.sh
sudo systemctl restart gsu
```

Configuration, state and the device inventory all survive. The restart is
graceful: the agent stops the receiver through its own shutdown path rather than
being killed, because a dongle killed mid-transfer needs a physical replug.

**There is no automatic update path and there should not be one yet.** It is the
same trust root as enrolment, and `contract/enrolment.md` §9.5 is unanswered —
see `DECISIONS.md`.

---

## 15. What is exposed

| Port | Bound to | What |
|---|---|---|
| 8088 | `127.0.0.1` | the setup console. **No authentication** — reach it over an SSH tunnel |
| 22 | everything | SSH. Key-only, please: this box is on the public internet |

The station makes **outbound** connections only, to the broker (6380/TLS) and
the platform API (8000/TLS). Nothing reaches inward: Starlink is CGNAT and the
platform can never initiate a connection to a station.

If the setup console is ever moved off loopback, it belongs on a private setup
network and nowhere else — it has no authentication, and physical presence is
the only control it has (`DECISIONS.md`, open decision 2).

---

## Appendix: everything in one place

```bash
# on the box, as root
/opt/percepta/station                    code (root-owned, not agent-writable)
/opt/percepta/station/.venv/bin/python   the interpreter the service runs
/etc/percepta/gsu.env                    configuration        0640 root:gsu
/etc/percepta/platform-ca.pem            the CA you carried in
/var/lib/percepta-gsu/                   state                0700 gsu
  credential.json                        the station's identity   0600
  platform-ca.pem                        the pinned CA            0600
  devices.json                           what is fitted
  station.db                             events
  recordings/                            audio
/etc/systemd/system/gsu.service          the unit

# commands, all as the gsu user with the env file loaded
python -m gsu preflight --probe          everything that must be true
python -m gsu devices                    intent against fact
python -m gsu whoami                     what this box thinks it is, offline
python -m gsu status                     what the platform thinks of it
python -m gsu bench                      what a tick costs on this hardware
```
