# Contract questions from the station side

Raised, not changed. Nothing in `contract/` has been edited by this side — each
item below is something the station ran into while being built, with the shape
of a proposed change and what the station does about it.

**Items 1, 2, 3, 7, 8, 12, 13 and 14 are settled and implemented**, station side
done and conformant. They are kept here with what shipped, because the reasoning
is why the schema and the harness look the way they do.

**Open: 4, 6, 9, 10, 11, 15 and 16.** The event channel (4) is the one that
matters most now that video is closed — it is the only thing the station buffers
during an outage with nowhere to send it afterwards. 10 is the stale TLS wording
in `enrolment.md`; 11 is that nothing can tell a station to look for an update.
**16 needs a console answer as well as a contract one**: the station now
streams H.265 from a camera that speaks it, so the media channel's codec string
is sometimes `hvc1.…`, and a browser that cannot decode HEVC shows black
without raising anything. 17 is that this station no longer publishes
`gsu/{station_id}/video` at all. 18 is not a question but a decision already
taken and built on both sides: position is set on the station, and the platform
has stopped offering the field. **19 needs no schema change**: it records the
two ADS-B datapoints the aircraft object cannot hold, now that it carries
everything else the receiver reports.

**The whole camera path closed in one pass**: 7 (the camera had nowhere to send
anything), 12 (the broker refused the video channel), 13 (on-demand needed a
command) and 14 (H.264 needed a transport). Video now runs end to end, snapshots
and live, and the reasoning behind each shape is in those four items.

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

## 7. The camera has nowhere to send anything — **RESOLVED**

**Shipped, on both sides, and the camera is no longer unmonitored.** Three
things closed it, and each answers a different half of the original complaint:

- **A channel for the picture.** `contract/schemas/video.schema.json` and
  `gsu/{station_id}/video` — one complete JPEG per message, 640×480 at 2 fps by
  default, granted by the broker ACL (item 12).
- **A way to say the camera is fitted and healthy.** The same `available: false`
  rule as telemetry, so *"no camera fitted"*, *"no cameras available"* and *"the
  camera is in use by the live stream"* are now three different, published
  statements rather than one silence. `health.devices[]` carries the camera slot
  and `health.unsourced_streams` carries `video`, so the console can tell a
  failed camera from one that was never fitted — which is exactly what this item
  said it could not.
- **Media on demand, as `00-topology.md` rules 5/8 describe it.** H.264 as
  fragmented MP4 over a WebSocket to the platform, started by `video.start` and
  stopped when the platform stops renewing the lease (items 13 and 14). GSU →
  server → viewer, pulled rather than pushed, and nothing reaches a viewer from
  a station.

**Still true, and worth keeping in view:** the ONVIF network camera in the
registry has no driver. It is a different job from grabbing a still off a ribbon
cable — an RTSP pull and a re-encode — and it is the one case that would need a
decoder on the station.

The original argument follows.

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

## 12. The video channel exists, and the broker refuses it — **RESOLVED**

**Shipped on the platform, verified from here.** The ACL now grants
`gsu/{station_id}/video` and the enrolment response carries `video_topic`. The
bench station publishes snapshots to it: 14.8 kB payloads at 2 fps, no refusal,
telemetry unaffected. The station still derives the topic when the field is
absent, so a credential issued before the change keeps working, and it stops
guessing the moment one carries it.

The rest of this item is the original argument, kept because the failure it
describes — a channel in the schema and not in the ACL — is one that can recur
at every future channel.

### The original

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

## 13. On-demand video needs a command — **RESOLVED, and implemented**

**Decided: the platform repeats `video.start` as the renewal**, every 10 seconds
while anyone is watching, with `lease_seconds`; `video.stop` when the last
viewer leaves. No separate keepalive. Silence is the stop signal, which is
exactly the failure this had to be built around.

**The station accepts `lease_seconds`**, and also the two provisional names this
side used before the answer came (`lease_s`, `ttl_s`) — a station that
understands only the newest spelling of a field breaks on the day somebody
deploys an older console. A repeat while streaming extends the lease and counts
viewers; it never restarts the encoder. Verified end to end against the running
platform: `video.start` → streaming, a repeat → `already streaming; lease
extended`, `video.stop` → stopped.

The rest of this item is the original proposal, which is what shipped.

### The original

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

## 14. The transport for H.264 — **RESOLVED: fMP4 over a WebSocket**

**Shipped and verified against the running platform.** `wss://…/media/ingest`,
`Authorization: Bearer <credential>`, opened only while streaming; a text frame
carrying the codec string, a text frame `init`, then the initialisation segment
and one fragment per frame as binary frames. The station id is never sent.

Three things this side had to decide, and did:

- **The station muxes.** `gsu/media/fmp4.py` turns access units into fMP4, so
  the hardware encoder, the software encoder and the synthetic source produce
  identical container output and nothing depends on which muxer flags a given
  build of rpicam-apps supports. That is not something to discover on a remote
  box.
- **The codec string is derived, not configured.** `avc1.PPCCLL` comes from the
  encoder's own SPS. Guessing it is how a browser accepts a source buffer and
  then decodes nothing, which from the far end looks like a dead camera.
- **The WebSocket client is written out**, like the Annex B parser and the
  broker's TLS handling, because `requirements.txt` is one line on purpose.

Measured: 1080p30 for 15 seconds, 459 fragments, none dropped, 30.1 fps.
HARDWARE.md §9 has the numbers and what happens when the link cannot carry it.

The rest of this item is what was asked for, kept because the reasoning is why
the shape is what it is.

### The original

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

---

## 15. The light stream should be able to carry its measured current

**Where** `schemas/telemetry.schema.json`, `$defs/light`.

The station now supports a current sensor on the floodlight circuit: a
threshold in amps above which the lamp counts as drawing, and a per-light
setting for whether `light.on` reports the relay or the measured current.
`on` needs no schema change either way — the schema already defines it as
"what the hardware is actually doing, not what was last commanded", and a
current-derived answer is a *better* reading of that sentence, not a new
field.

Two things do not fit in the schema as written, and are **not** being sent
until they do:

```jsonc
{"kind": "light", "on": true,
 "measured_a": 1.25,       // proposed: amps through the lamp circuit,
                           // omitted (never zeroed) when no sensor is fitted
 "state_source": "current" // proposed: "relay" | "current" — which witness
                           // `on` is reporting, so a console can say so
}
```

- `measured_a` is the console's only route to "the lamp is tired" trends and
  to distinguishing a bright lamp from a barely-conducting one. Omitted when
  unsourced, per this station's own rule (DECISIONS.md item 16).
- `state_source` matters because the same `on: true` is a different strength
  of claim from a relay coil than from a measured 1.25 A, and an operator
  deciding whether to roll a truck deserves to know which they are reading.

**Until adopted**, the measurements reach people through channels the
contract already has: the fault checks travel as health conditions
(`light.no_draw`, warning — lamp, fuse or wiring; `light.stuck_on`,
critical — a welded relay burning the battery at an unattended site), the
amps appear in the device detail in `health.devices[]` and on the setup
page's light tab, and the local event log records fault edges. Nothing
invented on the wire; nothing measured kept from the operator.

---

## 16. The media channel's codec string is now sometimes `hvc1.…`

**Where** `transport.md` §"The live video stream"; `schemas/video.schema.json`
for the neighbouring question it raises.

A real RTSP camera arrived on the bench and it is 4K HEVC Main. The station
now carries H.265 end to end on the live stream — the same Annex B reader, the
same fMP4 muxer, the same uplink, one `hvc1` sample entry instead of `avc1` —
so the first text frame of a media session is no longer always an `avc1.…`
string. Nothing else about the channel changes: same URL, same bearer
credential, same `init`-then-segment-then-fragments order, station id still
never sent.

### What the platform must accept

The codec string is **derived from the stream's sequence parameter set**, per
ISO/IEC 14496-15 Annex E.3, and is one of two shapes:

```
avc1.PPCCLL            as today, unchanged, for any H.264 source
hvc1.P.C.TLL.CC…       for an HEVC source
```

Real examples, each verified byte-for-byte against what ffmpeg writes for the
same parameter sets:

| Stream | String |
|---|---|
| 1080p Main, level 4.0 | `hvc1.1.6.L120.90` |
| 4K25 Main, level 5.0 — the shape the bench camera will produce | `hvc1.1.6.L150.90` |
| Main 10, level 3.0 | `hvc1.2.4.L90.90` |

(The 4K row is what libx265 produces for 3840x2160 at 25 fps. The bench
camera's own value is the camera's to state and will appear in `video.codec` in
health telemetry the first time it streams — it is read from the camera's
parameter sets, not assumed.)

The parts are: profile space (empty, or `A`/`B`/`C`), profile, the
compatibility flags **in reverse bit order** as hex with leading zeros dropped,
then `L` for main tier or `H` for high tier followed by the level times thirty,
then up to six constraint-flag bytes with trailing zero bytes omitted. The
count of trailing components is therefore variable — `hvc1.1.6.L120.90` and
`hvc1.1.6.L120.90.00.00.00.00.00` name the same thing, and a parser that
requires a fixed number of dots will reject valid strings.

**The ask is that the platform treat this field as opaque** and hand it to the
browser unread. It exists to be passed to `MediaSource.isTypeSupported` and
`addSourceBuffer`; nothing in the relay needs to understand it, and the relay
is meant to be a byte pipe. If the platform does validate it, please accept
both prefixes and a variable tail rather than matching `avc1\.[0-9a-f]{6}`.

Two smaller consequences of the same change:

- **The `ftyp` compatible brands now include `hvc1` instead of `avc1`** on an
  HEVC session. A relay that forwards bytes will not notice. One that sniffs
  the init segment to decide anything will.
- **The station always says `hvc1`, never `hev1`.** The two differ in whether
  parameter sets may also appear in the samples; this muxer strips them into
  the configuration box, so `hvc1` is the accurate name as well as the stricter
  one, and it is the only one Safari plays.

### Where this actually belongs

`transport.md` gives the codec string only as a prose example
(`text {"codec": "avc1.640028"}`). That example now reads as the specification,
which is how it came to be assumed on both sides. **Proposal:** state the field
as "an RFC 6381 codec string for the track in the init segment that follows,
opaque to the platform", and mark the `avc1.…` value as illustrative.

`schemas/video.schema.json` is a **different channel** — MJPEG snapshots on
`gsu/{station_id}/video` — and its `format` enum (`["mjpeg"]`) is not the field
above. It is worth noting only because the two are easy to conflate: the live
stream has no schema at all, and its wire format is prose in `transport.md`.
That is fine while it is one paragraph; it is the reason this question exists.

### The console-side constraint, which is the real risk

**HEVC playback in a browser is hardware-dependent in a way H.264 is not**, and
the failure mode is silence. Roughly: Safari plays it; Chrome and Edge play it
only where the machine has a hardware HEVC decoder (and on some builds only
behind a flag); Firefox largely does not. The same station, the same stream,
two operators — one sees the site and one sees a black rectangle.

Worse, there are two distinct failures and only one of them announces itself:

- `MediaSource.isTypeSupported(...)` returns **false** and
  `addSourceBuffer` throws `NotSupportedError`. Catchable, reportable, fine.
- `isTypeSupported` returns **true**, the source buffer accepts every fragment,
  and nothing is ever decoded. No exception, no `error` event on the media
  element, no console warning. A black video element and a station reporting
  that it is streaming happily — which is the same signature as a dead camera,
  an unopened uplink and a wrong NAL header, and is why this is being raised
  before anyone is looking at it over a satellite link.

**The ask: a station streaming HEVC to a browser that cannot decode it must
fail visibly.** Concretely, three things that would do it:

1. **Check before opening.** The station already publishes the codec string in
   health telemetry — `video.codec` in the `health` payload, populated as soon
   as a session starts — so the console can call `isTypeSupported` and say "this
   camera streams H.265, which this browser cannot play" *instead of* opening a
   player. That is the cheap one and it catches the honest failure.
2. **Time out the silent one.** After the init segment and some fragments have
   been appended, `videoWidth` is still 0 and `currentTime` has not advanced —
   that is the case `isTypeSupported` lied about. A few seconds of nothing
   should become a message, not a black box.
3. **Say what still works.** The MJPEG snapshot channel is unaffected by any of
   this and keeps arriving. An operator told "live H.265 will not play in this
   browser; snapshots below are current" has lost a feature; one shown black has
   lost confidence in the site.

None of this is the station's to build, and all of it depends on the station
sending an honest codec string, which is now the thing it is most careful
about: an HEVC sequence parameter set that will not parse stops the stream with
a reason rather than being guessed at, precisely because a guess here is
invisible.

---

## 17. The station has stopped publishing `gsu/{station_id}/video`

**Raised by the station, and already implemented on this side.** This is not a
question about whether it is allowed; it is a statement of what the platform
will stop receiving, with the reasoning, so the server side can be changed
deliberately rather than discovering it as a channel that went quiet.

### What stopped

`gsu/{station_id}/video` is no longer published at all. Nothing on that topic:
no frames, and **no `available: false` frames either**. The station does not
open the channel, does not resolve the topic, and does not report a refusal on
it.

`contract/schemas/video.schema.json` is untouched and still describes exactly
what a station *would* send if it sent anything. The schema is still validated
against in `tests/test_video.py`. What has changed is that this station has no
producer for it.

Concretely, the platform stops receiving:

| Was arriving | Now |
| --- | --- |
| One base64 JPEG per message, 640×480 at 2 fps by default, ~30–60 kB each | Nothing |
| `available: false` with `unavailable_reason` when no camera answered, at up to 1 Hz | Nothing |
| `width`, `height`, `captured_at` per frame | Nothing |

And the health frame's `video` object has changed shape. Removed:
`enabled`, `fps_configured`, `fps_measured`, `frames_published`,
`frames_dropped`, `bytes_per_frame`, `bitrate_bps`, `captured_at`, `refused`.
Added: `snapshots` (always `false`), `snapshots_removed` (one sentence saying
why), `preview_frames` / `preview_refused` / `preview_failed`, `watching`, and
`sensor` — see below. `video.stream` and `video.camera` are unchanged.

**A console that renders zeros from the old fields will render zeros**, which
is also what a broken camera looks like. That is the reason `snapshots: false`
is an explicit field rather than an absence: the platform should key off it and
say "this station does not send snapshots", not "0 fps".

### Why, in one paragraph

The CSI camera is one device with one owner. The snapshot publisher and the
live encoder were both trying to be that owner, and four successive fixes — a
lock in the driver, a relinquish before the stream starts, a terminal
`retire()` on rediscovery, a 2.5-second drain to outlast an in-flight capture —
were each correct about the case they named and silent about the next one. The
station still wedged with `Camera in Acquired state trying acquire()` and a
38-second stream delivering zero frames. Removing one of the two readers does
not narrow that class of bug, it deletes it. The owner's own framing was the
same and arrived independently: *"lets just disable all snapshot functionality
for now so i can tell what is camera not working rather than actually just
snapshots"*.

### What replaces it, for the operator

Nothing, on the platform side — deliberately. The platform has the media
channel for live video, and a second, worse copy of the same picture at 2 fps
was being paid for on a metered link to duplicate it.

Locally, the setup page keeps its preview: `/frame.jpg` still serves the newest
frame with an `X-Frame-Age` header, unchanged. It is now sourced on demand —
one frame, taken only while somebody actually has the page open, and never
while the live stream holds the sensor.

### The three things the platform may want back, and what each would cost

1. **A still image on the fleet list.** This is the real loss: a console
   showing twelve sites cannot open twelve live streams. If that view matters,
   the honest shape is a **command**, not a channel — `video.snapshot`,
   answered with one frame, the way `video.start` is answered with a stream.
   One reader, on demand, arbitrated by the same sensor lease. The station can
   implement that; it is not implemented now because nothing has asked.
2. **`available: false` as a liveness signal.** Already available and better
   sourced: the health frame carries the camera slot in `devices[]`, and
   `video.camera.backend_reason` carries the driver's own sentence.
3. **Bandwidth accounting for video.** `video.stream` still reports measured
   bytes and bitrate for the live path, which is now the only path that costs
   anything.

### One field worth adopting: `video.sensor`

New in the health frame, and it is the answer to the question all four previous
fixes were circling and none could answer from telemetry:

```json
"sensor": { "holder": "the live stream", "held_for_s": 12.4,
            "grants": 31, "refusals": 2 }
```

`holder` is `null`, `"the live stream"` or `"the camera preview"`. A camera that
is *busy* and a camera that is *broken* have looked identical from the platform
for the whole life of this bug, and this is the field that separates them. Worth
surfacing wherever a camera fault would otherwise be raised.

### What the station needs from the platform

Nothing blocking. Two things to do at leisure:

- **Stop expecting the channel.** Any dashboard, retention job or alert keyed
  on `gsu/{station_id}/video` arriving should be retired or repointed. An alert
  on "no video frames for N minutes" will now fire permanently.
- **The broker ACL can drop the video topic**, if it was ever granted. Item 12
  asked for it to be added; this withdraws that ask. Leaving it granted is
  harmless — nothing publishes to it.

### Not withdrawn

The `video_topic` field in the enrolment response is still parsed and still
stored, and `contract/enrolment.md` is unchanged. Removing it would be a
breaking change to the enrolment contract to save one unused string, and if
item 1 above is ever built it is exactly the field that would carry the reply
topic.

## 18. A station cannot tell the platform where it is — and it is the only side that knows

**Where** `schemas/telemetry.schema.json` `$defs/health`; `enrolment.md` §4
(`station.latitude` / `station.longitude`) and §7.

**This one is a decision, not a question.** The owner's instruction, verbatim:
*"location should only come from the station, not be set on the server at
all."* The station half is built and shipped; what follows is the specification
the platform side is owed, written so it can be implemented without reading the
station's code.

**Why the position moved.** Two places could set it — the platform's station
record (`server/web/src/components/SettingsStation.tsx`, which renders latitude
and longitude as editable inputs) and, now, the station's own setup page. Two
editable copies of one fact disagree, and this pair disagrees *invisibly*: the
station computes `range_km` and `bearing` for every ADS-B contact from its copy,
the console draws the map and the range rings from the platform's, and neither
display says which number it used. An operator would see a contact reported at
8 km drawn where 12 km is. Only one of the two copies can be entered by somebody
standing at the site, so that is the one that lives.

### What the station now sends

`position`, on the health cadence rather than at enrolment, so that a position
corrected six months after commissioning arrives without anybody re-enrolling a
box:

```jsonc
{
  "kind": "health",
  // …
  "position": {
    "latitude": -42.4004,     // required in the object; -90..90
    "longitude": 173.68,      // required in the object; -180..180
    "elevation_m": 120,       // optional, metres; omitted when not set
    "source": "configured"    // "configured" = typed by a person on site.
                              // "gps" the day a receiver is fitted and has a
                              // fix (enrolment.md §6 already intends one).
                              // Treat an unrecognised value as "configured"
                              // rather than rejecting the object.
  }
}
```

**The whole object is omitted when the station has no position.** Not `0, 0`,
not a default, not the last value it held. Null island is in the Gulf of Guinea,
and a fleet map that quietly draws every unconfigured station there looks like
data rather than like a gap.

`elevation_m` has no platform equivalent at all today. It exists because range
to an aircraft is slant range, and the station's own height is the term nobody
can supply remotely.

### What the platform must do

1. **Accept and persist `health.position`** onto the station record. Last write
   wins — it is the station's own fact and there is nothing to reconcile it
   against.
2. **Stop offering latitude and longitude as editable fields.** Render what was
   reported, with its `source`, and say where it is set. A read-only pair that
   says "set on the station's setup page" is the whole change to that pane.
3. **Absent stays a supported state.** It already is: `AdsbMap.tsx` returns
   *"Station has no location set"* rather than a map when either coordinate is
   null. Keep exactly that. Do not substitute an org centroid, a country
   centroid or `0, 0` — a station nobody has been to must look like one.
4. **`enrolment.md` §4's `station.latitude`/`station.longitude` stay in the
   response.** Removing them would break stations already in the field. They
   become an echo of what the station last reported. The station reads them
   only as the fallback for a box enrolled before its setup page had these
   fields, prefers its own value whenever it has one, and labels the fallback
   as the platform's on screen so that nobody reads it as a confirmed position.
5. **Anything the platform computes from position** — range rings, distances,
   sorting by proximity — should use the reported position, so that it and the
   station's own `range_km` agree by construction rather than by luck.

### The one thing this cannot yet express: retraction

An omitted `position` means **"this station is not telling you"**, and the
platform should treat it as no change rather than as a clear. It has to, because
absence is also what every station running an older agent sends.

So a station that *had* a position and has had it cleared — a commissioning
correction, or a box being moved — cannot currently make the platform forget the
old one. The setup page is worded for exactly that limit: it says clearing
"stops this station reporting one", not that it clears it on the platform.

If that matters, the smallest thing that fixes it is an explicit null:

```jsonc
{ "kind": "health", "position": null }   // proposed: "I have no position",
                                         // as distinct from an absent key
```

Not being sent, because a `null` where the platform expects an object is the
kind of thing that throws on the other side, and this side does not get to
decide that. Say the word and it is a two-line change here.

### Why this is on the wire before it is in the schema

`$defs/health` sets no `additionalProperties: false`, and its own description
asks for precisely this: unknown fields there *"are expected rather than
tolerated: a station that learns to report something new must not have to wait
for the platform."* A health frame carrying `position` was validated against
`telemetry.schema.json` as it stands and passes — checked, not assumed. Adding
`position` to `properties` therefore documents a field that already conforms.
It is not a breaking change and nothing has to land on both sides at once.

## 19. Two ADS-B datapoints the aircraft object cannot hold as it stands

The aircraft object now carries everything the receiver reports, which was the
owner's instruction and is implemented: emitter type, squawk, altitude datum,
vertical speed, `tslc`, the simulated flag and the source band all reach the
wire, and each honours its validity flag as a null rather than a zero. Two
things did not fit, and both are small.

### `on_ground` is answerable for three emitter types and no others

`ADSB_VEHICLE` has **no airborne/surface status field**. There is no bit in the
message for it. The only ground evidence it carries is `ADSB_EMITTER_TYPE`: 17
(emergency surface vehicle), 18 (service surface vehicle) and 19 (point
obstacle) are surface categories by definition, and the station reports `true`
for those.

Every other contact reports `null`, and will keep reporting `null`. The
alternative is to infer it — "the altitude is above the station, so it is
airborne" — and the contact that inference gets wrong is an aircraft holding on
a taxiway at an aerodrome below the station, which is the one contact where
being on the ground is the interesting fact. So it is not inferred.

Nothing needs to change in the schema; `boolean | null` is already the right
type and the description already says "Null when unknown". This is a note that
**a console rendering a ground/air indicator from this field will see null for
essentially all traffic from a MAVLink receiver**, and should render "unknown"
rather than "airborne". It is declared in `devices/registry.py` as an `absent`
field for that entry so it also arrives in `unsourced_fields`.

A dump1090/SBS receiver is the other way round: SBS output has a real on-ground
flag and no emitter category. If that driver is ever written, the same station
will source the two fields from opposite ends.

### `ADSB_FLAGS_BARO_VALID` has nowhere to go

The receiver sends a ninth validity flag, `ADSB_FLAGS_BARO_VALID` (256), which
is a second and independent statement about the altitude alongside
`altitude_type`. It is decoded (`mavlink.AdsbVehicle.baro_valid`) and reaches
the setup page's datastream line, and stops there — the contract has no field
for it.

It is not obviously worth one. `altitude_type: "pressure"` is what the
correction is gated on, per the schema's own wording, and that is the right
gate. But it is the single datapoint the receiver provides that the station
still drops on the floor, and "all datapoints provided" was the instruction, so
it is recorded here rather than silently discarded.

If it is wanted:

```jsonc
{ "baro_valid": true }   // proposed: the receiver's own confidence in the
                         // barometric altitude, distinct from its datum
```

### A naming trap worth writing down

MAVLink names `ADSB_ALTITUDE_TYPE` entry 0 `ADSB_ALTITUDE_TYPE_PRESSURE_QNH`,
and its description says "using QNH reference". That is a misnomer in the
message definition. ADS-B airborne position messages carry barometric altitude
against the **standard 1013.25 hPa datum** (DO-260B), not against a local QNH.
The schema's own wording — *"`pressure` is referenced to 1013.25 hPa"* — is the
correct one, and the station's correction works from 1013.25 accordingly.
Anyone reading the MAVLink XML and concluding the altitude is already
QNH-referenced would conclude there is nothing to correct.

### And one field the receiver does not provide at all

`altitude_corrected_m` is not a receiver datapoint. It is computed on the
station from the Airmar's barometer and the station's configured elevation
(`gsu/devices/altitude.py`), is off unless site configuration switches it on,
and is null whenever the station cannot compute it honestly — no barometer, a
reading older than five minutes, an unset elevation, or an altitude that is
already geometric. Which of those applies is reported in the health frame as
`adsb_altitude_correction`, because a null on every contact otherwise has four
indistinguishable causes.
