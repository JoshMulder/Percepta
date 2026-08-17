# Transport

## Direction and channels

A station has four streams on one authenticated socket, and no other route in
or out — in particular it never talks to a browser
(`server/docs/00-topology.md`, rule 8).

| Direction | Stream | Code | Payload |
|---|---|---|---|
| station → platform | telemetry | `t` | one telemetry object, JSON |
| station → platform | audio | `a` | one audio object, JSON |
| station → platform | events | `e` | one batch of events, JSON |
| platform → station | commands | `c` | one command object, JSON |

**A station never names a channel, and never sends its own id.** The socket is
authenticated, so the credential already says which station this is; the one
letter says which of its four streams a frame belongs to. The platform maps
that onto whatever per-station channels it uses internally, and those names are
its own business.

This is worth stating as a property rather than an optimisation. There is no
field in which a station could name another tenant's channel, so the whole
class of fault where one is *told* a topic it is not *granted* — a station that
enrols perfectly and publishes nothing, with no error at either end — cannot
occur. It is also the cheapest byte saving available here: the id is 36
characters, sent four times a second, for something the far end derives from
the credential anyway.

**The platform resolves the organisation from the authenticated identity**, via
its device registry. Nothing in the payload says which tenant this is, and
nothing should.

## What the platform does with what you send

| | |
|---|---|
| Station id | Derived from the **credential** on the socket, never from anything in the frame |
| Organisation | Resolved from the platform's own registry. A station id that is unknown or deactivated is dropped and nothing reaches any subscriber |
| Unknown `kind` | Dropped, as this contract promises, so a station may be newer than the platform |
| Malformed JSON | Dropped |
| Liveness | Derived from the fact that a station is publishing at all. **A station that stops publishing goes offline on its own** — there is no separate *application* heartbeat to send. The socket underneath still needs its own liveness check; see *Reconnect* |

**Authentication.** A station must be enrolled and hold a valid credential
before anything it publishes reaches a subscriber. Revoking one stops its data
**within thirty seconds**, whether or not the broker noticed, and that bound
holds on both transports.

**An expired credential closes the socket, and does not publish.** Enrolment
grants a credential seven days past expiry for *renewal only* (`enrolment.md`
§4), so that a station back from a fortnight offline is not stranded. That
grace does not extend here: the relay refuses an expired credential at connect
and closes an open socket when one expires under it, exactly as for a revoked
one. This is stated in both documents because it is one `authenticate()` shared
between the renewal endpoint and the relay, and a relay built only from this
page would otherwise inherit the grace by accident and let a seven-day-dead
credential publish.

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

The order of a session is fixed, and every message below is required:

```
text    {"codec": "avc1.640028"}    JSON, before each init
text    init                        exactly these four bytes, not JSON
binary  ftyp + moov                 the initialisation segment
binary  moof + mdat                 one per frame, from then on
```

`init` is the literal four-byte string, **not** a JSON document and not a
quoted string — it is the one text frame on this socket that is not JSON. A
platform matching it exactly will silently ignore `"init"`, so a station that
quotes it streams fragments against a stale initialisation segment for ever.

**Send `codec` before every `init`, not once per socket.** A new encoder
session can change resolution or level, which changes the codec string, and a
viewer given the old one decodes nothing.

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

### The station decides the picture, not the platform

**`video.start` carries no bitrate, resolution or frame rate, and will not.**
The station streams what its camera is configured for and what its link will
carry — nothing here caps it, and there is deliberately no field for the
platform to ask for less.

That is a decision rather than an omission, and the reasoning is that the
platform is the wrong party to hold it. It cannot see the camera's
configuration, cannot see what the link is doing minute to minute, and would be
negotiating a number it has no way to verify against a station that already
knows both. The station is where both facts live, so the choice lives there
too.

Two rules make it safe to leave it there. The stream is **on demand**, so the
expensive case only exists while somebody is watching. And back-pressure is
**drop, never queue** — a station that cannot push its configured bitrate sends
fewer fragments rather than buffering a picture that is already out of date,
and says so in `health.video.stream.dropped`.

What the platform gets instead of a lever is the truth: `bitrate_bps`,
`fps_measured` and `dropped` in every health frame, beside the `requested`
settings, so an operator can see what a site is actually costing and change it
where it is configured.

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
- **Media uplink** (`media_url`) - **verified exactly as the API is**, because
  it is the same host and the same certificate in every deployment this
  contract describes. It is *not* covered by `broker.ca_mode`: pinning the
  broker's private CA against a publicly-certificated media host fails every
  stream, and trusting the public roots against a private one fails the same
  way in the other direction. A station reports what it did under
  `health.security.api_trust`, which covers both.

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
does. Identity travels in the `Authorization` header and nowhere else. This is
a requirement rather than a convention because at least one widely used client
library lets credentials in a URL silently override the ones passed alongside
it — see `NOTES.md`.

## Broker

**The WebSocket relay on 443 (`WS /broker`) is the transport.** The port is the
whole reason: 6380 and 8883 are shut wherever a reverse proxy is, and 443 is
not. Build against it.

A direct pub/sub connection to the broker is sometimes available on a bench,
for tooling that predates the relay. **It is not specified here and a station
is not required to speak it** — this document previously mentioned it in a way
that read as a second supported transport, which left a station author to
infer a protocol, an authentication scheme and a subscription model from a port
number. If a deployment ever needs one, it is a contract change like any other.
A station decides from the scheme of `broker.url`: anything but `wss://` is a
URL this version does not define.

### The relay's wire format

```
wss://<platform>/broker            Authorization: Bearer <credential>

  ->  {"stream": "t", "payload": { … }}                 telemetry
  ->  {"stream": "a", "payload": { … }}                 audio
  ->  {"stream": "e", "payload": { … }}                 an events batch
  <-  {"stream": "c", "payload": { … }}                 commands, unrequested
  <-  {"type": "refused", "stream": "…", "reason": "…"}  a stream you may not use
```

- **Two keys, and neither is an identity.** `stream` is one of the four codes
  in the table above; `payload` is the object. A station sends nothing else,
  and there is nowhere for it to name a station, a channel or a tenant.
- **There is no subscribe handshake.** Commands arrive from the moment the
  socket opens, because the credential already determines whose they are. A
  station that tries to subscribe is refused.
- **`refused` is a frame, not a disconnection.** Sending an unknown or
  wrong-direction stream code — `c` upward, say — gets one of these and the
  socket stays up. A station silently dropping everything it publishes looks
  exactly like a station with nothing to say, and this is the fault most
  likely to be a misconfiguration.
- **Match a downward frame on `type` before `stream`.** Both downward frames
  carry a `stream` key and only the command carries `payload`, so a station
  dispatching straight on `stream` hits a missing `payload` on the refusal —
  and the refusal it is most likely to provoke is for publishing on `c`, which
  arrives as `stream: "c"` and looks exactly like a command. The frame written
  to explain a misconfiguration would instead crash the station that made it.
  A frame with a `type` is a control frame; anything else is a command.
- **Frames are capped at 512 KiB**, both directions, enforced by closing the
  socket (1009). A station that needs to send more than that is wrong about
  something; telemetry is current state. The cap is on the reassembled message,
  not on one fragment.
- **Both ends reassemble fragments, and neither may assume a message arrives
  whole.** RFC 6455 permits splitting a message unconditionally and most
  libraries do it on large ones without being asked, so this is not an exotic
  case — it is the default behaviour of the thing on the other side. A receiver
  that drops continuation frames loses whole messages silently and in one
  direction only, which reads as an intermittent peer rather than a bug. A
  control frame may arrive *between* fragments and must not disturb the message
  being assembled.
- **Close code 4401** means the credential was refused — at connect, or later,
  because it is re-checked on the open socket (see *Authentication*). A station
  attempts a renewal once, then reconnects on the schedule below; it does not
  give up, because 4401 covers both "revoked, and will never work again" and
  "expired while you were offline, and renewal will fix it", and a station that
  stopped on the first would need a site visit after a transient.
- **At connect, that means completing the handshake and *then* closing 4401 —
  never rejecting the upgrade with HTTP 401.** There is no socket to carry a
  close code until the handshake finishes, so a platform that authenticates
  before upgrading, which is what most libraries and every reverse proxy do by
  default, leaves the station nothing to react to. The renewal above is the
  whole recovery path for a box whose credential expired while it was offline;
  drop the 4401 and that box reconnects on a five-minute backoff for ever and
  never calls `/renew`. The seven-day grace in `enrolment.md` §4 exists to
  avoid exactly that site visit, and this is the one frame that reaches it.
- **A second socket on the same credential supersedes the first, and the
  platform closes the older one.** This is not a hypothetical: the ping rule
  below has a station reconnect the moment its link goes quiet, and the
  platform cannot distinguish the socket that died from one that is merely
  having a quiet minute — so for a while it holds both. Commands and
  `events.ack` must go to exactly one of them. Send them to the older and the
  station on the newer never sees its acknowledgement, re-sends the same batch
  for ever, and the platform stores it again on every round. Refusing the new
  socket instead is equally readable from "one authenticated socket" and is
  worse: it strands a live station behind a zombie connection until something
  times out.

**Reconnect on exponential backoff with jitter**, from about a second to a
five-minute ceiling. The jitter is not politeness: without it a platform
restart brings every station back in the same second, each opening a socket and
each beginning to drain an event backlog, which is a self-inflicted second
outage. Stagger the first event batch after a reconnect by a random delay too —
the data is already hours old and nothing is gained by every station in a
region delivering at once.

**Both ends send WebSocket pings, and both must answer one.** Ping after about
twenty seconds with nothing sent or received; treat no pong within ten as a
dead socket and reconnect on the backoff above. This is not the heartbeat the
liveness row rules out — that one is about whether a *station* is alive, and it
is still derived from publishing. This is about whether the *socket* is, and
nothing else can tell you. A station on CGNAT whose NAT mapping is dropped goes
on publishing into a hole indefinitely: nothing arrives downward on a healthy
link either, because commands are unrequested and a quiet hour is normal, so
without a ping there is no signal a station could possibly use. The platform
has the same hole in the other direction — it marks the station offline within
seconds and then keeps writing commands, including `events.ack`, into a socket
nobody is reading. A platform closes a socket it has stopped hearing from on
the same rule.

**A receiver imposes a parse depth limit before validating anything.** JSON
Schema cannot express nesting depth, so this is a rule rather than a
constraint: a 30 KB frame of deeply nested objects exhausts a recursive parser
*before* the malformed-JSON path can drop it, and the malformed-JSON path is
what this contract otherwise relies on. A few hundred levels is far beyond any
legitimate payload here. Note that the resulting error is not always the
parser's usual one — Python raises `RecursionError`, which is not a
`ValueError` — so a handler that catches only malformed-JSON exceptions still
dies.

**A receiver rejects the non-finite tokens `NaN`, `Infinity` and `-Infinity`.**
They are not JSON (RFC 8259 has no such literals) but several parsers accept
them by default, and one of them defeats every numeric bound in these schemas:
**every comparison against NaN is false**, so a NaN satisfies `minimum`,
`maximum` and `exclusiveMinimum` at once and validates anywhere a number is
allowed. Infinity is caught by any `maximum`; NaN is caught by nothing.

The consequences are worse than a bad reading. Re-serialising a NaN emits the
same non-JSON token, so one station's frame breaks `JSON.parse` in every
console in that organisation. And every threshold comparison against it is
false, so a NaN `soc_pct` never trips a low-battery alert — it does not read
as a wrong value, it reads as a value that is never a problem. Reject at the
parser, alongside the depth limit; there is no schema keyword that will do it
for you.

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

  **A station that has ever acted on a command must include that field from
  then on**, even where the schema marks it optional. Optional means a station
  that has no such hardware need not invent one; it does not mean a station may
  obey silently. A station that changes state and does not report it is
  indistinguishable from one that ignored the command, which is the single
  failure this whole arrangement exists to make visible.
- **Ordering is per channel only**, and not relied upon.

The events channel is the exception to the first of these, and only to the
first — see *Store and forward*.

**The schemas describe payloads; they are not an accept/reject gate.** Both
`telemetry` and `command` discriminate with a closed `oneOf`, so a 2.0 schema
rejects a kind added in 2.1 — which is correct as a *definition* of 2.0 and
wrong as a filter, because this contract promises unknown kinds are dropped
and unknown commands ignored rather than erroring. A receiver validates what it
recognises and passes over what it does not. Resource limits are enforced at
the transport — frame size, parse depth — not by the schema, which is why those
limits are stated here rather than left to a `maxLength`.

## Identity

Each station authenticates with its own credential — a bearer token today, with
mTLS client certificates still to come (`enrolment.md` §3) — and that identity
is what the platform resolves everything from. A station is confined to
**exactly the four streams** in the table above and cannot express anything
else: there is no field for a channel name, so the confinement is structural
rather than checked. That closes a fault this contract used to carry — a topic
granted in one place and not another produced a station that enrolled
perfectly and published nothing, with no error at either end.

Direction is enforced too. `t`, `a` and `e` are upward only and `c` is
downward only; a station sending `c` is refused. **A new stream is a change to
the contract, to the code table, and to whatever the platform grants behind
it** — all in the same commit.

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
weather and health come to **6.6–9.0 kbit/s, about 2.1–2.9 GB a month**, per
station, for ever — measured on the wire, which is what a metered link bills.
Roughly 45% of that is not payload at all: the relay envelope is 25 bytes and
WebSocket, TLS and TCP framing add another 70–82, against payloads of 52–369
bytes. A fully-populated health frame is the largest single message at about
1.8 kB, and is not in that range.

Dropping the station id from the envelope took 48 bytes off every message —
**0.53 GB per station per month, between a fifth and a quarter of the floor
above.** That is what the one-letter stream code bought.

**And that is the clear-airspace number.** It assumes an empty `aircraft`
array. A site that can see ten contacts costs about **41 kbit/s, 13 GB a
month**; fifty contacts is 53 GB. The floor is what a station costs when
nothing is happening, not what it costs.

Worth knowing before adding a field to a 1 Hz payload: anything that rides
`radio` or `power` is paid for 86,400 times a day whether it changed or not,
which is why capability constants belong in `health` and not beside the
readings — `radio.gains` alone is a fixed hardware list costing ~1.2 kbit/s,
more than a tenth of the whole floor.

**A station reports the cadences it is actually using**, in `health.cadence`,
as seconds per stream. Consumers deriving a timeout — "this panel is stale" —
must use that rather than the table above, which is the default and not a
promise. `weather_period_s` is a site setting and is settable at runtime, so a
station legitimately slowed down to save bandwidth would otherwise be marked
faulty by a console still assuming 0.2 Hz, with nothing at either end to say
why. Absent on an older agent, which means the table.

**ADS-B is the largest thing nobody gates.** A fully-populated contact is about
366 bytes, so a 50-contact picture at 1 Hz is **153 kbit/s, or 1.66 GB a day** —
continuously, whether or not a single console is open. Most of that is repeated
JSON key names, and none of it is compressed on the wire.

Three things follow, none of which needs a schema change:

1. **Omit what has no value**, which is worth far more than it sounds. Only
   `icao`, `range_km` and `bearing_deg` are required, and a contact with nine
   real values is **161 bytes omitted against 356 nulled** — so omitting saves
   **45–55% of the stream**, and a well-behaved station's 50-contact picture is
   about 68 kbit/s rather than 153. The weather payload already omits rather
   than nulls. (An earlier version of this said 10–20%, which was out by five
   times and made the cheapest saving here look marginal.)
2. **The array is capped at 500 contacts — a render bound, not a bandwidth
   one.** It exists so one station cannot hand every console an unbounded array
   to build DOM for. In bandwidth terms it bounds almost nothing: 500 populated
   contacts is ~1.5 Mbit/s, ten times the figure above. A station seeing more
   should send the nearest 500.
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
3. **Opus, at 16–24 kbit/s, batched.** Uncompressed 24 kHz 16-bit mono is
   384 kbit/s and base64 in a JSON envelope made it ~512. Whole packets travel
   base64'd in an array — `schemas/audio.schema.json` — which keeps the
   envelope JSON like everything else on this transport.

   Measured on the wire at 24 kbit/s with 8 packets of 20 ms: **43 kbit/s, an
   11–12× saving.** A twentieth is reachable only at 16 kbit/s and the maximum
   batch, which costs two seconds of latency on a push-to-talk channel. **The
   batch size is what decides this**: the per-message overhead is a fixed
   ~198 bytes against a 60-byte packet, so one packet per message would cost
   112 kbit/s to carry a 24 kbit/s codec and throw most of the saving away.

   The schema enforces a minimum of four packets, but **what matters is
   duration, not count** — four 10 ms packets is 74 kbit/s, still three times
   the codec. Send at least 80 ms of audio per message. JSON Schema cannot
   express packets × `frame_ms`, so that one is a rule.

   Binary frames would save a further third and are a transport change rather
   than a schema one, so they are not in this version.

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
| `video.start` lease, when the platform states none | 10 s | station default |
| `radio.spectrum` lease, when `lease_seconds` is absent | 15 s | station default |
| `video.poster` lease, when the platform states none | 180 s | station default |
| `video.poster` interval, when the platform states none | 60 s | station default |
| Any lease the station will honour | clamped to **5–300 s** | station |
| …except `video.start`, clamped to | **5–30 s** | station |
| Renewal, for every lease | at or before **one third** of the lease | platform |
| `radio.monitor` releases itself after | 300 s | station |
| Events per message | at most 100, **and at most 128 KiB** | station |
| Event batches in flight at once | 1 | station |
| Wait for `events.ack` before re-sending | 30 s | station |
| Event re-send backoff | 30 s doubling to a 15 min ceiling, jittered | station |
| Reconnect backoff | ~1 s doubling to a 5 min ceiling, jittered | station |
| WebSocket ping, after nothing sent or received for | 20 s | both ends |
| Pong deadline, after which the socket is dead | 10 s | both ends |
| Credential renewal begins | `credential.renew_after`, as issued | platform states it |
| Credential overlap after renewal | 24 h | platform |
| Revocation takes effect within | 30 s | platform |

Five rules go with the table, because the numbers alone do not settle it.

**The poster lease is the long one, for the mirror image of video's reason.**
Video's lease is short because the tail after an abandoned view costs
megabytes. A poster costs about 20 kB a minute, so the tail is worth
approximately nothing — and a lease shorter than the interval would be actively
wrong, because it would expire between captures and make every single capture a
cold start against a camera that had just been released. Three intervals is the
smallest number that survives two lost renewals, which is the same one-third
rule the rest of the table follows, read from the other end.

**And the poster is the one lease a station is expected to refuse.** Every other
command here is a request the station carries out if it can; this one it
declines on its own authority when its battery is low, because a capture is
standing load and standing load is what browns out a solar site. It accepts the
lease and reports the refusal in `health.video.poster.reason`, which can begin
and end in the middle of a lease — a refusing station is not a broken one, and
the platform must not treat it as one.

**Video's lease is the short one because video is the expensive one.** Every
other stream costs tens of kilobits per second and video costs megabits, so the
tail after a viewer vanishes is worth an order of magnitude more than anywhere
else: at 3 Mbit/s a 30-second lease throws away up to 11 MB per abandoned
view, and 10 seconds up to 4. The renewals that buys — one every 3.3 seconds,
a few hundred bits — are free by comparison. Size a lease against what it
gates, not for symmetry. (Those are worst cases; the average tail is five
sixths of the lease, because renewal happens at a third.)

**Video gets its own clamp ceiling for the same reason.** A default of 10 s
bounds nothing if the platform may state 300, so the tail argument above only
holds because a station refuses anything longer than 30. This is the one place
the shared 5–300 s clamp is wrong: it was sized for the cheap streams.

**The cost of the short lease, stated rather than discovered.** Renewals ride
the broker socket, not the media one, so a 10-second lease means video stops
during any broker interruption longer than about 6.7 seconds — where a
30-second lease survived 20. Recovery is not free either: the encoder restarts,
which means a new `codec` frame, a new initialisation segment, and a buffer
reset for every attached viewer. On links this document says drop routinely
that turns some ordinary blips into visible restarts, and it is the price of
not paying for abandoned streams.

**A lease the platform states always wins, inside the clamp.** The defaults
above are what a station uses when told nothing. They are not a ceiling and not
a negotiation: a platform asking for 45 s gets 45 s.

**A renewal replaces the remaining lease; it never extends it.** A repeat
carrying 5 s leaves five seconds to run even if ninety were left, and this is
the only way a platform can stop audio early — `radio.audio` has no stop
command by design, so shortening the lease *is* the off switch when the last
listener closes their console. A station that takes the maximum of the old and
the new instead removes that lever entirely: audio keeps flowing to nobody for
up to the full clamp on a metered link, and the platform sees a
`lease_remaining_s` it never asked for with no way to tell an extension from a
clamp. Both readings are defensible from the word "renewal", which is why the
choice is written down here rather than left to be discovered.

**The clamp is the station's, and it is not silent.** 5–300 s exists so a
platform that has gone wrong cannot pin an unattended box's uplink open for a
day, or make it stutter with a two-second lease. A station that clamps is
already reporting the truth in `health.video.stream.lease_remaining_s`, which
is where a platform sees that its 3600 became 300.

**Renew at a third, not at the edge.** A platform that renews exactly when the
lease expires produces a gap on every cycle, because the renewal has to cross a
link that drops. A third means **one renewal can be lost with a third of the
lease to spare** — ten seconds on a 30-second lease, 3.3 on video's ten — and a
second loss puts the next attempt exactly on the expiry, which is a race rather
than a margin. If a stream ever needs to survive two consecutive losses
properly, renew at a quarter; the traffic is negligible either way. Check this
arithmetic against the lease you are actually holding rather than repeating the
number: it has been wrong here twice, once claiming two losses and once quoting
a margin that belonged to a different lease.

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

Stream `e` carries things that happened —
`schemas/events.schema.json`. A telemetry frame has a newer version a second
later; an event does not. A transmission at 03:12, a proximity alert, a
floodlight drawing no current: lose the message and the fact is gone.

So this channel alone is **acknowledged**:

1. The station records events to local storage that survives a reboot, each
   with a stable `id` and a monotonic `seq`.
2. It publishes them oldest first, **at most 100 per message and at most
   128 KiB serialised, whichever comes first — and no single event may exceed
   8 KiB serialised**. The per-event bound is what makes the batch bound
   reachable: `data` is free-form, JSON Schema cannot bound a serialised size,
   and without it one event can exceed the frame cap on its own, at which point
   "split, never dropped" is unsatisfiable and the loop below is unavoidable. A
   station with more to say truncates `data`, keeps the event, and says so in
   `detail`. The byte limit is not
   decoration: `data` is free-form, and 100 events each within every field
   bound reaches 786 KiB — over the 512 KiB frame cap, which closes the
   socket, after which the station reconnects and re-sends the identical
   batch for ever. That loop also takes telemetry and commands down with it,
   because they share the socket. A batch that does not fit is split, never
   dropped.
3. The platform stores the batch durably — refusing any event it is required
   to, which counts as dealt with rather than as a failure — and replies on the
   command channel with `events.ack {through_seq}`.
4. The station deletes up to that seq, and sends the next batch. It does not
   run ahead of the acknowledgement — one batch in flight at a time, so a
   station reconnecting after a week does not arrive as a flood.
5. Anything unacknowledged is re-sent, with backoff, until it is acknowledged
   or evicted (below).

**A station applies an `events.ack` only to the batch it is currently
awaiting, and ignores every other.** One batch is in flight at a time, so
there is exactly one ack a station can be expecting; anything else is a
duplicate, a straggler from a previous connection, or from before a store
rebuild. This is the rule that matters, and it needs no new field precisely
because the one-batch-in-flight discipline already identifies the batch.

**An acknowledgement covers the batch, not only the rows that survived it.**
The platform acks the highest `seq` in the batch once it has durably stored
every event in it that it accepted, and an event it refuses — a reserved
`platform.` type, a payload that fails the schema — counts as dealt with. This
is the one place where the obvious reading is the dangerous one. Read
`through_seq` as "the highest I stored" and a batch whose *first* event is
unstorable has no honest ack: the highest stored seq falls below the batch, the
rule above tells the station to ignore it, and it re-sends the same batch for
ever with every later event queued behind it. One malformed event would end
that site's history permanently. Re-sending cannot help, because nothing about
a second delivery makes a refused event acceptable — so refusal has to be
terminal and the cursor has to move past it. **The platform records what it
refused**, on its own side; the station has been told the batch is done and no
longer holds it. If a station ever needs to know which rows died, that is an
optional field on a later `events.ack` and a free minor — but it must never be
the difference between a channel that drains and one that wedges.

The clamp below is the weaker companion to it and is not sufficient alone:

**A station also ignores an `events.ack` above the highest `seq` it has
actually published.** The ack is the one irreversible thing the platform can do
to a station, and it can arrive late, twice, or after a reconnect.

**Keep the `seq` counter in durable storage of its own**, rather than deriving
it from the rows still present. An emptied store otherwise restarts at zero and
reuses numbers it has already used — and on a quiet station, draining to empty
is routine rather than rare. That is what makes a stale ack dangerous: a
pre-rebuild `through_seq` lands *inside* the fresh range and the clamp above
does not fire, which is exactly why the batch rule and not the clamp is the one
that protects the data.

**The platform records its own receipt time for every event and never trusts
`at` for anything but display and ordering.** `at` is set by the station, on a
clock the station itself may flag as `unsynced`, and the rule below turns it
into an alerting decision — so a station that is wrong, or lying, must not be
able to backdate a real event into silence. Receipt time is the platform's, and
it is what staleness is judged on.

**Event `type` values beginning `platform.` are reserved**, and **the platform
enforces that by normalising before it compares.** The vocabulary is otherwise
the station's to extend, but the platform writes its own facts — credential
issued, revoked, enrolment claimed — into the same operator-visible timeline,
and nothing else should be able to forge one.

The schema carries a pattern and it is not the defence: it forbids exactly
`platform.` and a station has a dozen ways past it that render identically to
an operator — `Platform.`, a leading space, a Cyrillic а, a full-width stop, a
zero-width space. **Require `type` to be printable ASCII, reject it outright if
it is not, and only then normalise (NFKC, casefold, strip) and compare.** The
ASCII rule is doing the real work and the normalisation alone is not enough:
NFKC maps a full-width `Ｐ` to `P` but leaves Cyrillic а exactly where it is,
because the two are different letters and not two spellings of one — and
`strip()` removes whitespace, which a zero-width space is not. Both of those
were measured against this paragraph's own examples and both walked straight
through. An event type is a machine vocabulary; nothing legitimate here needs a
character outside ASCII, so refusing them costs nothing and closes the whole
class rather than the instances anyone thought to enumerate. A schema pattern
cannot do this and the party it constrains is the wrong one.

**Local storage is finite and the contract does not pretend otherwise.** A
station caps its event store, evicts oldest-first when it is full, counts what
it dropped in `health.storage.events_dropped`, and raises a condition. "Re-sent
for ever" is the delivery rule, not a storage promise: the alternative is a full
disk, which stops recording and sensing as well, and a site that has been
offline for six weeks has already lost the argument. A platform seeing
`events_dropped` rise knows the gap is real rather than a delivery fault.

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
after an outage is history. Waking an operator at nine in the morning for a
proximity alert from three in the morning is how a station's backlog becomes a
reason to turn alerting off.

Judge that on **receipt time**, not on `at`. The two rules above collide
otherwise: an `unsynced` event's `at` may be fifty years out in either
direction, so classifying by it either buries a live alert as ancient history
or pages the whole fleet for events stamped in the future. An event whose
`clock` is `unsynced` is **never** classified by `at` — display it, order it by
`seq`, mark it as untimed, and decide liveness from when it arrived.

`health.storage.events_pending` is the backlog, and is the number to watch: it
rising during an outage is the system working, and it staying high while the
uplink is up is a delivery fault.
