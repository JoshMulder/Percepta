# Decisions and assumptions

Three lists. The first is what `contract/enrolment.md` §9 says needs a human —
no answers invented, only what the station does in the absence of one. The
second is every choice I made that someone should confirm. The third is the
deployment session: **items 21–34 are new, all of them need review, and none of
them has been run on a Raspberry Pi.**

---

## Open decisions, still open

### 1. Compute platform (§9.1)

Now partly answered — a Raspberry Pi 2B, deployed as a systemd service (item 21)
— and that answer has consequences the decision has not caught up with, in
HARDWARE.md:

- **No hardware keystore.** The credential is a 0600 file in a 0700 directory
  (`credentials.py`), which is what §3 allows. The pinned CA is now stored the
  same way, beside it. `CredentialStore` is the seam if a keystore appears.
- **No real-time clock.** §6's failure mode is live on this hardware. The
  station refuses to enrol with an implausible clock, raises a critical health
  condition, and now also reports what is disciplining its clock — which is
  mitigation and visibility, not a fix. **An RTC module would remove the class
  of failure and costs about £4** (HARDWARE.md §4, item 30).
- **Where the setup console is served** is still undecided — see 2. The deployed
  answer is an SSH tunnel to loopback, which is an interim, not a design.

### 2. Who installs, and with what (§9.2)

Unanswered, and it decides two things the station cannot decide for itself:

- **Which interface the console binds to.** It binds `127.0.0.1:8088` by
  default. On real hardware it belongs on the box's own setup network — a
  soft-AP or a dedicated Ethernet port. The Pi 2B has **no onboard Wi-Fi**, so a
  soft-AP means a USB Wi-Fi adapter, which means another device on the contended
  USB bus (HARDWARE.md §3). A dedicated Ethernet port and a laptop avoids that.
- **Whether the console needs authentication.** It has none: physical presence
  on the setup network is the control, which holds only if that network is not
  the site's routable one. If a subcontractor installs, or if the console ends
  up reachable from the site LAN, that assumption fails and the console needs a
  password or a pairing step.

The console is built mobile-first (single column, large touch targets) on the
assumption of a phone. If it is a laptop, that was harmless.

### 3. Token lifetime (§9.3)

Not for the station to choose. Observed: the platform issues 24-hour codes and
90-day credentials, renewable from 45 days. The station renews from
`renew_after`, retries with jittered backoff to a 15-minute cap, and raises a
health condition on the first failure — warning, escalating to critical inside
six hours of expiry. If boxes are shipped and installed a fortnight later,
24 hours is wrong and the station cannot compensate.

### 4. Broker: managed or self-hosted (§9.4)

Still unanswered, and **still open despite TLS landing.** Redis now speaks TLS
with a private CA, which answers "is the transport encrypted" but not "who runs
the broker in production". **MQTT remains unimplemented**: `transport/mqtt.py`
is a stub carrying the requirements — now including that it must use the same
`Trust` object and refuse the same downgrades — rather than a plausible-looking
client that has never connected to anything. The transport interface is the only
place that knows the broker is Redis; swapping it is one class and a URL scheme.

### 5. Software update path (§9.5)

Still nothing built, and deliberately so even now that there is an installer.
`deploy/install.sh` is idempotent and can be re-run to upgrade, but that is a
person with SSH, not an update mechanism: no self-update, no signature
verification, no rollback. An update path is the same trust root as enrolment
and is worse than useless improvised — a box that can be updated by anyone who
can answer a DNS query is a box that can be replaced by them.

**This is now the most expensive of the five to leave open**, because there is
hardware in the field to update. Whatever is chosen needs a signature check
against a key that is not the one enrolment uses, and a way to roll back a bad
build without a site visit.

---

## Assumptions I made — please confirm

### Identity and connection

1. **The broker URL from enrolment may be unroutable.** The platform returns
   `redis://redis:6379/0`, a container-internal name. `GSU_BROKER_URL` overrides
   the address only; username and topics still come from enrolment, because
   those are identity rather than deployment. In production the platform should
   return an address that is routable from a station.
2. **The station authenticates as `gsu:{station_id}` from the first
   connection**, even though Redis' `default` user is still open. Verified: the
   ACL genuinely refuses a publish to another station's channel.
3. **A clock before 2026-01-01 or more than ten years ahead is refused for
   enrolment.** Wide bounds on purpose: this catches a reset clock, not drift.

### Devices

4. **Everything ships simulated, and says so.** Default inventory is all
   simulated devices, each reporting `simulated: true`. Nothing ever silently
   substitutes a simulation for hardware that did not answer.
5. **Drivers written but not exercised against hardware**: the Airmar NMEA
   decoder and the MAVLink/ping RX decoder are complete and unit-tested against
   synthetic and hand-worked data; the serial layer under them (`serialio.py`,
   termios 8N1) has never spoken to a real UART. First thing to check on a real
   box.
6. **Drivers deliberately not written**: RTL-SDR airband, dump1090, the Victron
   charge controller, the GPIO relay, both cameras. They are selectable in the
   registry and report "supported as a selection, no driver in this build". A
   station configured with one of them publishes nothing for that stream rather
   than pretending.
7. **Gust is derived** as the peak of a rolling 10-minute window over 1 Hz wind
   samples. WMO uses a 3-second peak, which 1 Hz data cannot give.
8. **True wind direction needs a mast orientation** — a configured constant,
   because the 110WX has no compass. Default 0°, which is wrong at most sites.
9. **`is_day` is not published by the Airmar path.** It is derivable from
   position and time and would be honest to compute; I left it absent rather
   than deriving a field the instrument does not measure. Say the word and it
   becomes four lines.
10. **SDR tuners are keyed on serial number.** A dongle with no serial
    programmed is flagged as indistinguishable from an identical one.

### Site policy — all defaults, all should be reviewed

11. Proximity alert: within **12 km and below 1500 m**. Battery low at **20%**,
    critical at **10%**. The floodlight is **shed below 12%** state of charge, by
    the station, without waiting for a command.
12. Retention: **24 hours or 200 MB** of audio recordings, **30 days** of
    events. A remote box with a full disk is a site visit.
13. Wind alarm at **45 kt**, unused until there is an event channel to report it
    on (CONTRACT-QUESTIONS item 4).
14. Simulated airband traffic defaults to **"low"** — a transmission every
    70–220 seconds, which is what a rural airband channel is like. `busy`
    exercises the audio path; `off` silences it.

### Behaviour that goes slightly beyond the contract

15. **A `health` telemetry kind is published** every 30 seconds. It is not in
    the schema; the schema promises unknown kinds are dropped and logged, and
    this is the only way to report `config_version`, which `enrolment.md` §7
    requires be in telemetry. Proposed properly in CONTRACT-QUESTIONS item 5.
    It shows up in conformance output as an ignored unknown kind.
16. **Unsourced fields are omitted, not defaulted** — including
    `weather.humidity_pct` on an instrument with no RH module, which the schema
    no longer requires. A stream with **no source at all** is a different
    statement and uses `available: false` on its normal cadence, with a reason
    written for an operator; the structured form of the same fact is in the
    health payload's device inventory. CONTRACT-QUESTIONS items 1 and 2.
17. **`config.set` is handled** with a provisional payload shape, since the
    platform has not shipped it yet.

### Housekeeping

18. State lives in `station/var/` in a checkout, and in `/var/lib/percepta-gsu`
    when deployed (item 22) — credential, pinned CA, device inventory, site
    config, receiver state, event database, recordings.
19. **One runtime dependency: `redis`.** Everything else is standard library,
    including the HTTP client, the console, the serial layer, the TLS trust
    handling and the MAVLink and NMEA decoders. A box in the field should not
    need to install anything.
20. **No git commits.** Work is left uncommitted for review, as instructed.

---

# Deployment session — all of this needs review

Everything below was decided in one session, against a Pi that was not present.
**Nothing here has run on ARMv7, on a real UART, on a real camera or on a real
SDR.** Where something was genuinely verified, it says so and says how; where it
was not, it says that instead. HARDWARE.md §7 is the same register in one table.

## Transport security

### 21. Deployed as a systemd service, not Docker — and here is the argument

The owner asked for an "image". I have taken that to mean *a repeatable way to
get from a blank SD card to a running station*, and delivered it as a systemd
unit plus an installer rather than a container.

**Why not Docker on this box:**

- **Memory.** 1 GB total. `dockerd` plus `containerd` idle at 80–150 MB before
  anything runs, against an agent whose whole job is ~30 MB. That is 10–15% of
  the machine spent on a supervisor that systemd already is.
- **Device access.** The agent needs two USB-UARTs whose names change between
  boots, a USB SDR, and later the CSI camera. Doing that in a container means
  `--privileged` or a growing list of `--device` flags plus udev inside the
  container — which is strictly *less* isolation than the systemd sandbox below,
  achieved with more moving parts.
- **The SD card.** Overlayfs multiplies small writes, and the card is already
  the most likely hardware failure at a remote site.
- **ARMv7 images.** Increasingly an afterthought upstream; base images get
  dropped, and a station that cannot pull an image is a station that cannot be
  fixed.
- **It would prejudge §9.5.** Containers imply a registry and a pull-based
  update path, and the update decision is explicitly unanswered. Choosing the
  mechanism by accident, through packaging, is the wrong way to answer it.

**What the systemd unit buys instead:** `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=strict`, `ProtectHome`, `ProtectClock`, an empty
`CapabilityBoundingSet`, a `@system-service` syscall filter, a dedicated
unprivileged user, and code owned by root that the agent cannot rewrite. That is
a tighter sandbox than a default container, and it is described in one file a
person can read.

**What would change my mind:** a fleet of dozens where image-based rollout and
rollback is the update story, or a decision to run other services on the same
box. Neither is true of one Pi 2B.

**Confidence: high on the reasoning, untested in practice** — the unit parses
cleanly under `systemd-analyze verify`, and has never been started on a Pi.

### 22. TLS is mandatory, pinned to the platform's CA, with no way to turn it off

`contract/enrolment.md` §4 says `ca_pem` is *pinned*. Implemented literally, in
`gsu/tls.py`:

- The CA is persisted at `$GSU_HOME/platform-ca.pem`, **0600**, beside the
  credential — they are one identity.
- Both the broker (`rediss://`) and the API (`https://`) verify against it.
  `ssl_cert_reqs=required`, `ssl_check_hostname=True` and TLS 1.2 minimum are
  passed **explicitly** to redis-py rather than left to its defaults, which have
  differed between versions.
- **There is no mode that disables verification.** `GSU_TLS_TRUST` takes
  `pinned` (default) or `system`, and `system` still verifies — it accepts any
  CA the OS trusts, which is weaker and is reported as a health condition.
- **There is no plaintext fallback.** A pinned station pointed at `redis://` or
  `http://` refuses, raises `uplink.refused` (critical), records an event, logs
  `NOT PUBLISHING`, and shows `REFUSED` on the setup page — while continuing to
  sense, record and alert locally.

**Verified, not asserted.** Against a TLS-only `redis-server` with a private CA
and a per-station ACL: publishes over TLS with the right CA; refuses the wrong
CA and reports it as a TLS failure rather than a dropout; refuses plaintext
before opening a socket; refuses TLS with no CA rather than using the system
store; the ACL still refuses another station's channel. Then end to end against
a fake platform on HTTPS: enrol, persist the CA 0600, publish over `rediss://`,
restart, re-pin from the persisted CA alone, and refuse a plaintext override.
27 checks, all passing. **On x86-64 — not on ARM.**

**Needs review:** whether `GSU_TLS_TRUST=system` should exist at all. I kept it
for a platform that later sits behind a public certificate. It is an escape
hatch, and escape hatches get used.

### 23. Broker URLs must carry no credentials, and the station enforces it

redis-py's `ConnectionPool.from_url` ends with `kwargs.update(url_options)`, so
**the URL overrides the keyword arguments**. A `GSU_BROKER_URL` containing
`user:pass@` would replace this station's identity with whatever it names —
failing confusingly at best and, at worst, publishing as another principal,
which leaves the tenancy model the whole platform rests on.

The transport strips any credentials from the URL, warns, and connects as
`gsu:{station_id}` regardless. URLs are also redacted before they appear on the
console or in health telemetry. Verified: a URL naming the `default` superuser
still connected as the station and was still refused another station's channel.

*(Found by the platform side and passed on; the fix and its test are mine.)*

### 24. The first enrolment call needs a CA installed out of band

A bootstrap problem with no clever answer: the first `POST /api/enrol` happens
before anything has been pinned, and that call carries the enrolment token and
receives the credential. Verifying it against the system trust store would make
the pinning decorative.

So `GSU_CA_FILE` is a file the installer puts on the box, and without it the
station **refuses** to enrol over `https://` and says what to install. The
installer prints the CA's fingerprint and tells the operator to check it against
the platform. That eyeball check is the root of the whole chain.

**Needs review:** this is the one manual verification step in the procedure. If
the CA is emailed to the technician alongside the code, it is not really out of
band, and the pinning is worth less than it looks.

### 25. Precedence: installed CA, then persisted CA, then nothing

An installed `GSU_CA_FILE` wins over the one from enrolment, because it was put
there deliberately by a person. A CA arriving in a response that *differs* from
the stored one is accepted — the response carrying it was itself verified — but
logged as a warning and recorded as an event, because a rotation and somebody
else's certificate look identical from here. A pinned CA that has gone missing
is a refusal, never a fallback.

## Deployment

### 26. Paths, user and ownership

`/opt/percepta/station` (code, **root-owned and not writable by the agent**),
`/etc/percepta/gsu.env` (0640 root:gsu), `/var/lib/percepta-gsu` (0700, state),
user `gsu` (system account, no login, in `dialout`, `video`, `plugdev`).

Code the agent cannot rewrite is deliberate: a compromised agent should not be
able to persist itself by editing the thing systemd restarts.

### 27. The unit does not wait for the network, and restarts for ever

`After=local-fs.target network.target` — ordering only, no `Requires`, and
**not** `network-online.target`. The whole design keeps working with the link
down, so a unit that will not start without one contradicts it. `Restart=always`
with `StartLimitIntervalSec=0`: on a site hours away, the right behaviour for a
crash loop is to keep trying and keep saying so, not to give up after five
attempts and go quiet.

Also **not** `After=time-sync.target`, which would wait for NTP, which waits for
the network. The agent handles a bad clock itself.

`TimeoutStopSec=45s` and `KillSignal=SIGTERM` so the receiver is shut down
through its own path rather than killed — a dongle killed mid-transfer needs a
physical replug (`server/docs/05-radio-integration.md`).

### 28. Two hardening lines that could bite, named here so they are findable

- **`MemoryDenyWriteExecute=yes`.** CPython and the one pure-Python dependency
  do not need writable-executable pages. If a native extension is ever added and
  the service will not start, this is the first line to remove — and the reason
  should be written down when it is.
- **`RestrictAddressFamilies` includes `AF_NETLINK`**, which looks removable and
  is not: glibc's `getaddrinfo()` uses it to enumerate local addresses, so
  dropping it breaks DNS and the station cannot find the broker.

`PrivateDevices` is deliberately *not* set — it would take away the UARTs and
the SDR. A `DeviceAllow` list is used instead.

### 29. Python 3.11 minimum, so Raspberry Pi OS Bookworm or newer

The code uses `datetime.UTC` (3.11+). Bookworm ships 3.11.2; Bullseye ships 3.9
and will not run this. The installer checks and refuses with that explanation
rather than failing at import time. **This is a hard deployment constraint and
it should be confirmed against the actual SD card image before anyone travels.**

## Sensors

### 30. The four real devices are selected; two of them have no driver, and say so

`deploy/devices.pi.json` fits the ping RX Pro (ADS-B), the Airmar 110WX
(weather), the RTL-SDR (airband) and the Pi camera. The last two have **no
driver in this build** and report exactly `not supported by this software
build`; their streams go out as `available: false` with that reason on the
normal cadence. Nothing is stubbed to look like it works. `power` and `light`
have no hardware specified for this site and are left empty, which is a
different statement and reads differently on the console.

Selecting hardware the software cannot drive is deliberate: it lets the platform
and the console see that the device is *fitted* and that what is missing is
software, rather than showing an empty slot that reads as "nothing there".

### 31. Serial ports default to empty, not to `/dev/ttyUSB0`

Two USB-UARTs, enumerating in whatever order the kernel probed them, in an order
that changes between boots. A default of `/dev/ttyUSB0` is a trap: when they
swap, the weather driver reads the ADS-B stream and vice versa, and it presents
as *both* instruments failing, which sends somebody looking for a power fault.

So the port is empty until set, and every failure message names the ports that
are actually present. The setup page offers the detected `/dev/serial/by-id/…`
names in a picker, `gsu devices` and `gsu preflight` list them, and the message
that reaches the platform in `unavailable_reason` contains them too — so the
telemetry reporting the fault also reports the fix.

Baud defaults are now per device: 4800 for NMEA, **57600 for the ping RX Pro**
(it was 4800 for everything, which would have read as a dead receiver — bytes
arriving and no frame ever parsing).

**The serial layer still has never opened a real UART.** Its failure paths are
tested; its success path is not. It is the first thing to check on the box.

### 32. `gsu preflight` exists, and is the commissioning step

One command that checks the clock and what disciplines it, the trust root and
its fingerprint, TLS handshakes to both endpoints (`--probe`, sending no
credential), file permissions on the credential and CA, the serial ports
present, and every device slot. PASS/WARN/FAIL, non-zero exit on any FAIL.

It exists because "how do I tell it is working" deserves an answer that is one
command rather than six, and because the person running it may be about to drive
away.

## Time

### 33. The station reports what keeps its clock; GPS goes in chrony, not here

`clock.discipline()` reports `gps`, `ntp`, `rtc-only`, `none` or `unknown`, plus
whether an RTC exists, in every health frame and on the setup page.
`clock.unsynchronised` is a **warning**, not a refusal — sensing and recording
do not need a correct clock, and enrolling already refuses separately.

**GPS is deliberately not implemented in Python.** The right place is the OS
clock discipline: `gpsd` feeds `chrony` over SHM, and PPS — which is what makes
GPS timing worth having, sub-microsecond against ~100 ms for serial NMEA — is a
kernel device only a disciplining daemon can use properly. A Python loop reading
`$GPRMC` and calling `settimeofday` would be a *worse* clock than the NTP it
replaced and would fight whatever else was disciplining the system.

The drop-in therefore needs no station change at all: fit the receiver, wire
PPS, configure chrony (DEPLOYMENT.md §11), and `discipline()` starts reporting
`gps` because chrony's reference id becomes `PPS`. The installer installs chrony
rather than relying on `systemd-timesyncd` **specifically so that this is a
config file later and not a change of daemon.**

**Needs review:** whether `clock.unsynchronised` should ever block anything. I
say no — a station that stopped sensing because it was unsure of the time would
turn a cosmetic problem into an outage — but it is a judgement.

### 34. Fit a DS3231 RTC (~£4) as the interim, and keep it after GPS arrives

Reasoning in full in HARDWARE.md §4. Briefly: NTP fails in exactly the case that
matters — the box reboots during an outage, cannot reach anything to learn the
time, and therefore cannot authenticate to the platform that would have told it.
The RTC breaks that circle without needing the network or a sky view, and it is
still worth having after GPS because the kernel reads it in the first second of
boot while a cold GPS fix takes minutes, and the agent is trying to renew a
credential in between.
