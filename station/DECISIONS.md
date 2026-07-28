# Decisions and assumptions

Two lists. The first is what `contract/enrolment.md` §9 says needs a human — no
answers invented, only what the station does in the absence of one. The second
is every choice I made that someone should confirm.

---

## Open decisions, still open

### 1. Compute platform (§9.1)

Now partly answered — a Raspberry Pi 2B — and that answer has consequences the
decision has not caught up with, in HARDWARE.md:

- **No hardware keystore.** The credential is a 0600 file in a 0700 directory
  (`credentials.py`), which is what §3 allows. `CredentialStore` is the seam if
  a keystore appears.
- **No real-time clock.** §6's failure mode is live on this hardware. The
  station refuses to enrol with an implausible clock and raises a critical health
  condition, which is mitigation, not a fix. **An RTC module would remove the
  class of failure and costs a few pounds.**
- **Where the setup console is served** is still undecided — see 2.

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

Unanswered, so **MQTT is not implemented**. `transport/mqtt.py` is a stub with
the requirements written into it rather than a plausible-looking client that has
never connected to anything. The transport interface is the only place that
knows the broker is Redis; swapping it is one class and a URL scheme.

### 5. Software update path (§9.5)

Nothing built. The station has no self-update and no signature verification, and
should not acquire either before there is an answer — an update path is the same
trust root as enrolment and is worse than useless if it is improvised.

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

18. State lives in `station/var/` — credential, device inventory, site config,
    receiver state, event database, recordings. On real hardware that belongs on
    a persistent partition chosen with the compute platform.
19. **One runtime dependency: `redis`.** Everything else is standard library,
    including the HTTP client, the console, the serial layer and the MAVLink and
    NMEA decoders. A box in the field should not need to install anything.
20. **No git commits.** Work is left uncommitted for review, as instructed.
