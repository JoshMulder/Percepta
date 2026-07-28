# Contract questions from the station side

Raised, not changed. Nothing in `contract/` has been edited by this side — each
item below is something the station ran into while being built, with the shape
of a proposed change and what the station does about it.

**Items 1, 2, 3 and 8 are settled and implemented** (contract commits `ee44e25`
and `ed02b31`, station side done and conformant). They are kept here with what
shipped, because the reasoning is why the schema and the harness look the way
they do. **Item 5 is with the owner. Items 4, 6, 7 and 9 are open** — the event
channel and the camera media path are the two that matter most. **Item 10 is
new**: TLS has landed, and `enrolment.md` §4 and §11 now describe a state of the
world that has passed.

---

## 1. There is no way to say "this stream has no source" — **RESOLVED**

**Shipped.** Any telemetry payload may carry `available: false` and
`unavailable_reason` (≤200 characters); when it does, the stream's own fields
are no longer required. The console renders a `NO ADS-B` badge, deliberately
distinct from its fault marker.

**The station now** publishes `{"kind": "adsb", "available": false,
"unavailable_reason": "no ADS-B receiver connected"}` **on the normal cadence**
for any stream with no source — never by going quiet, because a station that
goes quiet has failed and only the station knows the difference. Conformance
passes with the stream declared unavailable and commands against it skipped.

Note the line the contract draws, which the station observes: `available: false`
is for a stream with **no source at all**. A field the instrument simply does
not measure is **omitted** (item 2). Reaching for the former where the latter is
meant would tell an operator the weather station is missing when it is working.

The original argument follows.

**Where** `schemas/telemetry.schema.json`, `conformance/check_station.py`.

The hardware in the current station is one RTL2838 dedicated to airband, an
Airmar 110WX on a USB-UART, a Pi camera on CSI, and a uAvionix ping RX Pro for
ADS-B **which is not yet connected**. A station in that state has no ADS-B
source at all, and the contract gives it two options, both wrong:

- publish `{"kind": "adsb", "aircraft": []}` — which means *clear airspace*, and
  is indistinguishable from a working receiver seeing nothing;
- publish nothing — which is what this station does, and which fails
  `publishes adsb` in conformance and leaves the console with a panel that has
  no explanation attached to it.

An empty ADS-B map and a dead ADS-B receiver look identical, and only the
station knows which it is.

**Proposed, additive.** Allow any telemetry object to carry an availability
statement, and relax the `required` list when it says the stream is unavailable:

```jsonc
{
  "kind": "adsb",
  "available": false,               // default true when absent, so nothing breaks
  "unavailable_reason": "no ADS-B receiver connected"
}
```

Consumers that do not know the field see a payload with no `aircraft` and can
ignore it exactly as they ignore an unknown kind today; consumers that do know
it can render "no receiver" instead of an empty sky. `check_station.py` would
then accept `available: false` in place of a full payload for a slot the station
declares empty.

The gap is also still reported structurally in the `health` payload (item 5) as
`unsourced_streams`, which carries the distinction the prose reason cannot: the
device inventory says whether a slot is `not_fitted`, `configured_absent` or
`stalled`. A console that wants to tell "never fitted" from "fitted and failed"
without parsing English has it there.

---

## 2. `weather.humidity_pct` was required, and the fitted instrument may have no
   humidity sensor — **RESOLVED**

**Shipped.** `humidity_pct` is out of `weather.required`, which is now `kind`,
`wind_kt`, `gust_kt`, `wind_dir_deg`, `temperature_c`. The console strikes an
absent reading through in red, distinct from the dashes it shows while waiting.

**The station's behaviour is unchanged** — it always omitted what it could not
measure — except that those payloads are now schema-valid rather than
deliberately invalid. The original argument follows, because it is also the
record of what a 110WX can and cannot source.

**Where** `schemas/telemetry.schema.json`, `weather.required`.

Airmar sell the 110WX in two variants: with and without the relative-humidity
module. The datasheet is explicit that RH is optional and that dew point and
heat index are calculated *from* it. The unit has **no rain gauge, no visibility
sensor and no sky observation** in either variant.

So on the hardware described to us:

| console field | source on a 110WX |
|---|---|
| `wind_kt`, `wind_dir_deg` | measured (ultrasonic) |
| `gust_kt` | **derived** station-side: peak of a rolling 10-minute window |
| `temperature_c` | measured |
| `pressure_hpa` | measured |
| `humidity_pct` | **only if the RH module is fitted** — and it is *required* |
| `rain_rate_mmh`, `rain_mm_today` | **no sensor exists** |
| `visibility_km` | **no sensor exists** |
| `sky` | **no sensor exists** |
| `is_day` | derivable from position and time, not measured |

`rain_mm_today: 0.0` during a downpour, because there is no rain gauge, is a
number an operator can act on and cannot tell is invented.

The station omits every field it has no sensor for, including `humidity_pct`
when the module is absent. **Rainfall is settled too, as a hardware decision:**
there will be no gauge, and the console strikes the reading through. Omitting
the rain fields is the correct long-term behaviour rather than a placeholder,
and no rain driver is to be written.

Still worth considering, and not part of the decision: `sky` has no `"unknown"`
member and is not nullable, so "unobserved" and "clear" can only be
distinguished by absence.

---

## 3. A contact with no position cannot be represented — **RESOLVED, no schema
   change**

**Decided:** transmitting position is the primary function of ADS-B, so a return
without one is not a contact worth reporting. `range_km` and `bearing` stay
required, and the rule is now written into the `aircraft` description.

**The station's behaviour is unchanged and correct:** positionless returns are
dropped, counted, and the count reported in health telemetry. The original
argument follows.

**Where** `schemas/telemetry.schema.json`, `$defs/aircraft`.

`latitude`/`longitude` are nullable, and the description says why: "Null until a
position has been decoded; a Mode S response alone gives none." But `range_km`
and `bearing` are **required**, and both can only be computed *from* a position.
MAVLink's `ADSB_VEHICLE` makes this concrete: `ADSB_FLAGS_VALID_COORDS` may be
clear while altitude and callsign are valid, which is a real contact the station
has genuinely heard.

The station drops positionless contacts from the `aircraft` array and counts
them in health telemetry. They are heard and not reported, which is a small,
now-deliberate loss of information.

---

## 4. There is no channel for anything the station buffered during an outage

**Where** `transport.md`, "Store and forward".

The contract asks the station to "keep sensing, recording and locally alerting,
and reconcile when the link returns", and separately — correctly — says not to
replay stale telemetry. But *events* are exactly what should survive an outage:
"an aircraft came within 6 km at 400 m at 02:14" stays true whether or not
anyone heard it, and it is the first thing an operator asks about afterwards.
There is no channel for it. The only alert path in the codebase is
`hub.status_message`, which is platform-internal and not reachable from a
station.

**Proposed.** A third station→platform channel, `gsu/{station_id}/events`, or an
`event` telemetry kind, carrying `at`, `kind`, `severity`, `detail` and a
station-generated id so the platform can deduplicate a replay. Unlike telemetry
this one is worth delivering at least once.

**Until then** events are written to SQLite on the box, survive reboots, are
shown on the local console, and are marked unsynced. `store.pending_events()`
exists and nothing calls it, deliberately.

---

## 5. Proposed additive telemetry kind: `health` — with the owner

The station already publishes this, on the strength of the schema's own promise
that unknown kinds are dropped and logged rather than erroring "so a new sensor
can be added station-side before the platform renders it". It is the only way to
say several things the contract otherwise has no room for, including
`config_version`, which `enrolment.md` §7 *requires* be reported in telemetry
and which no schema field carries.

**Today it is write-only.** `health` is not in the ingest's `KNOWN_KINDS`, so
the platform drops it and nothing here reaches a console — which is the platform
doing exactly what the contract promises, not a fault. The station keeps
publishing it regardless: it costs ~90 B/s, it is what the *local* console
renders when there is no link at all, and when the kind is accepted nothing on
the station side has to change.

The part worth having soonest is `devices[].status` — `not_fitted`,
`configured_absent`, `stalled`. That is the structured form of a distinction the
platform currently receives only as English prose in `unavailable_reason`, and
"never fitted" versus "fitted and failed" is a difference an operator acts on
differently.

```jsonc
{
  "kind": "health",
  "agent_version": "0.1.0",
  "config_version": 3,               // enrolment.md §7 asks for this in telemetry
  "status": "ok",                    // ok | info | warning | critical
  "conditions": [                    // each with an id, severity, detail, since
    {"id": "credential.renewal_failing", "severity": "warning",
     "detail": "3 failed renewals; expires in 41.2 h", "since": "…"}
  ],
  "uplink": {"connected": true, "dropped_frames": 0, "offline_seconds": 0},
  "credential": {"expires_at": "…", "renewal_failures": 0},
  "devices": [ /* configured vs detected, per slot — enrolment.md §7 */ ],
  "unsourced_streams": ["adsb"],     // item 1
  "unsourced_fields": {"weather": ["humidity_pct", "rain_rate_mmh"]},  // item 2
  "resources": [ /* SDR tuners, by serial */ ],
  "storage": {"events": 12, "events_pending": 12, "recordings_mb": 4.1}
}
```

It costs about 90 bytes/s at 30-second cadence. Currently it appears in
conformance output as `unknown kind 'health' ignored, as the contract allows`.

---

## 6. `config.set` is specified in prose and absent from the schema

`enrolment.md` §7 describes configuration delivery on the command channel and
the platform lists it as still owed. `command.schema.json` has no entry, so its
payload shape is undefined. The station implements a provisional one and will
change it to whatever the platform actually sends:

```jsonc
{"kind": "config.set", "version": 4, "config": {"alert_range_km": 8.0}}
```

Reported back as `health.config_version`, per the same rule as every other
command: the platform never assumes the change took.

---

## 7. The camera has nowhere to send anything

`00-topology.md` puts a camera in every station and rules 5/8 describe media as
GSU → server → viewer, pulled on demand. The contract has no media channel, no
camera telemetry kind, and no way for a station to say a camera is fitted and
healthy. The device registry supports both a Pi CSI camera (no address, no
credentials — it is a ribbon cable) and an ONVIF network camera (address and
credentials, which is the only case §7 describes), and neither can currently
produce anything the platform will accept.

Not urgent, but it means the camera is unmonitored: a failed camera and one that
was never fitted look identical from the console.

---

## 8. Conformance and missing sensors — **RESOLVED, both halves**

**Missing hardware.** `check_station.py` accepts a stream declared `available:
false` in place of a payload and skips commands against it. Verified: with
ADS-B, radio, weather and power all unsourced, the station passes with six
skipped command checks and four notes, and is not failed for lacking hardware.

**Flakiness.** The audio-gating check compared *any* audio in the window against
the **last** radio frame, so a station that transmitted at second 3 and closed
its gate at second 8 failed a check it had not broken. The command checks
sampled a fixed window and took whatever came last. Both are fixed upstream: the
gating check now concludes only when the squelch was closed throughout and says
plainly when it could not test, and commands wait for the expected value with a
ceiling.

Verified from this side after the fix — five consecutive clean runs, two of them
on a deliberately busy airband channel where audio flowed for most of the
window, which is the condition that used to fail. Those two produced the honest
"not tested" note instead of a false failure, and runs dropped from ~55 s to
~29 s.

Worth keeping in view: this harness is the only shared arbiter between two teams
on different machines, so a check that fails a correct station occasionally is
worse than one that is merely slow. Both remaining sources of luck — a channel
that happens to be quiet, and a station that happens to be transmitting — now
produce a note rather than a verdict.

---

## 9. The audio format costs a third more than the note says

`transport.md` puts uncompressed audio at ~384 kbit/s. Measured from this
station, base64 in JSON is **512 kbit/s while the gate is open** — 24 kHz × 16
bits is 384 kbit/s of PCM, and base64 adds 33%. Squelch gating is doing the real
work: measured over a busy channel the whole uplink averages 138 kbit/s, of
which 128 kbit/s is audio, against 10.7 kbit/s for all telemetry combined.

Nothing to change in the contract — the note already says Opus is coming — but
the number is worth having right when the case is made. Encoding is behind one
function (`gsu/radio/audio.py`) as the schema asks.

---

## 10. TLS has landed, and four things about it are now under-specified

**Where** `enrolment.md` §4, §11, §3; `transport.md` "Broker".

The station now connects over `rediss://` and `https://`, verifying both against
the `ca_pem` from the enrolment response, pinned and persisted 0600. Five
observations, in descending order of how much they would cost to get wrong.

### 10a. The CA needs `basicConstraints` and `keyUsage`, and nothing says so

Python's `ssl` module rejects a CA certificate that carries neither, with *"CA
cert does not include key usage extension"*. `redis-cli --cacert` accepts such a
certificate happily — so a CA can be tested, appear to work, and still be
refused by every station in the fleet.

**Proposed**, in §3 or §4 beside `ca_pem`: state that the CA must carry
`basicConstraints=critical,CA:TRUE` and `keyUsage=critical,keyCertSign,cRLSign`,
and that verifying it with `redis-cli` is not evidence a station will accept it.
This costs one sentence now and a fleet-wide outage the first time a CA is
rotated by somebody who has not hit it before.

*(Found and fixed on the platform side already; writing it down is what stops it
recurring at the next rotation.)*

### 10b. `ca_pem` is scoped under `broker` and is used for the API too

§4 puts `ca_pem` inside the `broker` object, and its comment says "the station
verifies the platform" — the broker's CA, described as verifying the platform.
In practice one CA signs both, and the station uses it for both. That works, but
it is inferred rather than stated, and a deployment that later fronts the API
with a different certificate would break stations silently.

**Proposed, additive:** either a sentence in §4 saying explicitly that the same
CA signs the API and the broker, or a `platform.ca_pem` alongside it for the day
they differ. The station reads `broker.ca_pem` today and would read either.

### 10c. Nothing says how the CA gets onto the box for the *first* call

The first `POST /api/enrol` happens before anything has been pinned, and that
call carries the enrolment token and receives the credential. If it is verified
against the system trust store, the pinning that follows is decorative — the
identity was handed over on a connection nobody checked.

The station's answer is `GSU_CA_FILE`: a CA installed with the image or carried
by the technician, and a refusal to enrol over `https://` without one. §5 says
what the technician does and does not mention carrying a CA; §3 discusses
credential types and not trust roots.

**Proposed:** a paragraph in §5 stating that the platform CA is provisioned out
of band, before enrolment, and that its fingerprint is verified by a person
against a channel that is not the same one that delivered the file. That last
part is the whole root of the chain and it is currently nobody's documented job.

### 10d. `broker.url` must be credential-free, and it is worth saying why

redis-py's `ConnectionPool.from_url` ends with `kwargs.update(url_options)`, so
credentials in the URL **override** those passed alongside it. A `broker.url`
carrying `user:pass@` would make a station authenticate as whatever the URL
names rather than as `gsu:{station_id}` — quietly leaving the tenancy model that
`README.md` rule 1 rests on.

The URL the platform sends is already credential-free and deliberately so. The
station strips and warns regardless.

**Proposed:** one line in §4 under `broker.url` — "carries no credentials; the
station authenticates as `broker.username` with its own secret" — so that a
future change to that field has the reason attached to it.

### 10e. §4 and §11 are now stale, in a way that reads as permission

§4's comment on `ca_pem` still says `NOT YET SENT … do not require it yet`, and
§11 still lists *"`ca_pem` is not yet sent, because the development broker has
no TLS."* Both were true and are not any more. A station written against the
document as it stands would treat the CA as optional — which is precisely the
"do not require verification" behaviour the pinning exists to prevent.

**Proposed:** delete both notes and replace them with the requirement: a station
**must** pin `ca_pem`, **must** verify the broker and the API against it, and
**must not** fall back to plaintext or to the system trust store if verification
fails. `transport.md`'s "Broker" section, which still says "Redis pub/sub
today", could say Redis-over-TLS in the same commit.
