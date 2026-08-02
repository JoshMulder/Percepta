# Transport

## Direction and channels

A station publishes upward on channels named for itself, and subscribes to one
channel for commands. It has no other route in or out — in particular it never
talks to a browser (`server/docs/00-topology.md`, rule 8).

| Direction | Channel | Payload |
|---|---|---|
| station → platform | `gsu/{station_id}/telemetry` | one telemetry object, JSON |
| station → platform | `gsu/{station_id}/audio` | one audio object, JSON |
| platform → station | `cmd/gsu/{station_id}` | one command object, JSON |

`{station_id}` is the UUID the platform issued at enrolment. A station publishes
under its own id and nothing else; the broker ACL enforces that, so a compromised
station cannot publish into another tenant's namespace or read anything back.

**The platform resolves the organisation from the station id**, via its device
registry. Nothing in the payload says which tenant this is, and nothing should.

## The platform side of this boundary

**Built.** `server/app/backend/services/station_ingest.py` subscribes to
`gsu/*/telemetry` and `gsu/*/audio`, resolves station → organisation from the
device registry, and republishes onto the platform's internal fan-out
(`rt:g:org:{org}:gsu:{station}:{stream}`). It starts with the application. A
station publishing to this contract is now heard.

What it does with what you send:

| | |
|---|---|
| Station id | Taken from the **channel name**, never from the payload |
| Organisation | Resolved from the registry. A station id that is unknown or deactivated is dropped, logged once, and nothing reaches any subscriber |
| Unknown `kind` | Dropped and logged once — as this contract promises, so a station may be newer than the platform |
| Malformed JSON | Dropped and logged |
| `last_seen_at` | Written by the ingest, at most every 15s per station. This is what drives online/offline in the console, so **a station that stops publishing goes offline on its own** — there is no separate heartbeat to send |

The simulator publishes across this same boundary, so it exercises the ingest
rather than bypassing it, and `conformance/check_station.py` passes against it
without `--legacy`.

**Authentication.** A station must be enrolled and hold a valid credential
before anything it publishes reaches a subscriber; revoking one stops its data
within about thirty seconds, whether or not the broker noticed. Enrolment is
built — see `enrolment.md` — and issues each station a broker principal
(`gsu:{station_id}`) pinned to exactly the three channels above.

That thirty seconds holds on both transports, and by different means. On the
direct Redis path the ACL is deleted and the station's clients are killed
outright. On the 443 relay — the deployment path — there is no ACL to delete,
so each open socket carries a watcher that re-checks the credential every 15s
and closes the socket when it no longer stands. **Authenticating once, at
connect, is not enough on a link that stays up for months.** Both the relay and
the media uplink do this; `scripts/verify_enrolment.py` §5 revokes a credential
out from under a live socket and fails if the socket survives.

Redis' `default` user is closed: `server/docker-compose.yaml` passes
`--requirepass` and the stack refuses to start without it, so a process that
reaches the port still has no identity. Per-station ACLs are the second layer,
not the only one.

## Video

**There is no video channel.** `gsu/{station_id}/video` carried one MJPEG frame
per message and is gone, along with `schemas/video.schema.json`.

It was removed at the station and for a reason worth keeping: the camera is a
single device with a single owner, and a periodic snapshot publisher competing
with the live encoder for it is what wedged a real camera —
`station/gsu/video.py` has the full account. Removing one of the two readers
does not narrow that class of fault, it deletes it.

Live video goes over the media WebSocket instead (`WS /media/ingest`), started
only while somebody is watching. What is left on the station is a preview that
publishes nothing at all: it serves the newest frame it has to the local setup
page, over loopback, so an installer can aim a camera.

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

The plaintext listeners are **disabled**, not deprioritised. A station pointed at
`redis://` or `http://` fails to connect rather than quietly sending its
credential in clear, because a silent downgrade is the failure nobody finds out
about.

Two traps, both of which have already cost someone an hour:

- **redis-py lets a URL override keyword arguments.** `ConnectionPool.from_url`
  ends with `kwargs.update(url_options)`, so connecting with a credential-
  carrying URL *and* `username=`/`password=` authenticates as whoever the URL
  names. The `broker.url` handed over at enrolment is deliberately credential-
  free for this reason.
- **`redis-cli` accepts a CA that Python refuses.** A CA without
  `basicConstraints` and `keyUsage` works on the command line and fails in
  `ssl`. Testing with `redis-cli` alone proves nothing about your client.

## Broker

A WebSocket relay on 443 in production (`WS /broker`); Redis pub/sub direct on a bench
(`server/docs/01-architecture-notes.md`). The port is the whole reason: 6380 and
8883 are shut wherever a reverse proxy is, and 443 is not.

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

## Identity

Each station authenticates with its own credential — a bearer token today, with
mTLS client certificates still to come (`enrolment.md` §3) — and that identity
is pinned to **exactly the three channels** in the table above, by name. Not a
`gsu/{station_id}/` prefix: a prefix would also admit channels nobody consumes,
and a station inventing its own on a shared broker is what the check exists to
prevent. A new channel is a change to the contract and to the grant, together.

On the 443 relay the same rule is enforced a second time and more tightly still
— the relay compares each frame's topic against the two a station may *publish*
to, so the command channel is receive-only there even though a Redis ACL cannot
express the difference.

## Cadence and bandwidth

These sites are on metered, intermittent links. The platform's console is built
to tolerate gaps, so favour dropping data over queueing it.

| Stream | Cadence | Notes |
|---|---|---|
| `adsb` | 1 Hz | Full current picture each time, not deltas |
| `power` | 1 Hz | |
| `radio` | 1 Hz | |
| `light` | 1 Hz | |
| `weather` | 0.2 Hz | Changes slowly. A metered site may set this lower — see below |
| `audio` | while squelch is open only | See below |

**A station reports the cadences it is actually using**, in `health.cadence`,
as seconds per stream. Consumers deriving a timeout — "this panel is stale" —
must use that rather than the table above, which is the default and not a
promise. `weather_period_s` is a site setting and is settable at runtime, so a
station legitimately slowed down to save bandwidth would otherwise be marked
faulty by a console still assuming 0.2 Hz, with nothing at either end to say
why. Absent on an older agent, which means the table.

**Audio is the platform's largest single consumer** and the one place where
being careless is expensive. Uncompressed it is ~384 kbit/s, and base64 inside
a JSON envelope makes it ~512. Three things keep it manageable, and the third
is not done:

1. **Send only while the gate is open.** Airband is silent most of the time, so
   this alone is most of the saving. Required.
2. **Send only while somebody is listening.** Required. The platform sends
   `radio.audio` with a `lease_seconds`, renews it while a console holds the
   subscription, and **stops saying anything at all** when the last one goes.
   The lease expires on its own, so audio stops when the platform goes away
   rather than when it remembers to say so — the same shape as `video.start`,
   and for the same reason: most listeners never say goodbye. A closed laptop,
   a dropped link, a revoked session and a shut tab all look identical to
   somebody still listening, right up until the lease runs out.

   A station that has never been asked sends no audio. **Recording is not
   gated by this** — a transmission nobody had a console open for is still
   written to disk, exactly as one during an outage is.
3. **Compress it.** Opus at 16–24 kbit/s is transparent for voice and would cut
   the rest by more than an order of magnitude. The current format is
   base64-encoded PCM inside JSON, which is convenient and wasteful; expect this
   to change, and keep the encoding behind one function.

## Store and forward

A station must keep working with no link at all: continue sensing, recording and
locally alerting, and reconcile when the link returns. What it buffers and for
how long is the station's business, but note that replaying a backlog of stale
telemetry into a live console is worse than dropping it — the console shows
*current* state. Buffer events and recordings; drop stale telemetry.
