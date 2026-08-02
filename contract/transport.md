# Transport

## Direction and channels

A station publishes upward on channels named for itself, and subscribes to one
channel for commands. It has no other route in or out — in particular it never
talks to a browser (`server/docs/00-topology.md`, rule 8).

| Direction | Channel | Payload |
|---|---|---|
| station → platform | `gsu/{station_id}/telemetry` | one telemetry object, JSON |
| station → platform | `gsu/{station_id}/audio` | one audio object, JSON |
| station → platform | `gsu/{station_id}/events` | one batch of events, JSON |
| platform → station | `cmd/gsu/{station_id}` | one command object, JSON |

`{station_id}` is the UUID the platform issued at enrolment. A station publishes
under its own id and nothing else; the broker ACL enforces that, so a compromised
station cannot publish into another tenant's namespace or read anything back.

**The platform resolves the organisation from the station id**, via its device
registry. Nothing in the payload says which tenant this is, and nothing should.

## What the platform does with what you send

| | |
|---|---|
| Station id | Taken from the **channel name**, never from the payload |
| Organisation | Resolved from the platform's own registry. A station id that is unknown or deactivated is dropped and nothing reaches any subscriber |
| Unknown `kind` | Dropped, as this contract promises, so a station may be newer than the platform |
| Malformed JSON | Dropped |
| Liveness | Derived from the fact that a station is publishing at all. **A station that stops publishing goes offline on its own** — there is no separate heartbeat to send |

**Authentication.** A station must be enrolled and hold a valid credential
before anything it publishes reaches a subscriber. Revoking one stops its data
**within thirty seconds**, whether or not the broker noticed, and that bound
holds on both transports.

**Authenticating once, at connect, is not enough on a link that stays up for
months.** Where the transport has its own identity layer, revocation removes it
and drops the station's connections; where it does not — the 443 relay, which
is one authenticated socket — each open socket re-checks the credential on a
timer and closes when it no longer stands. The media uplink does the same. Any
long-lived connection carrying a station credential owes this.

The broker's anonymous or default identity is closed, so a process that reaches
the port still has no identity. Per-station credentials are the second layer,
not the only one.

## Video

**There is no video channel.** `gsu/{station_id}/video` carried one MJPEG frame
per message and is gone, along with `schemas/video.schema.json`.

It was removed for a reason worth keeping: the camera is a single device with a
single owner, and a periodic snapshot publisher competing with the live encoder
for it is what wedged a real camera. Removing one of the two readers does not
narrow that class of fault, it deletes it.

Live video goes over the media WebSocket instead (`WS /media/ingest`), started
only while somebody is watching. A station may serve a preview to its own local
setup page so an installer can aim a camera; that publishes nothing and does not
cross this boundary.

A station's camera state — fitted or not, which capture path, who holds the
sensor — is reported in the health frame (`health.video`), which is where a
console should read it.

## The live video stream

Separate from the broker entirely: video is bulk data, and the broker carries
control and telemetry that must not be delayed by it.

```
station ──(outbound wss, station credential)──► platform ──(per viewer)──► browser
```

- **`wss://<platform>/media/ingest`**, authenticated with the station credential
  as a bearer token. The station id is derived from the credential, never sent.
- **Fragmented MP4**, not Annex B. The relay is then a byte pipe - it forwards
  fragments without parsing or re-muxing, so a second viewer costs a socket
  rather than a codec, and a browser plays it through Media Source Extensions
  with no player library.

The order of a session is fixed, and all three messages are required:

```
text    {"codec": "avc1.640028"}    once, before anything else
text    init                        a new encoder session starts here
binary  ftyp + moov                 the initialisation segment
binary  moof + mdat                 one per frame, from then on
```

- **The codec string is not optional and is not guessable.** Media Source
  Extensions cannot open a buffer without the exact string, so a viewer given
  only bytes decodes nothing and shows black - which is indistinguishable from
  a dead camera, and is why this frame comes first. Derive it from the
  stream's own sequence parameter set (ISO/IEC 14496-15 Annex E.3), never from
  configuration: `avc1.PPCCLL` for H.264, `hvc1.P.C.TLL.CC` for HEVC. A
  station that guesses is a station that reports a picture nobody can watch.
- **The first binary frame of a session is the initialisation segment**
  (`ftyp` + `moov`). The platform keeps it and gives it to every later viewer,
  because a viewer handed only the next fragment sees nothing at all. Send a
  text frame `init` to declare a new encoder session; the platform discards the
  old one, since parameters that no longer match decode as corruption rather
  than as an error.
- **Frames are capped at 512 KiB**, as on the broker relay.
- **Nothing reaches a viewer directly** (topology rule 8). The platform
  terminates the stream and re-originates it, so a viewer never learns a
  station's address and a station never learns a viewer's.

### On demand

The platform sends `video.start` when the first viewer attaches and `video.stop`
when the last one leaves, and **renews `video.start` while anyone is watching**.
The lease is what makes silence the stop signal: if the platform crashes or the
link drops, the station stops on its own rather than transmitting to nobody,
which is the whole point of on-demand on a metered link.

`video.start` carries `lease_seconds`. Treat a repeat as a renewal, not as a
second start. Both commands, and the identically shaped `radio.audio` lease for
audio, are defined in `schemas/command.schema.json`.

## Streams with no source

Send `available: false` with a short `unavailable_reason` rather than an empty
payload or silence. See `README.md`. Keep sending it on the normal cadence - a
station that goes quiet is a station that has failed, and "I have no receiver"
is something you have to keep saying.

## Transport security

Both channels are TLS. **All traffic between a station and the platform is
encrypted; there is no unencrypted path and no downgrade.**

The two channels are verified differently, and the distinction matters:

- **Broker** - verified as `broker.ca_mode` says. `"pinned"` means against
  `broker.ca_pem` and no other issuer, which is stronger than public PKI rather
  than weaker. `"system"` means this platform is behind a publicly trusted
  certificate, so its private CA is not what the station will be shown.
- **API** - normally behind a TLS-terminating reverse proxy with a public
  certificate, so verified against the system trust store. A station may pin the
  API to a private CA instead where there is no proxy, but that is configuration
  rather than the default.

The field is `broker.ca_pem` and not `ca_pem` because it is the *broker's* trust
root. Using it for the API works today and stops working the moment a real
certificate is in front.

**The mode is stated, never inferred.** A missing `ca_pem` cannot distinguish
"none sent yet" from "use the public roots", and a station that guesses is wrong
in one direction or the other — refusing to connect at all, or widening its
trust on its own. An absent or unrecognised `ca_mode` means `"pinned"`, so the
station with nothing to pin refuses rather than downgrades, and a pinned CA
always outranks a stated mode.

Plaintext listeners are **disabled**, not deprioritised. A station pointed at a
plaintext scheme fails to connect rather than quietly sending its credential in
clear, because a silent downgrade is the failure nobody finds out about.

**`broker.url` carries no credentials**, and a station must not accept one that
does. Identity comes from `broker.username` and the station's own secret. This
is a requirement rather than a convention because at least one widely used
client library lets credentials in a URL silently override the ones passed
alongside it — see `NOTES.md`.

## Broker

A WebSocket relay on 443 (`WS /broker`) is the deployment transport; a direct
pub/sub connection is available on a bench. The port is the whole reason: 6380
and 8883 are shut wherever a reverse proxy is, and 443 is not.

### The relay's wire format

```
wss://<platform>/broker            Authorization: Bearer <credential>

  ->  {"topic": "gsu/{id}/telemetry", "payload": { … }}   one JSON text frame
  <-  {"topic": "cmd/gsu/{id}",       "payload": { … }}   commands, unrequested
  <-  {"type": "refused", "topic": "…", "reason": "…"}    a topic you may not use
```

- **The station id is never sent.** It is derived from the credential, as
  everywhere else. A box holding a valid secret still cannot say which station
  it is.
- **There is no subscribe handshake.** Commands arrive on the same socket from
  the moment it opens, because the credential already determines the one
  command channel this station may receive. A station that tries to subscribe
  is refused.
- **`refused` is a frame, not a disconnection.** Publishing to a topic outside
  the enrolment response's three gets one of these and the socket stays up —
  a station silently dropping everything it publishes looks exactly like a
  station with nothing to say, and this is the fault most likely to be a
  misconfiguration.
- **Frames are capped at 512 KiB**, both directions, enforced by closing the
  socket (1009). A station that needs to send more than that is wrong about
  something; telemetry is current state.
- **Close code 4401** means the credential was refused — at connect, or later,
  because it is re-checked every 15 s on the open socket (see *Authentication*).

Both transports are fire-and-forget from the station's point of view, and the
contract assumes nothing stronger:

- **Telemetry may be dropped.** It is a stream of current state, not a ledger.
  Publish the current value on a fixed cadence rather than publishing changes
  and assuming they arrive.
- **Commands may be missed.** A command is a request, not a guarantee. The
  platform confirms nothing itself: it publishes the command and waits to see
  the change reflected in the station's own telemetry. That is why every
  command has a corresponding field in a telemetry payload — if you add a
  command, add the field that reports its effect.
- **Ordering is per channel only**, and not relied upon.

The events channel is the exception to the first of these, and only to the
first — see *Store and forward*.

## Identity

Each station authenticates with its own credential — a bearer token today, with
mTLS client certificates still to come (`enrolment.md` §3) — and that identity
is granted **exactly the four channels** in the table above, by name. Not a
`gsu/{station_id}/` prefix: a prefix would also admit channels nobody consumes,
and a station inventing its own on a shared broker is what the check exists to
prevent. **A new channel is a change to the contract and to the grant,
together**, and a topic granted in one place and not the other produces a
station that enrols perfectly and publishes nothing.

Where the transport can distinguish publish from subscribe, it must: a station
publishes to its three upward channels and only receives on its command
channel. Some brokers cannot express that in a grant, in which case the
constraint is enforced by whatever terminates the connection.

## Cadence and bandwidth

These sites are on metered, intermittent links. The platform's console is built
to tolerate gaps, so favour dropping data over queueing it.

| Stream | Cadence | Notes |
|---|---|---|
| `adsb` | 1 Hz | Full current picture each time, not deltas. The costliest un-gated stream — see below |
| `power` | 1 Hz | |
| `radio` | 1 Hz | |
| `light` | 1 Hz | |
| `weather` | 0.2 Hz | Changes slowly. A metered site may set this lower — see below |
| `health` | every 30 s | The station's own state. Slow by design |
| `audio` | while squelch is open, and only while asked | See below |

**What a station costs when nothing is happening.** The four 1 Hz streams plus
weather and health come to roughly **5–6 kbit/s, about 2 GB a month**, per
station, for ever. That is the floor the design accepts in exchange for the
console never having to guess whether a silent station is a failed one. It is
worth knowing before adding a field to a 1 Hz payload: anything that rides
`radio` or `power` is paid for 86,400 times a day whether it changed or not,
which is why capability constants belong in `health` and not beside the
readings.

**A station reports the cadences it is actually using**, in `health.cadence`,
as seconds per stream. Consumers deriving a timeout — "this panel is stale" —
must use that rather than the table above, which is the default and not a
promise. `weather_period_s` is a site setting and is settable at runtime, so a
station legitimately slowed down to save bandwidth would otherwise be marked
faulty by a console still assuming 0.2 Hz, with nothing at either end to say
why. Absent on an older agent, which means the table.

**ADS-B is the largest thing nobody gates**, and it is worth stating because
the two streams the contract is careful about are both cheaper than it in the
cases that matter. A contact serialises to roughly 400 bytes, so a 50-contact
picture at 1 Hz is about **150 kbit/s, or 1.5 GB a day** — a third of live
audio, continuously, whether or not a single console is open. Most of that is
repeated JSON key names, and none of it is compressed on the wire.

Three things follow, none of which needs a schema change:

1. **Omit what has no value.** Only `icao`, `range_km` and `bearing_deg` are
   required. Sending explicit nulls for the other sixteen fields is 10–20% of
   the stream, and the weather payload already omits rather than nulls.
2. **The array is capped at 500 contacts** — far above any real receiver, and
   there so one station cannot hand every console an unbounded array to
   render. A station seeing more should send the nearest 500.
3. **Full picture, not deltas, stays right.** It is what makes the stream
   droppable on a link that drops, which is the whole cadence model. The cost
   is the price of that property, not an oversight — but it is a real cost and
   the contract should not price two streams and stay quiet about the biggest.

**Audio is the platform's largest single consumer while it is flowing** and the
one place where being careless is expensive. Three things keep it manageable,
and all three are required:

1. **Send only while the gate is open.** Airband is silent most of the time, so
   this alone is most of the saving.
2. **Send only while somebody is listening.** The platform sends `radio.audio`
   with a `lease_seconds`, renews it while a console holds the subscription,
   and **stops saying anything at all** when the last one goes. The lease
   expires on its own, so audio stops when the platform goes away rather than
   when it remembers to say so — the same shape as `video.start`, and for the
   same reason: most listeners never say goodbye. A closed laptop, a dropped
   link, a revoked session and a shut tab all look identical to somebody still
   listening, right up until the lease runs out.

   A station that has never been asked sends no audio. **Recording is not
   gated by this** — a transmission nobody had a console open for is still
   written to disk, exactly as one during an outage is.
3. **Opus, at 16–24 kbit/s.** Uncompressed 24 kHz 16-bit mono is 384 kbit/s and
   base64 in a JSON envelope made it ~512; Opus is transparent for voice at a
   twentieth of that. Whole packets travel base64'd in an array —
   `schemas/audio.schema.json` — which keeps the envelope JSON like everything
   else on this transport and still gets the order of magnitude. Binary frames
   would save the remaining third and are a transport change rather than a
   schema one, so they are not in this version.

   **This is a change from the format that shipped first**, which was base64
   PCM. It was replaced before this contract was fixed rather than after,
   because the alternative was freezing the most expensive payload on the link
   in the shape everybody already knew was wrong, and paying for a breaking
   change later to fix it.

## Timings, and who owns each one

Every number both sides must agree on, in one place. These are the values that
are invisible in a schema — a shape can be validated and a lifetime cannot —
and each one is somewhere two independently built ends would otherwise each
choose something reasonable and not match.

| What | Value | Owned by |
|---|---|---|
| `radio.audio` lease, when `lease_seconds` is absent | 30 s | station default |
| `video.start` lease, when the platform states none | 30 s | station default |
| `radio.spectrum` lease, when `lease_seconds` is absent | 15 s | station default |
| Any lease the station will honour | clamped to **5–300 s** | station |
| Renewal, for every lease | at or before **one third** of the lease | platform |
| `radio.monitor` releases itself after | 300 s | station |
| Events per message | at most 100 | station |
| Event batches in flight at once | 1 | station |
| Credential renewal begins | `credential.renew_after`, as issued | platform states it |
| Revocation takes effect within | 30 s | platform |

Four rules go with the table, because the numbers alone do not settle it.

**A lease the platform states always wins, inside the clamp.** The defaults
above are what a station uses when told nothing. They are not a ceiling and not
a negotiation: a platform asking for 45 s gets 45 s.

**The clamp is the station's, and it is not silent.** 5–300 s exists so a
platform that has gone wrong cannot pin an unattended box's uplink open for a
day, or make it stutter with a two-second lease. A station that clamps is
already reporting the truth in `health.video.stream.lease_remaining_s`, which
is where a platform sees that its 3600 became 300.

**Renew at a third, not at the edge.** A platform that renews exactly when the
lease expires produces a gap on every cycle, because the renewal has to cross a
link that drops. A third means two consecutive renewals can be lost before a
listener hears anything, which on these links is the point.

**Stopping is never only a command.** `video.stop` and `radio.spectrum {on:
false}` end things sooner as a courtesy, and the lease running out is the
guarantee. A station that never receives a stop still stops. This is why
`radio.audio` has no stop at all — see its schema entry.

## Store and forward

A station must keep working with no link at all: continue sensing, recording and
locally alerting, and reconcile when the link returns. **Buffer events and
recordings; drop stale telemetry.** Replaying an hour of old readings into a
live console is worse than a gap, because the console shows *current* state and
has no way to tell it is being shown the past.

That split is why there are two upward channels with opposite rules, and it is
the one place in this contract where "may be dropped" does not apply.

### Events, which are the exception to everything above

`gsu/{station_id}/events` carries things that happened —
`schemas/events.schema.json`. A telemetry frame has a newer version a second
later; an event does not. A transmission at 03:12, a proximity alert, a
floodlight drawing no current: lose the message and the fact is gone.

So this channel alone is **acknowledged**:

1. The station records events to local storage that survives a reboot, each
   with a stable `id` and a monotonic `seq`.
2. It publishes them oldest first, **at most 100 per message**.
3. The platform stores the batch durably and replies on the command channel
   with `events.ack {through_seq}`.
4. The station deletes up to that seq, and sends the next batch. It does not
   run ahead of the acknowledgement — one batch in flight at a time, so a
   station reconnecting after a week does not arrive as a flood.
5. Anything unacknowledged is re-sent, with backoff, for ever.

**Delivery is at-least-once and never exactly-once.** An acknowledgement can be
lost after the platform has stored the batch, and the station will then send it
again; that is correct behaviour, not a fault. **Consumers deduplicate on `id`**
— which is why `id` is a UUID rather than a counter, and why it is separate from
`seq`. A station whose local store is rebuilt starts `seq` again from zero, and
a platform that had deduplicated on a counter would silently discard real events
from a station it recognised.

**A timestamp from an unsynchronised clock is marked as one.** Events carry
`clock: "synced" | "unsynced"`, because a box with no battery-backed clock that
has not reached a time source produces times that are internally consistent and
absolutely wrong. An event log that cannot say so is worse than one with gaps:
somebody will read those times as fact (enrolment.md §6).

**The platform must not raise stale events as live alerts.** A batch arriving
after an outage is history, and `at` says when it happened. Waking an operator
at nine in the morning for a proximity alert from three in the morning is how a
station's backlog becomes a reason to turn alerting off.

`health.storage.events_pending` is the backlog, and is the number to watch: it
rising during an outage is the system working, and it staying high while the
uplink is up is a delivery fault.
