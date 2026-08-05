# Decisions and assumptions

Three lists. The first is what `contract/enrolment.md` §9 says needs a human —
no answers invented, only what the station does in the absence of one. The
second is every choice I made that someone should confirm. The third is the
deployment sessions: **items 21–40 are new, all of them need review, and none
of them has been run on a Raspberry Pi.**

---

## Open decisions, still open

### 1. Compute platform (§9.1)

Now partly answered — a Raspberry Pi 2B, deployed as a container (item 35c)
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
  **Still open.**
- ~~**Whether the console needs authentication.**~~ **Answered — it does, and it
  has it.** Item 41. The old answer, "physical presence on the setup network is
  the control", was only ever true if that network was not the site's routable
  one, and nothing in the deployment guaranteed that. A per-box password is now
  required before the page will bind anywhere but loopback.

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

**Answered on 2026-08-01 — see item 47.** Production is a WebSocket relay on
the platform's own 443 (`transport/relay.py`), because the port is the whole
problem: 6380 and 8883 are both shut wherever a reverse proxy is. MQTT was
priced and dropped, and `transport/mqtt.py` is deleted. What follows is the
original entry, kept because the reasoning that led here is worth reading.

Redis now speaks TLS with a private CA, which answers "is the transport
encrypted" but not "who runs the broker in production". MQTT remains
unimplemented: `transport/mqtt.py` is a stub carrying the requirements — now
including that it must use the same `Trust` object and refuse the same
downgrades — rather than a plausible-looking
client that has never connected to anything. The transport interface is the only
place that knows the broker is Redis; swapping it is one class and a URL scheme.

### 5. Software update path (§9.5) — **partly answered, and the rest is now urgent**

**Carried forward to item 48**, which re-establishes the updater for the
container era (item 35 dropped it), decides signing — the headline open item
below — and adds a platform-commanded trigger. Governance (who publishes, how a
release is approved, staging) is recorded there as still open.

**A mechanism now exists** (item 39): pull on a jittered timer, apply, prove the
new image publishes, and roll back to the image already on disk if it does not.
That was built because the owner's constraint — *"once these stations are
installed they are going to be difficult to physically access"* — makes a bad
update the most expensive routine failure there is.

**What is still open, and it is the part that needs a human:**

- **Signing.** A digest pin means the image cannot change under you; it does not
  prove who built it. Anyone who can write to the registry, or substitute for
  it, can publish a station update. Proper signing needs a key that is *not* the
  enrolment trust root — a compromise of one should not be a compromise of both
  — and neither key management nor a signing process exists.
- **Who publishes, and how a release is approved.** Nothing technical stops a
  half-finished build being pushed to the tag every station follows.
- **Staging policy.** The timer's 2-hour jitter staggers a fleet by accident.
  Deliberately updating one station first, watching it, then the rest, is a
  process nobody owns.

So the answer to "can a station be fixed remotely" is now *yes*, and the answer
to "can a station be **trusted** to update itself" is still *not yet*. The
mechanism is safe against a bad build; it is not safe against a hostile one.

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

15. **A `health` telemetry kind is published** every 30 seconds. Proposed from
    this side and **since adopted**: it is in the contract schema, in the
    platform's `KNOWN_KINDS`, and `devices[].simulated` drives the console's
    DEMO badge. It carries `config_version`, which `enrolment.md` §7 requires be
    in telemetry and which no other field holds. CONTRACT-QUESTIONS item 5 has
    the two schema violations that adoption then exposed, and how they escaped.
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

### 21. systemd rather than a container — right conclusion, partly wrong reasons

This item argued for systemd instead of a container. **The conclusion is now the
decision** (item 35, ruled by the owner) — but not on the strength of what is
written here: several of my arguments did not survive checking, and item 35
keeps the table of which ones. Both paths were built before the decision was
made, which is why there was something concrete to decide about.

Kept because it is what I said at the time.

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

## 35. Docker — the full history, in three parts

This decision reversed twice. **The trail is kept deliberately**: 35a is my
original argument, 35b is the owner rejecting containers on the strength of my
own compose file, and 35c is the reversal once the premise underneath 35b turned
out not to hold. Reading them in order is the only way to see why the final
answer is right, and what a tidy single conclusion would have hidden.

**Current decision: 35c. Docker is the deployment path.**

---

## 35a. Docker: built, evaluated, and rejected by the owner — *superseded*

**The decision at the time: systemd is the deployment path. Docker is not
suitable for an unattended remote station.** The owner's words:

> so what you're saying is that using docker on the station is going to
> complicate the setup, and possibly stop the station working - requiring manual
> intervention - if/when it restarts? that's not acceptable

That is the correct reading of my own compose file, which says in capitals that
Docker refuses to start a container whose mapped device does not exist. For a
box hours from anyone, a reboot after a USB adapter fails to enumerate takes
down **the entire station** rather than one sensor, and recovery needs a person
on site. It is the exact failure class this project exists to avoid, and it
outweighs everything in the table below.

`deploy/Dockerfile` and `deploy/docker-compose.yml` were **kept** — they may suit
a co-located station with someone on site.

### The mitigation that exists, and why it was not taken

*(This is the paragraph that 35c overturns. Left exactly as written.)*

Recorded so nobody later finds it and assumes it was missed. The won't-start
failure **is** avoidable: bind-mount `/dev/serial` and `/dev/bus/usb` as
*directories* instead of mapping individual nodes. Nothing is then missing at
start, and hot-replug works — a UART plugged in later becomes visible, and the
SDR survives re-enumeration.

The cost is that the container gets **every USB device on the box**, present and
future, rather than the three it needs. That surrenders most of the isolation
which was the reason to containerise; what is left is a filesystem boundary the
systemd unit already provides with `ProtectSystem=strict`, plus a supervisor
that systemd already is.

So the honest choice was between a container that can strand the site and a
container that isolates almost nothing. Neither beats the unit file. The option
was understood and rejected, not overlooked.

---

## 35b. What I got wrong along the way

*(Part of the 35a record: my original arguments, checked.)*

**This table stays.** My original argument against Docker (item 21) was built
partly on claims that did not survive checking, and the decision above was made
on the one that did — unattended restart behaviour — rather than on my errors.

**Docker does work on a Pi 2B.** Here is what I claimed, and what is actually
true:

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

The first of those four is the one 35a turned on. I originally called it a
"recommendation, narrowly" and weighed it against memory, image size and update
ergonomics as though those were comparable quantities. They are not: the others
cost effort, and that one costs a site visit. The owner read the same list and
said so plainly, which was the right call **on the information available**.

---

## 35c. Reversed: Docker is the deployment path — **CURRENT DECISION**

The owner:

> is docker the right option from a portability standpoint though? I'm not
> overly concerned about the isolation factor, this will be the only thing
> running on the station. this needs to be easy to stand up, easy to maintain,
> and easy to debug. once these stations are installed they are going to be
> difficult to physically access.

**That removes the premise 35b rested on.** The rejection was not really about
containers — it was about the won't-start failure, and the reason I did not
simply fix that failure was the paragraph above: the fix costs isolation. If
isolation has no value here, the fix is free, and the argument collapses.

I should have surfaced that conditional myself. I wrote "the choice is between a
container that can strand the site and a container that isolates almost
nothing" and treated the second as obviously unacceptable, without ever asking
whether the isolation being protected was worth anything on a single-purpose
box. It was not. **The question "what is this isolation actually buying us
here?" was mine to ask and I did not ask it.**

### What changed in the files

- **`devices:` is gone.** The container gets `/dev` bind-mounted wholesale plus
  `device_cgroup_rules` for every major it might use (188 USB-serial, 166
  CDC-ACM, 204 on-chip UART, 189 USB-raw, 81 V4L2, 249 PPS). A missing sensor
  can no longer stop the station, and a replugged one is picked up by the
  agent's existing 30-second rediscovery with nobody touching anything.
- **It has to be all of `/dev`**, not just `/dev/serial` and `/dev/bus/usb`:
  `by-id` entries are *relative* symlinks (`../../ttyUSB0`) that resolve against
  the container's own `/dev`. Mounting the symlink directory without the nodes
  gives you stable names pointing at nothing. This is the sort of thing that
  looks like a typo at 2am, so it is commented in the compose file.
- **`privileged: true` is still not used** — not for isolation, but because it
  also changes cgroup, AppArmor and `/sys` handling, and that is a blunter tool
  and one more thing to reason about when something misbehaves.
- **Cheap hardening is kept** (`read_only`, `cap_drop: ALL`,
  `no-new-privileges`) because it costs nothing operationally. It is not
  protecting anything anyone is worried about; it is just tidy.

### Why containers actually win here

Not memory, not packaging elegance, not isolation. **The update story**, which
is the only thing on the list that speaks to *"difficult to physically
access"*: an update is atomic, and a rollback is a retag of an image already on
the disk — no download, over a link that may be exactly why you are rolling
back. Item 39 is that mechanism, and it is the deliverable that makes this
decision worth having rather than a coin flip.

### Accepted costs, written down rather than apologised for

- The container can reach **every device on the box**, including ones fitted
  later. Accepted: nothing else runs here.
- **50–100 MB** of RAM for the daemon, of 1 GB. Estimated, not measured.
- Container logs need rotation configured or they fill the SD card; journald
  would have given that free. Configured at 10 MB × 3.
- One more moving part to understand when debugging — mitigated by making every
  `gsu` subcommand work identically through `docker compose run --rm`.

**Verified: none of it.** No Docker daemon on this machine. The compose file
validates against the schema and that is all.

**What I could not test: all of it.** `docker info` returns a permission error
on this machine, so the image has never been built and the container has never
been started, on any architecture. The compose file validates against the schema
(`docker compose config`); the ARMv7 base image is verified at the registry.
Everything about the runtime behaviour of the device mappings is reasoned from
documentation. Expect an hour on those specifically.

**Update, 2026-07-29, from the first real Pi:** the paragraphs above are now
history rather than status. The image built and the container ran on a Pi 2B
(Trixie, armhf); the device mappings behaved as reasoned; the camera overlay's
constraints were each necessary and together sufficient for the sensor. Two
things the desk got wrong are fixed and pinned by tests: the installer's
floating service uid against the image's pinned 10001 (one uid on both paths
now — `deploy/install.sh`), and the compose file's `GSU_CA_FILE` pointing at a
read-only path that contradicted enrolment-delivered trust. And one honest
limit: `rpicam-vid` inside the Debian-based camera image takes a bus error on
the Raspbian host, so camera stations run systemd — DEPLOYMENT.md §3.

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
read it.**

## 38. I asserted a platform behaviour twice without checking the platform

I wrote, in two consecutive reports, that `health` was not in the platform's
`KNOWN_KINDS` and that the device inventory was being dropped on arrival. **Both
were false.** `health` has been in `KNOWN_KINDS`, in the contract schema, and
rendered by the console since before either report.

How I got there: I saw no health frames on the fan-out, at a moment when the
only station publishing any was a bench station I had myself just stopped while
tearing down a test lab. I inferred a consumer-side gap from an absence of data
I had caused. `server/app/backend/services/station_ingest.py` is in this
repository, I am permitted to read it, and I did not.

It is precisely the reasoning this station is built to refuse — an empty ADS-B
frame and a dead receiver are indistinguishable unless somebody says which it
is — applied in the wrong direction by the thing that keeps arguing for it.

**Three things changed as a result**, beyond correcting the text:

- **`health` is now schema-validated in the station's own tests.** It had been
  left out of `test_telemetry_matches_the_schema` from when it was an unknown
  kind. Adding it found two real violations of mine within seconds — a `status`
  in the wrong vocabulary and a null `expires_at`. Both are fixed;
  CONTRACT-QUESTIONS item 5 has the detail and the reason conformance never
  caught them.
- **Do not tear down the bench station.** It is the only real station the
  console has and the owner looks at it. Stopping it at the end of a session
  removes the platform's only live data and, as here, invites conclusions drawn
  from the silence.
- **Conformance is 20 checks in a normal window, not 21.** The health schema
  check appears only when a frame lands inside the sample, which at 30-second
  cadence is intermittent. "All checks passed" is the thing to read, not a
  count I quoted as though it were fixed.

## 39. The update mechanism: pull on a jittered timer, gate on publishing, roll back

Built because item 35c made it the reason to prefer containers, and because the
owner's constraint makes it the highest-value thing on the list. `deploy/`:
`gsu-update.sh`, `gsu-update.service`, `gsu-update.timer`.

**Delivery is a pull, because nothing can reach inward.** Starlink is CGNAT and
the platform can never initiate a connection to a station
(`contract/enrolment.md` §1). So the station asks every 6 hours **plus up to
2 hours of random delay**, which is the line that matters most in the whole
timer: without jitter, every station in a fleet checks in the same minute and a
bad image takes all of them out together. Staggered, the first station fails
long before the last one has looked.

Not at boot, either — `OnBootSec=30min`. A box in a boot loop must not pull a
fresh image on every cycle, and a station that has just come up should prove
itself on what it has before being handed something new.

**The gate is the design, and it insists on publishing.** Within 180 seconds the
new container must be running, answer its own console, report itself enrolled,
report the uplink up, **and increase its published-frame counter**. That last
condition is the one that earns its keep: a container can start, log cheerfully
and publish nothing, and that failure is invisible to a "did it start?" check
and indistinguishable from a healthy station until somebody looks days later.

It reuses the console's `/status.json` rather than inventing a health endpoint —
the station already reports exactly these facts for the local console, and a
second mechanism would be a second thing to keep true.

**Rollback is a retag, and the rollback is itself gated.** The previous image is
already on disk, so recovery needs no network — which matters, because a broken
uplink is one of the reasons an update might fail. After rolling back it runs
the same gate again, so "the rollback worked" is a fact rather than an
assumption; and if the *old* image also fails, it says so specifically, because
that is not an update fault and sending someone after the update wastes the trip.

**A rejected digest is recorded and not retried.** Otherwise a bad image is
re-pulled every 6 hours for ever, spending metered bandwidth and flapping the
station in and out of service each time. `--force` overrides, once somebody
knows why.

**A half-completed pull is a non-event.** `docker pull` is atomic at the image
level — layers are content-addressed and verified as they arrive, and the local
reference only moves once all of them are present — so a dropped link cannot
produce a half-built image. The script treats a failed pull as normal, changes
nothing and exits 0. *Documented Docker behaviour; not verified here.*

**The updater runs as root on the host, not in the container.** A container that
can reach `/var/run/docker.sock` can replace itself with anything, which would
make the gate decorative. It also means a container that cannot start can still
be rolled back by something that is still running.

**Verified:** the decision logic, against a stubbed Docker and a real HTTP
console — accept a good image; roll back one that starts but never publishes;
verify the rollback; refuse to retry a rejected digest; survive a failed pull
without touching the running container; no-op on an unchanged digest; refuse to
roll back when there is no previous image. **21 scenarios, all passing.**

**Not verified:** it has never driven a real container, because there is no
Docker daemon on this machine. The stub exercises the branching, not Docker's
actual behaviour. Run it by hand with `--status` and then once for real on the
first box before letting the timer near it.

**Needs review:** the 180-second gate. Long enough for an enrolled station on a
working link; possibly not for a site whose uplink takes minutes to come back
after a restart, and a gate that is too short rolls back good updates. It is one
environment variable, and the first real site should set it from observation.

## 40. Update bandwidth, measured

The numbers that make pull-on-a-timer defensible on a metered link. Taken
against the real registry and the real package, not estimated.

| | |
|---|---|
| A check that finds nothing new | **~6 KB** — an auth token (5.4 KB) plus a manifest HEAD (0.6 KB of headers, no body) |
| Four checks a day | ~24 KB |
| A **code-only** update | **~91 KB** — one layer |
| The docs layer, when only docs changed | ~40 KB |
| The base image, first install only | 39.9 MB compressed, 4 layers |
| This station's telemetry, for scale | ~113 MB/day (10.7 kbit/s, measured earlier) |

**The update check is 0.02% of the telemetry budget.** That settles it: polling
four times a day costs nothing worth discussing, and there is no case for a
push-based trigger on bandwidth grounds. (There is a case on *latency* grounds —
up to ~8 hours from release to a station having it — which is
`CONTRACT-QUESTIONS.md` item 11 and is a contract change, not something to
invent.)

**The 91 KB depends on layer order**, so the Dockerfile now copies the docs
*before* the code, leaving `COPY gsu/` as the last layer. A code change then
invalidates nothing above it and ships one small layer. Keep it that way: moving
a `COPY` line above it would quietly turn every update into a bigger download,
and nobody would notice until somebody looked at a bill.

Method, so it can be re-taken: `tar --exclude=__pycache__ -czf - gsu/ | wc -c`
for the layer (a layer is a gzipped tar, so this is within a few percent), and
`curl` against `registry-1.docker.io` with `-w '%{size_download}'` for the
manifest and token. Both are in the session transcript rather than a script,
which is a small gap — a `make measure-update` target would be better.

## 41. The setup GUI is authenticated, windowed, and off by default

**All of this needs review, and none of it has been exercised on a Pi.**

The owner's requirement: *"the station should come up and host a web gui to
perform the configuration. input code, select sensor types etc. the server
address should be hardcoded in the .env as there will only ever be one."*

The page already existed (`gsu/console.py`, contract/enrolment.md §5 and §7) and
already did both jobs — code entry and per-slot device selection driven off
`devices/registry.py`, so there is still exactly one supported-device list. What
did not exist was any way to reach it that was both usable by an installer with
a phone and safe on a box with a public address. It bound loopback only, had no
authentication, ran for as long as the agent did, had no CSRF, echoed a stored
camera password back into the HTML, and read `Content-Length` bytes off the wire
without a bound. Those are addressed. `gsu/setup_access.py` carries the full
reasoning; the decisions, and what would change them:

**a. It binds loopback by default and always keeps loopback.** Unchanged, and
deliberately: the SSH-tunnel path documented in DEPLOYMENT.md §7 still works,
still needs no password — reaching loopback already required SSH, and a second
secret in front of the first protects nothing — and `deploy/gsu-update.sh`
keeps its `/status.json` health gate. The window does not apply to loopback.

**b. A per-box password, or no LAN listener exists.** Not a check inside the
handler: `Console._target_host()` cannot return a non-loopback address unless a
password is configured, so a station nobody provisioned with one binds loopback
and raises a health condition saying why. This is the answer to "the default
must be safe when the box is on a public address" — the unsafe configuration is
unreachable rather than discouraged.

The secret is `GSU_SETUP_PASSWORD_HASH` (pbkdf2-sha256, 120 000 rounds, written
by `python -m gsu setup-password`), with a plain `GSU_SETUP_PASSWORD` accepted
for a bench. It is per-box and written on the box, like a router's. **It is not
the enrolment code**: the code is single-use, issued by the platform, and cannot
be verified offline — which is exactly when an installer needs to get in.

**Needs a human:** who sets that password and where it is recorded. An image
that ships one password for a fleet is one compromise away from every station,
and this decision belongs with §9.2 (who installs) rather than here.

**c. Private source addresses only.** Written out by hand rather than using
`ipaddress.is_private`, because that predicate counts 100.64.0.0/10 as private
and on a Starlink site carrier-grade NAT is the carrier's shared network, not
this site's LAN. Using the stdlib answer would have admitted every other
subscriber behind the same pool.

**This control does not survive Docker**, and that is written into
DEPLOYMENT.md §15 rather than left as a surprise: behind the bridge every
request appears to come from 172.17.0.1, which is inside 172.16/12. On the
container path the password and the window are the only two controls left.
Another reason the systemd path is the right one for a station that serves this
page — it was already the recommendation for camera-equipped boxes.

**d. It is not a permanent service.** Once the station is enrolled, thirty
minutes after the last authenticated action the LAN socket is *closed* and
rebound to loopback. Closed rather than answering 403, because a port that
answers is a port somebody enumerates. Sessions are dropped with it, so a laptop
left on the bench does not walk back in.

While the station is **unenrolled** the window does not run down. An installer
mid-job must not be locked out, and an unenrolled station is inert: there is
nothing on it worth reaching and an attacker who could reach it would still need
a code the platform issued.

Reopening is deliberate — reboot the station, or `touch $GSU_HOME/setup-open`
with a shell on the box. Both need real access to the site or to SSH. Note the
consequence: **a valid session cookie cannot reopen a closed window**, which is
tested, because a cookie that could would be the back door in a different shape.

**Needs review:** thirty minutes. Long enough for six slots and a code, short
enough that a forgotten install does not leave a page up for a week. It is one
environment variable and the first real install should set it from observation.

**e. The platform address is read-only on the page.** As asked. It is rendered
so an installer can confirm the box points at the right platform before they
leave — that is worth doing, because a wrong address presents as a station with
no signal — but there is no control that can change it. `GSU_PLATFORM_URL` and
`GSU_BROKER_URL` stay in the environment file.

**f. The rest, briefly.** CSRF token bound to the session cookie on every
mutating POST, plus an `Origin` check; `Host` must be an IP literal or a
`.local` name, which is what defeats DNS rebinding — the attack that otherwise
lets a public web page drive this form from inside a technician's browser;
request bodies bounded at 64 KB before they are read, because `Content-Length`
is attacker-controlled and this box has 1 GB of RAM; per-peer lockout after five
failed passwords; `no-store` and a CSP allowing no framing and no script beyond
the one per-response nonce'd block (which the Devices tab now needs to configure
a device — that one control does not degrade without it, and says so in a
`<noscript>`); and the stored camera password is never rendered back — the page
shows *that* one is stored, and a blank field means "unchanged".

**g. What is not solved: the setup page is plain HTTP.** Anyone already on the
setup network can read the password off the wire. A certificate cannot be issued
for a DHCP address, and a self-signed one that an installer click-throughs is
authentication theatre rather than encryption anybody verified. The controls
that bound this are the short window, the per-box password and the expectation
that the setup network is a cable or a dedicated AP. **If this page ever has to
live on a shared site LAN, this is the assumption to revisit first** — and the
answer is probably a pinned certificate provisioned with the image, which is a
real piece of work and was not attempted here.

## 42. A pasted RTSP URL gives up its credentials rather than being refused or stored

Camera vendors hand installers one line: `rtsp://user:pass@host/path`. Three
possible fates for it in the camera form, two of them wrong:

- **Store it as typed** — the address is a text field this page renders back
  on every visit, so the password would be echoed into HTML forever. That is
  the exact leak the password field's stored-never-rendered rule exists to
  prevent, arriving through a different door.
- **Refuse it** (what the driver does, and keeps doing) — correct at the
  driver boundary, where there must be exactly one stored copy of the secret,
  but at the form it means retyping a password on a phone on a roof.
- **Split it** — what the form now does (`console._strip_url_credentials`,
  `rtsp.split_credentials`): the address is stored without its userinfo, the
  credentials move into the username/password parameters, and the save
  message says so. Values typed into the separate fields on the same save win
  over URL-embedded ones — the separate field is the more deliberate act —
  and a URL-borne password replaces a stored one, because a fresh paste is
  what the installer currently believes.

The driver's refusal stays: `python -m gsu` and hand-edited inventory files do
not pass through the form, and two copies of a secret is still one too many.

## 43. The camera preview never captures; it serves the publisher's newest frame

The Devices page's camera tab shows a picture (`/frame.jpg`, click to expand
via a checkbox — no script needed) instead of capture statistics. The endpoint
has **no capture path at all**: it reads the frame the video publisher cached,
with its age in `X-Frame-Age` and beside the image. That is what makes it
safe against the live stream holding an exclusive sensor — a page poll cannot
contend for hardware it never touches; the cached frame just ages, and the
age says so. Same gate as every page, `no-store` like every response.

Consequence: the publisher now runs its capture loop before enrolment too
(publishing still requires an identity — nothing changed on the wire). An
unenrolled box is exactly where an installer needs to see what the camera
sees, and the previous arrangement only started capturing after attach.

## 44. Floodlight current sense: measured amps against commanded state, two volumes

The light devices grew `sense_source`, `sense_threshold_a` (amps at or above
which the lamp counts as drawing) and `state_source` (`relay`, the default and
today's behaviour, or `current` — per light, what `light.on` reports). The
only source honestly offerable today is a sense element on the light feed
read through an ADC; the power monitor is deliberately not offered because it
reports the whole system load, and inferring one lamp from a total that also
moves with everything else is a guess wearing a number. No ADC driver exists
yet — the simulated light carries a simulated sensor (same policy as every
other slot) so the model and both fault paths are real and exercised.

The faults are deliberately at different volumes: commanded on with no draw
is `light.no_draw`, a **warning** (lamp, fuse, wiring — a dark site, bad but
not compounding); commanded off and still drawing is `light.stuck_on`,
**critical** (a welded relay burning the battery at an unattended site).
Judged only after 3 s in the same commanded state, so a moving contactor and
a striking lamp are never faults. The measured amps stay off the telemetry
wire — `$defs/light` carries no such field and this station does not invent
schema; CONTRACT-QUESTIONS.md item 15 proposes `measured_a` and
`state_source`. Until adopted the amps travel in the device detail of the
health frame, the light tab's datastream line, and the local event log.

## 45. One owner for the sensor, stated and enforced — and one reader removed

The camera wedge outlived four fixes. Each was correct about the case it named:
a lock over the driver's open/capture/close; a relinquishing `close()` before
the stream starts; a terminal `retire()` so a replaced driver never reopens; a
2.5-second drain to outlast an in-flight capture. The Pi 2B still came up with
`Camera in Acquired state trying acquire()`, a 38-second stream delivering zero
frames, and no recovery short of a reboot.

The reason they could not converge is that **none of them was a statement of
who owns the sensor.** They were an accumulating set of places that tried to be
polite about a shared device, and politeness has no closure property: every fix
narrowed one window and left the next one to be discovered on hardware.

Three changes, in the order they matter.

**The snapshot channel is gone, and that is the fix rather than a
simplification made alongside it.** Two readers of one sensor was the whole
source of contention; removing one deletes the class of bug instead of
narrowing it. It also removes a diagnostic ambiguity the owner named directly —
*"so i can tell what is camera not working rather than actually just
snapshots"* — because a black console had two indistinguishable causes and now
has one. What the platform stops receiving is written up in
CONTRACT-QUESTIONS.md item 17. The setup page keeps its preview, sourced on
demand: `/status.json` is the signal that somebody is looking, and a station
with nobody on the setup page opens its camera **exactly never**, which leaves
the live stream as the sole consumer on an unattended box.

**There is no libcamera inside this process any more.** `picamera2` existed for
one reason — a subprocess per frame could not sustain 2 fps on a Pi 2B — and
that reason left with the channel. It was also the only thing that could
produce the reported error: `Camera in Acquired state trying acquire()` comes
from `libcamera::Camera::acquire`, and only a process that already holds the
camera in its own `CameraManager` can reach it. The leak had a specific,
reachable path that no amount of `close()`/`retire()` discipline could cover:
`Picamera2()` acquires in its constructor, and a `configure()` or `start()`
that raised afterwards dropped the half-built object with the acquisition still
in it and `self._camera` still `None` — nothing to close, nothing referencing
it, unrecoverable for the life of the run. Worse, the driver's own handler read
that *permanent* failure as "the camera is merely busy" and retried it every
five seconds for ever. Deleting the backend makes the error unreachable by
construction; a subprocess per frame is entirely affordable at one frame when a
human looks.

**Ownership is a lease with a token** (`gsu/camera/ownership.py`). Named, so
telemetry and the setup page can say *who* has the camera — `video.sensor` in
the health frame, which is the question all four previous fixes were circling
and none could answer. Token-based, not a flag, because the failure that
started this was a driver instance nobody referenced any more still acting on a
sensor its replacement had been given: under a boolean, that instance's release
frees its successor's hold and the two run concurrently, which is the bug
wearing the fix as a disguise. A stale token's release is refused and logged.
The 2.5-second drain is gone with it — waiting on the lease waits exactly as
long as the holder takes and no time at all in the normal case.

Two holes the lease is structurally unable to close, both closed elsewhere and
both worth naming because they are the ones that survive a service restart:

- **A second process.** `gsu camera` or `gsu stream` while the service is up is
  two programs opening one ribbon with nothing in between. The lease is
  in-process state and cannot see it. Those commands now take the station's
  file lock and refuse with the fix in the message.
- **An orphaned encoder.** `_pump` respawns `rpicam-vid` itself after a lost
  acquisition race, and there was a window between the retry wait returning and
  the `Popen` after it. A `stop()` landing inside it reaped the old process and
  the pump then created a new `--timeout 0` encoder that nothing referenced and
  nothing would ever kill — reparented to init, outside the service's control
  group, holding the camera across restarts. Spawning is now atomic against
  stopping. This is the only mechanism found that explains "it did not clear on
  a service restart", and it is the one a purely in-process model would have
  missed.

What is deliberately *not* claimed: `retire()` is kept, but it is no longer
load-bearing — there is no lazily-reopened handle left to leak, and it now only
stops an already-dispatched capture from spending a lease its successor is
waiting for. Keeping it costs a flag and closes a small window; removing it
would have been a third change to the same code in a week.

## 46. The stream's facts are read from the stream, per stream

Three numbers describing the live video — codec, frame rate, picture size —
were each taken from somewhere other than the bitstream, and each went stale
independently. All three were measured wrong on one bench camera in one
evening, and all three fail *silently*: ffmpeg copies happily, MSE accepts the
source buffer, and what an operator sees is a degraded picture, which is what a
failing camera looks like.

- **Codec.** `RtspCamera._codec` was probed once and cached for the life of the
  driver. A camera whose encoder is changed in its own web UI — a checkbox, and
  it happened twice — leaves the station announcing `hvc1.1.6.L153.a0` over
  H.264 bytes, with H.265 NAL rules applied to them.
- **Frame rate.** `StreamSession.settings()` runs *before* `_build_source()`,
  and it read `camera.stream_fps` — which at that moment still held the value
  seeded from a stored `fps` param, because the probe that corrects it runs
  inside `stream_source()`. A stored 30 against a camera sending 25 clocked the
  muxer 20% fast: the timeline advanced faster than frames arrived, which is
  stutter and catch-up. Only on the first stream after a restart, because every
  later one reused the cached probe — which is what made it look intermittent.
- **Dimensions.** The H.264 sample entry took its size from the station's
  configured resolution. Under a 1080p site policy in front of a 4K camera that
  writes 1920x1080 into a container carrying 3840x2160 pictures. HEVC had been
  fixed to read its SPS and H.264 was explicitly flagged and left.

The rule now: **nothing about a stream may be cached across streams or seeded
from stored configuration.** The probe is redone on every `video.start`; the
rate travels on the *source* and reaches the muxer directly, so the clock
cannot be built before the source is known; and both codecs read their picture
size out of the sequence parameter set. `settings()` is now what its name says
— instructions to an encoder, which a remux path has none of — rather than a
description of a stream.

The stale `fps` param got a structural answer rather than a migration.
`Inventory._instantiate` filters stored params by **constructor signature**,
not by what the registry declares, so a field removed from the registry goes on
being honoured for as long as the driver will accept it. Removing the argument
is what actually retires the setting: a field that cannot be passed cannot be
stale.

And the probe is no longer trusted, only *checked*. `sniff_codec` reads the
codec off the NAL headers, consulting no configuration, and a session whose
bytes disagree with its container is stopped with a reason rather than streamed
as something that says one thing and carries another. That is what makes "hvc1
announced for H.264 bytes" impossible by construction rather than by
remembering to invalidate a cache — and it is also the mid-stream detector: a
codec changed underneath a running session is caught at the next keyframe.

Two things it deliberately does not do. It reads **only parameter sets**: slice
headers alias badly between the codecs — H.264's non-IDR `0x41` and IDR `0x45`
read as HEVC types 32 and 34, VPS and PPS — and the first version of the
function did exactly that and declared every H.264 stream to be H.265 on its
second frame. The unit suite caught it before the hardware did. And it
**restarts rather than re-negotiates**: the muxer's clock, its parameter sets
and the platform's init segment all belong to the old codec, and the platform's
viewer cannot swap a `MediaSource` codec mid-session either.

Not built, and recorded rather than quietly skipped: **composition time
offsets**. The muxer writes a flat presentation timeline and this was the
standing first suspect for the stutter. It was measured and ruled out — the
bench camera reports `has_b_frames=0` and delivered 21 I and 328 P frames over
five seconds. The gap is real and remains unreachable from any hardware in
front of us: rpicam-vid and the synthetic source emit no B-frames by
construction, this camera emits none by configuration. Building a fix with no
failing case to prove it is how the flat timeline came to be written in the
first place.


## 47. The broker is reached over 443, and MQTT is not the destination any more

Redis on 6380 works on a LAN and nowhere else. Behind a reverse proxy — which
is what a public deployment is — that port is shut and 443 is the only one
open, at every site, on every corporate network, over Starlink. "Only 443 is
open" is the normal condition, not a quirk to work around once.

**MQTT was the recorded intention and it is not any more.** `mqtt.py` was a
stub whose constructor raised, nothing instantiated it, and yet
`contract/transport.md` and `contract/enrolment.md` both pointed at it — three
documents naming a destination the project had stopped walking toward, which
is worse than having no plan, because somebody would have built toward it.

MQTT was priced properly before it was dropped. It is the better protocol for
this traffic and it loses on one thing: port 8883, which is shut wherever 6380
is. MQTT over WebSocket on 443 is real and would clear that, but it costs a
broker service, a client library on the station — breaking the
one-dependency rule — and a bridge into Redis, which does **not** go away:
the console's realtime bus, the media relay and the ingest leader election all
use it. So MQTT would have been a *second* broker alongside Redis, to replace
forty lines of topic checking. More moving parts, not fewer.

What it would have bought that the relay does not: broker-enforced ACLs, which
is audited code holding the isolation property instead of ours. That is a real
loss and the reason `verify_broker.py` exists — deleting the check makes it
fail and name exactly what leaked. Last Will was the other argument, and the
relay gets the same thing free by knowing when its own socket closes.

**If station traffic ever needs to be consumed by anything outside this
platform** — a customer's SCADA, another vendor's tooling — MQTT's standardness
becomes worth the second broker, and adopting it then is much more expensive
than adopting it now. That is the condition that would reverse this.

## 48. Remote update, container era: signed registry images, commanded by the platform, over the pull that already works

Item 39 built a remote updater — pull a digest-pinned image on a jittered timer,
gate it on the new container actually *publishing*, roll back to the image
already on disk if it does not — and item 40 proved it costs nothing on the link.
Item 35 then moved deployment from systemd to Docker Compose built from the local
checkout, and in doing so **dropped the updater**: `docker-compose.yml` and
`bootstrap.sh` now say plainly "no updater daemon, no image registry," and the
update is `git pull && docker compose up -d --build` by hand over SSH. Item 5
called finishing this "now urgent" and named what was still owed. This is the
answer, for the container era.

**The shape.** CI builds a versioned, multi-arch image and pushes it to a
registry. The platform names a target in a command over the broker. The agent —
which cannot update itself, and must not be able to — writes the target into a
handoff file on a shared volume. A host-side updater, root, *outside* the
container, reconciles it: pull the pinned digest, **verify its signature**,
`up -d`, run item 39's publish-gate, roll back on failure. It is item 39's
mechanism re-homed from systemd onto Compose, plus the two things item 5 said
were still missing: a trigger the platform controls, and provenance.

**The sandbox is why the updater is not in the container, and that has not
changed.** The agent has no docker socket, `cap_drop: ALL`, `read_only`,
`no-new-privileges` (item 35c). A container that could recreate itself could
replace itself with anything, which makes the signature check and the gate
decorative — item 39 said this for systemd; a container sharpens it. So the agent
only *requests*, the same shape as the `setup-open` marker the host touches to
reopen the console window, and the host does the privileged work. The handoff is
a bind-mounted directory, not a reach into Docker's volume path, so neither side
depends on the other's internals; and a container that cannot start can still be
rolled back by something that is still running.

**Signing, which item 5 left open and is the point of this pass.** A digest pin
makes the image immutable; it does not say who built it. Anyone who can write to
the registry, or stand in for it, can publish a station update — remote code
execution on every box in the fleet. So the updater **verifies a signature
(cosign) before it runs a pulled image**, against a public key baked onto the
box. Two rules item 5 set, kept here: the signing key is **not** the enrolment
trust root — a compromise of one must not be a compromise of both — and it is not
the broker credential either. Pin and signature are belt and braces: the pin
stops the bytes changing under a name, the signature stops an attacker choosing
the name.

**Trigger: the platform commands it, over the channel that already exists.** Item
40 showed the pull is 0.02% of the telemetry budget, so bandwidth was never the
case for a push — *latency* is (up to ~8 h from release to a box having it),
which item 40 handed to `CONTRACT-QUESTIONS` item 11 as a contract change rather
than something to invent. This is that change: a `system.update` command in
**contract 2.1**, carrying the target tag and digest. An old station ignores a
command it does not know — the "station is older than the platform" path is
forward-compatible already — so shipping the command breaks nothing in the field.

**Keep the pull as the floor — recommended, and the one thing here to confirm.**
The command is the low-latency trigger; item 39's jittered timer is the resilient
floor under it. The broker can be down for exactly the reason an update is wanted
— a bad release wedged the uplink — and a station reachable *only* by command is
one that cannot be rescued when the command path is the thing that broke. The
pull already exists, is tested to 21 scenarios, and costs ~24 KB/day. Dropping it
to have a single control path trades the cheapest resilience on the box for
tidiness. The owner picked "command" over "both"; this record recommends
**both** — command for speed, pull for rescue — and flags it for that decision.

**Backwards compatibility, and why rollback rests on it.** The command is
optional and additive (2.1); config stays additive-only (SiteConfig is versioned
and tolerant of fields it does not know). The load-bearing case is rollback: a
box that rolls back drops to the *previous* contract version, so **the platform
must speak N-1 as well as N through any rollout**. That is not politeness — a
rolled-back station the platform can no longer talk to is a bricked station with
extra steps.

**Still open — governance, not mechanism (item 5's other two).** Who may publish
a release and how one is approved: nothing technical yet stops a half-finished
build reaching the tag every station follows, and the signing key turns "who
holds it" into the same question with higher stakes. And staging: updating one
station, watching it, then the fleet — item 39's jitter staggers by accident, a
command lets it be staged on purpose, but the *policy* is a process nobody owns.
Key custody and the release/approval pipeline are prerequisites to turning this
on, not details to settle afterwards.

**Build order.** The station + host slice lives in this repo and is testable
here: the `system.update` command handler and version reporting; the host
updater, re-homed from item 39's `gsu-update.sh` onto Compose with `cosign
verify` added; `bootstrap.sh` to install it and `docker-compose.yml` to pull a
signed, pinned image from the registry instead of building locally. The registry,
the CI multi-arch build with signing, and the platform half (sending the command,
tracking desired-vs-running per station, N-1 support, the staged-rollout UI) are
their own tracks, and the platform half is another repo.

**Not decided here — needs the owner or the platform team:** the registry and the
box's scoped pull credential; the signing key's custody and rotation; the
publish/approve pipeline; the staging policy; and the pull-floor question above.

**Not verified:** none of this has run yet. Item 39's decision logic is tested
against a stubbed Docker; the `cosign verify` step, the broker-command path, and
the Compose re-homing are new and unexercised. First box gets it by hand,
`--status` then once for real, before any timer or command is let near it.
