# Decisions and assumptions

Three lists. The first is what `contract/enrolment.md` §9 says needs a human —
no answers invented, only what the station does in the absence of one. The
second is every choice I made that someone should confirm. The third is the
deployment session: **items 21–36 are new, all of them need review, and none of
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

### 21. Both a systemd unit and a container image — superseded by item 35

This item originally argued for systemd *instead of* a container. The owner
asked about Docker twice, which is a preference and not a question, and the
argument did not earn the right to override it. **Both paths are now built** and
the honest comparison — including where my original reasoning was overstated —
is item 35. This entry is kept because it is what I said at the time.

What the systemd unit buys, which still stands: `NoNewPrivileges`,
`PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `ProtectClock`, an empty
`CapabilityBoundingSet`, a `@system-service` syscall filter, a dedicated
unprivileged user, and code owned by root that the agent cannot rewrite.

**Confidence: untested in practice** — the unit parses cleanly under
`systemd-analyze verify` and has never been started on a Pi.

### 22. TLS is mandatory, pinned, with no way to turn it off

> **Partly superseded by item 36.** The parts about one CA covering both links,
> the file name `platform-ca.pem` and the `GSU_TLS_TRUST` switch are all out of
> date — the broker and the API now have separate trust roots. Everything below
> about *refusing* is unchanged and still holds.

`contract/enrolment.md` §4 says `ca_pem` is *pinned*. Implemented literally, in
`gsu/tls.py`:

- The CA is persisted **0600**, beside the credential — they are one identity.
  (Now `$GSU_HOME/broker-ca.pem`, and broker-only. Item 36.)
- `ssl_cert_reqs=required`, `ssl_check_hostname=True` and TLS 1.2 minimum are
  passed **explicitly** to redis-py rather than left to its defaults, which have
  differed between versions.
- **There is no mode that disables verification.** (The `GSU_TLS_TRUST` switch
  described here has since been removed entirely — item 36.)
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

> **Resolved by item 36.** It should not, and it is gone. Splitting the two
> trust roots removed the only legitimate use it had.

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

### 24. The first enrolment call needs a trustworthy connection

> **Reframed by item 36.** The bootstrap is now solved by the *public* trust
> store in the expected deployment, and only needs a carried CA while the
> platform serves its own certificate.

The first `POST /api/enrol` carries the enrolment token and receives the
credential, over a connection that must already be trustworthy. Two ways to get
one, and the second is much better:

1. **A CA carried to the box** (`GSU_API_CA_FILE`), fingerprint checked by eye
   against a channel that did not deliver the file. Necessary today. The one
   manual verification step in the whole procedure — and if the CA is emailed to
   the technician alongside the code, it is not really out of band and is worth
   less than it looks.
2. **A public certificate on a real domain**, verified against the system trust
   store. The bootstrap was then done years in advance by the OS vendor, and
   there is nothing to carry, check or get wrong. This is where the platform is
   going, and it is a good reason to go there.

The broker's CA has no bootstrap problem either way: it arrives inside the
enrolment response, which was itself verified.

### 25. Broker CA precedence: installed, then persisted, then nothing

An installed `GSU_CA_FILE` wins over the one from enrolment, because it was put
there deliberately by a person. A CA arriving in a response that *differs* from
the stored one is accepted — the response carrying it was itself verified — but
logged as a warning and recorded as an event, because a rotation and somebody
else's certificate look identical from here. A pinned CA that has gone missing
is a refusal, never a fallback.

Note what this does *not* do: the CA from an enrolment response never becomes
the API's trust root. One CA arriving over a channel must not silently become
the root for that same channel.

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

---

# Second pass — the container path, and splitting the trust roots

## 35. Docker: it works, it is built, and here are the real costs

**Supersedes item 21.** I argued against a container and I was asked twice for
one, which means the argument was not carrying its weight. Both paths now exist:
`deploy/Dockerfile` and `deploy/docker-compose.yml` alongside the systemd unit.
Neither is a fallback for the other, and DEPLOYMENT.md §16 compares them where
somebody deploying will actually read it.

**Docker does work on a Pi 2B.** Here is what I claimed before, and what is
actually true:

| I said | Actually |
|---|---|
| "ARMv7 images are increasingly an afterthought" | **Wrong enough to matter.** `python:3.11-slim-bookworm` publishes `linux/arm/v7` today. Verified at the registry: manifest `sha256:d2091b0d…`, 39.9 MB compressed across 4 layers. The image is pinned by its multi-arch index digest so `--platform` still resolves |
| "`dockerd` + `containerd` idle at 80–150 MB" | **Directionally right, imprecisely stated.** 50–100 MB is the better range. I could not measure it here — the Docker daemon is not reachable from this machine — so it stays an estimate and is labelled as one |
| "containers mean `--privileged` or a growing list of `--device` flags" | **Half right, and the wrong half was doing the work.** No `privileged: true` is needed and none is used. The device list *is* the cost, but the specific problem is not privilege — see below |
| "overlayfs multiplies small writes" | True but minor next to the real SD-card risk, which is **unrotated container logs**. Docker's `json-file` driver has no rotation by default; an unrotated log will eventually fill the card and take the site down. `max-size: 10m, max-file: 3` is configured |
| "it would prejudge §9.5" | **Still true, and still worth saying** — but it is an argument for not *deciding* the update path, not for refusing to build the packaging. Containers make updates and rollback genuinely easier, which is an argument *for* them that I under-weighted |

**Where the container path is actually weaker, which is device handling:**

- **A mapped device that does not exist prevents the container from starting.**
  On the systemd path a missing UART is a health condition the station reports
  and recovers from on its own when somebody plugs it in. In a container it is a
  box that will not come up. For a station with two USB-UARTs that are sometimes
  unplugged, that is a real regression in unattended behaviour.
- **A device replugged while running is invisible** until the container is
  recreated, because the device cgroup is fixed at start. The agent's 30-second
  rediscovery loop cannot help it.
- **`/dev/serial/by-id` needs both the symlink directory and the target nodes**
  mapped — the symlinks are relative and resolve inside the container. It works;
  it is two coupled bits of configuration instead of none.
- **The SDR cannot be mapped by node.** libusb needs
  `/dev/bus/usb/<bus>/<device>` and the device number changes on every
  re-enumeration, so today's node stops working after a replug. The workable
  answer is to map the whole USB bus with a cgroup rule for major 189, which is
  broader than one dongle. Written into the compose file, commented, because
  there is no SDR driver in this build. **This is a genuine finding rather than
  a failure to configure it properly.**

**Recommendation: systemd for this one station, narrowly.** The deciding row is
unattended device behaviour, not memory or ideology. **If the fleet grows or the
update path lands on image pulls, switch** — the container is built, the sandbox
is comparable (`read_only`, `cap_drop: ALL`, `no-new-privileges`), and rollback
is better.

**What I could not test: all of it.** `docker info` returns a permission error
on this machine, so the image has never been built and the container has never
been started, on any architecture. The compose file validates against the schema
(`docker compose config`); the ARMv7 base image is verified at the registry.
Everything about the runtime behaviour of the device mappings is reasoned from
documentation. Expect an hour on those specifically.

## 36. Two trust roots: the broker is pinned, the API is not by default

**This corrects a real design error of mine.** I used `broker.ca_pem` to verify
both the broker and the platform API. The field is named for the broker because
that is what it is, and the platform is moving its API behind a TLS-terminating
reverse proxy with a public certificate — at which point pinning the API to the
broker's private CA would have failed every station at once, with a certificate
error and no obvious cause.

| | Verified against | Configured by |
|---|---|---|
| **Broker** `rediss://` | a pinned private CA, **always** | nothing: `broker.ca_pem` from enrolment, persisted 0600 at `$GSU_HOME/broker-ca.pem` |
| **Platform API** `https://` | the **system CA bundle** by default | `GSU_API_CA_FILE` to pin instead |

Three consequences worth stating:

- **The API is not weakened by this.** A public certificate for a real domain
  verified against a well-audited root store is what that store is for. What
  would weaken it is pinning to a CA that is not the one answering, which is an
  outage wearing a security costume.
- **Pinning the API stays possible and is currently correct**, because the
  platform still serves its own certificate on `https://192.168.2.49:8000`. The
  migration is one commented line in the environment file, in both directions.
- **`GSU_TLS_TRUST` is gone.** It was a global pinned/system switch, and I
  flagged it for review last night as an escape hatch that would get used.
  Splitting the roots removed the only legitimate reason for it — a
  publicly-signed platform — so the switch went with it. The broker now has no
  system-trust option at all, which is the right shape: it is a private service
  with a private CA, and "any CA the OS trusts" is not a description of it.

**Also deliberate: a pinning request that cannot be honoured is a refusal, not a
downgrade.** `GSU_API_CA_FILE` pointing at an unreadable file raises a critical
health condition and refuses the connection. It does *not* quietly fall back to
the system bundle — the operator asked for something specific, and silently
doing something weaker than they asked for is the exact failure this module
exists to prevent.

**Verified** against a live self-signed platform and a TLS-only broker: the two
roots resolve independently; the broker still pins from the enrolment response
and survives a restart; the API is refused when unpinned against a self-signed
certificate and accepted when pinned; the refusal messages name the right
environment variable for each link. 14 end-to-end checks plus 26 unit tests.
**On x86-64 — not on ARM.**

## 37. The DEMO badge: no change needed here, and worth recording why

The platform showed a bench station as connected with no DEMO badge while every
device it reported was `simulated-*`. The station side was correct — `health`
carries `devices[].simulated` per slot and always has — and the console was
reading the platform's own record instead. Fixed on the platform side.

Recorded because it is the same failure this station keeps guarding against,
seen from the other end: **the honest signal existed and the consumer did not
read it.** That is an argument for `CONTRACT-QUESTIONS.md` item 5 — the `health`
kind is still not in the platform's `KNOWN_KINDS`, so the structured device
inventory the station publishes every 30 seconds is dropped on arrival. This is
the second time that data would have answered a question somebody had.
