# Contract questions from the station side

Raised, not changed. Nothing in `contract/` has been edited by this side — each
item below is something the station ran into while being built, with the shape
of a proposed change and what the station does about it.

**Items 1, 2, 3 and 8 are settled and implemented** (contract commits `ee44e25`
and `ed02b31`, station side done and conformant). They are kept here with what
shipped, because the reasoning is why the schema and the harness look the way
they do. **Items 4, 6, 7, 9 and 11 are open** — the event
channel and the camera media path are the two that matter most. **Item 10 is
new**: TLS has landed, and `enrolment.md` §4 and §11 now describe a state of the
world that has passed. **Item 11 is new**: the station can update itself, but
nothing can tell it to look.

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

## 5. Additive telemetry kind: `health` — **RESOLVED, adopted**

**Shipped, on both sides.** `health` is in
`contract/schemas/telemetry.schema.json` `$defs/health`, in the platform
ingest's `KNOWN_KINDS`, and rendered by the console. `devices[].simulated` is
what drives the DEMO badge — the schema says so in the field's own description.

**I claimed twice, in two reports, that the platform was still dropping this
kind. That was wrong**, and it had been wrong for a while. I inferred it from an
absence of health frames on the fan-out at a moment when the only station
publishing them was one I had just stopped, and I did not check
`server/app/backend/services/station_ingest.py` — which is in this repository
and which I am permitted to read. Recorded here because the mistake is the exact
one this document exists to argue against, made in the other direction: absence
of data treated as evidence about a consumer.

Two consequences worth keeping:

- The station's own `test_telemetry_matches_the_schema` did not include
  `health`, left over from when it was an unknown kind. Adding it immediately
  found **two schema violations of mine** — see below. A payload nobody
  validates is a payload that drifts.
- `devices[].status` (`not_fitted`, `configured_absent`, `stalled`) is in the
  schema and delivered. That is the structured form of a distinction the
  platform would otherwise receive only as English prose in
  `unavailable_reason`, and "never fitted" versus "fitted and failed" is a
  difference an operator acts on differently.

### The two violations, and why neither was caught

Both were found by validating the payload rather than by anything failing.

**`status` used the wrong vocabulary.** The schema's `health.status` is a
summary — `ok | degraded | failing` — deliberately *not* the per-condition
severity vocabulary `info | warning | critical` that `conditions[].severity`
uses. The station published the latter. Fixed by `health.Health.summary()`,
which maps `info → ok` (an informational condition is not a fault), `warning →
degraded` and `critical → failing`.

**`credential.expires_at` was `null` before enrolment**, where the schema types
it as a string. The station was breaking its own rule that an unsourced value is
omitted rather than defaulted. The whole `credential` block is now omitted when
there is no credential — a station with none has no renewal health, and that
fact is already `enrolment.missing` in `conditions`.

**Why neither showed up:** conformance validates `health` only when a frame
lands inside its sample window, which at a 30-second cadence is intermittent —
and when one did, the station happened to be enrolled and healthy, which are
precisely the two states in which the payload was valid. Both faults appeared
only when something was already wrong. `tests/test_station.py` now validates
`health` in the unenrolled state and at every condition severity.

```jsonc
{
  "kind": "health",
  "agent_version": "0.1.0",
  "config_version": 3,               // enrolment.md §7 asks for this in telemetry
  "status": "ok",                    // ok | degraded | failing — a SUMMARY, and
                                     // not the severity vocabulary below
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

It costs about 90 bytes/s at 30-second cadence.

**Three fields the station sends are not in the schema**: `security` (which link
is verified and against which CA), `clock` (what is disciplining the clock, and
whether an RTC is fitted), and `resources` (SDR tuners by serial). The schema
allows additional properties, so they are valid rather than merely tolerated,
and the platform is free to ignore them. Proposing them properly: the first two
answer questions about an unattended box that are otherwise unanswerable from a
desk — *is that station's traffic actually encrypted, and is its clock being
kept* — and `clock` is the early warning for the §6 failure that strands a site.

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

### 10b. `ca_pem` is the broker's root, and the contract should say so out loud

§4 puts `ca_pem` inside the `broker` object, and its comment says "pinned; the
station verifies the platform" — the broker's CA, described as verifying the
platform. I read that as "one CA signs both" and used it for the API as well.
**That was wrong**, and it would have failed every station at once the day the
API moved behind a reverse proxy with a public certificate.

The station now keeps two roots: the broker is always pinned to `broker.ca_pem`,
and the API is verified against the system trust store unless a CA is configured
for it locally (`DECISIONS.md` item 36).

**Proposed:** correct the comment on `ca_pem` to say what the field is — *the
broker's trust root* — and add a sentence stating that the API's certificate is
a deployment concern, not something enrolment describes. The current wording
actively invites the mistake I made, and the next person to implement a station
from this document will make it too.

If the platform ever *does* want to pin the API from enrolment, that wants its
own field (`platform.ca_pem`) rather than reuse of this one, so that the two can
diverge without an ambiguity.

### 10c. Nothing says how a trust root gets onto the box before it is needed

Two bootstraps, and neither is documented.

**The API.** The first `POST /api/enrol` carries the enrolment token and
receives the credential, over a connection that must already be trustworthy. A
public certificate on a real domain solves this completely — the system trust
store is the out-of-band provisioning, done years in advance by the OS vendor.
That is now the expected arrangement and is a good reason to prefer it. But
**while the platform serves its own certificate**, the CA must be carried to the
box and verified by eye, and nothing in §5 says so.

**The broker.** Bootstraps cleanly — `broker.ca_pem` arrives inside the
already-verified enrolment response. Worth stating explicitly, because it is the
part that reassures somebody reading §4 that there is no circularity.

**Proposed:** a paragraph in §5 covering both: that the broker's CA arrives with
enrolment and needs no provisioning, and that a platform not fronted by a
publicly-trusted certificate requires its CA to be installed out of band with
its fingerprint verified by a person against a channel that did not deliver the
file. That last part is the root of the whole chain and is currently nobody's
documented job.

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
**must** pin `broker.ca_pem`, **must** verify the broker against it, and **must
not** fall back to plaintext or to the system trust store if that verification
fails. `transport.md`'s "Broker" section, which still says "Redis pub/sub
today", could say Redis-over-TLS in the same commit — and its "Identity" section
still says enrolment "is not built yet on either side", which is two revisions
out of date.

---

## 11. There is no way to tell a station to check for an update

**Where** `command.schema.json`; `enrolment.md` §9.5.

The station now updates itself: it pulls a pinned image reference on a timer,
applies it, proves the new build enrols and publishes, and rolls back to the
image already on disk if it does not (`DECISIONS.md` item 39). That mechanism
needs no contract change and none has been made.

What it cannot do is find out sooner. The timer runs every 6 hours with up to
2 hours of jitter, so **a release takes up to about 8 hours to reach a
station**. For a routine update that is fine. For a fix to something that is
actively wrong at a site — a bad build that got through, a decoder crashing on
real traffic — 8 hours of waiting while the answer already exists is the wrong
shape, and the only alternative today is SSH to a box that is *"difficult to
physically access"*.

**Proposed, additive:** a command on the existing channel that means only *"run
your update check now"*.

```jsonc
{"kind": "update.check"}
```

Deliberately **not** `{"kind": "update.apply", "image": "…"}`. The command must
not carry an image reference, a URL or anything else the station would act on:

- The station already knows what it is allowed to run — `GSU_UPDATE_REF`, set
  locally, ideally a digest. A command that could name an image would turn the
  command channel into a code-execution path, and the broker credential into a
  way to replace the software on every station in an organisation. That is a
  much larger blast radius than the credential is meant to have.
- Everything protective stays in force: the same digest pin, the same health
  gate, the same automatic rollback. The command changes *when* the station
  looks, never *what* it fetches or whether it keeps it.
- It fits the contract's existing shape — commands are requests, may be missed,
  and are never assumed to have taken effect. A missed `update.check` costs at
  most the normal timer interval, which is exactly the current behaviour.

**Reporting it back.** Every command has a corresponding field in telemetry, per
`transport.md`. The station already publishes `health.agent_version`, so the
effect is observable: the platform sees the version change, or it does not.
Worth considering an explicit `health.update` block (`last_check`,
`last_result`, `rolled_back`) so a rollback is visible without inferring it from
a version that stayed the same — the rollback is the case somebody most needs
to know about, and it is currently only in the station's local journal.

**Until this exists** the station polls, which costs about 6 KB a check
(item 40) and works. This is a latency proposal, not a bandwidth one.

---

## 12. The video channel exists, and the broker refuses it — **BLOCKER, measured**

**Where** `contract/enrolment.md` §4 (`broker` object), `transport.md` (the
channel table), and the platform's `services/broker_acl.py`.

`schemas/video.schema.json` defines `gsu/{station_id}/video` and the platform's
ingest already subscribes to it — `VIDEO_PATTERN` is there and `video` is in
`KNOWN_KINDS`. **The broker ACL was not changed with it.** A station's principal
is granted exactly three channels:

```python
f"&gsu/{station_id}/telemetry", f"&gsu/{station_id}/audio", f"&cmd/gsu/{station_id}"
```

Measured against the live broker with this station's own credential, not
inferred:

```
PUBLISH gsu/29ed8568-…/telemetry  -> OK, 1 subscriber
PUBLISH gsu/29ed8568-…/video      -> NoPermissionError: No permissions to
                                     access a channel
```

**Two changes are needed on the platform side and neither is this side's to
make:**

1. Add `&gsu/{station_id}/video` to `_channels()` in `broker_acl.py`. Existing
   stations need `sync_all` — which runs at start-up — or a re-enrolment.
2. Add `video_topic` to the enrolment response's `broker` object, and to §4's
   worked example beside the other three.

**What the station does meanwhile.** It publishes to
`telemetry_topic`-with-`video`-on-the-end, derived rather than told, and says in
`credentials.py` that this is a temporary exception to "topics come from
enrolment, never from string-building". The moment `video_topic` arrives it stops
guessing. A refused publish is reported as `video.topic_refused` in health
telemetry, retried every five minutes so a fixed ACL needs nobody on site, and —
importantly — **does not take the uplink down**: a `NOPERM` used to be handled as
a broken connection, which would have closed the client and backed off *all*
telemetry because video was not permitted. That is fixed and tested
(`tests/test_video.py::RefusedChannelTests`).

**Also worth a line in `transport.md`.** Its channel table still lists two
station→platform channels. Video is a third, and the "Streams with no source"
rule applies to it unchanged — this station sends `available: false` with a
reason when no camera is fitted, rate-limited to 1 Hz, which is telemetry's own
cadence for the same statement.

---

## 13. On-demand video needs a command, and here is the shape

**Where** `schemas/command.schema.json`.

Publishing video to a console nobody is watching is the most expensive mistake
this station can make, and it makes no noise. The platform knows whether anyone
is attached; the station cannot. So the platform has to ask.

**Implemented on this side already, provisionally, and easy to change:**

```jsonc
{"kind": "video.start", "viewers": 2, "lease_s": 30,
 "width": 1920, "height": 1080, "fps": 30, "bitrate_kbps": 3000}
{"kind": "video.stop", "reason": "the last viewer detached"}
```

Four properties, each of which is a failure designed out rather than noticed:

- **Idempotent.** A second viewer extends the lease and does not start a second
  encoder. There is one camera, and the second `rpicam-vid` fails with a
  device-busy that reads like broken hardware.
- **Leased, so it fails closed.** `lease_s` is a deadline, not a preference: the
  station stops when the platform stops renewing. That is deliberately the
  opposite of "stop when told to stop", because the case to design for is the
  console closing or the link dropping while the station keeps paying for a
  stream nobody can see. Bounded to 5–300 s, defaulting to 30.
- **A ceiling as well.** `stream_max_minutes` in site config stops a stream that
  is somehow still being renewed after an hour.
- **The station may narrow what is asked for, never widen it.** Resolution, rate
  and bitrate are capped by site configuration, because the link belongs to
  whoever pays for it rather than to whoever opened a console.

**Reported, not assumed**, per `transport.md`: `health.video.stream` carries
`state`, `viewers`, `since`, `lease_remaining_s`, measured `fps` and
`bitrate_bps`, `dropped`, and a `reason` when it is not running. A `video.start`
that silently did nothing is visible — a station with no camera answers
`state: "unavailable"` with `"no camera fitted, so there is nothing to stream"`.

**The one thing to decide together:** whether the lease is renewed by repeating
`video.start` (what this implements) or by a separate `video.keepalive`. Repeating
`video.start` is fewer moving parts and is naturally idempotent; a distinct
keepalive is cheaper on the wire and easier to rate-limit. Either is a small
change here.

**And a snapshot equivalent.** The same argument applies to the MJPEG channel at
2 fps, which is a twelfth of the stream but still continuous. A `subscribers`
count pushed by the platform, or `video.snapshots {on|off, fps}`, would let the
console drop it to a frame every ten seconds when nobody has the station open.
Until then `config.set` with `video_enabled` and `video_fps` is the manual
version and it works.

---

## 14. There is no transport for H.264, and the station must not invent one

**Where** nothing yet — this is the piece that does not exist on either side.

1080p30 is 2–4 Mbit/s. That cannot go through the broker: Redis pub/sub carries
telemetry and commands, and several Mbit/s alongside them would compete with the
traffic it must not delay. It cannot go to a viewer either
(`00-topology.md` rule 8, `03-realtime-isolation.md` §7). So it is an outbound
connection from the station to the platform, TLS, authenticated with the
credential the station already holds — outbound because Starlink is CGNAT and
nothing reaches inward.

**`gsu/transport/stream.py` is a documented stub**, like `mqtt.py` beside it,
because a wire format guessed at from this side would be discovered to be wrong
by somebody debugging a black video panel over a satellite link.

What is convenient to produce, from the encoder outward:

- **Annex B, untouched.** `rpicam-vid -o -` emits NAL units with start codes.
  Fragmented MP4 means muxing on a 900 MHz core that is already running the
  station and buys the station nothing. The platform can remux once, centrally,
  where there is CPU.
- **One frame per message, length-prefixed.** Four bytes of length, a flag byte
  for keyframe, eight bytes of capture timestamp, then the access unit. The
  station already has all three; anything self-describing beyond that is work it
  would have to do per frame.
- **Parameter sets repeated at every keyframe** — the station passes
  `--inline`, and also keeps the last SPS/PPS so a viewer attaching mid-stream
  can be given them without waiting up to two seconds for the next IDR.
- **Back-pressure the station can see.** When the link cannot carry the stream
  the station must drop frames rather than buffer them — a buffered second of
  1080p is several megabytes of a picture that is already out of date — and it
  needs to know it is happening in order to report it. A socket that simply
  blocks turns a bandwidth problem into a stalled encoder.
- **Capture timestamps, not arrival ones.** Same rule as the snapshot channel
  and for the same reason.

**Say what you want and it gets implemented behind `StreamUplink`**, which is
one class with `open`, `send`, `close`. Nothing above it changes.
