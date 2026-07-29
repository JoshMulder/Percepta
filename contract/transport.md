# Transport

## Direction and channels

A station publishes upward on channels named for itself, and subscribes to one
channel for commands. It has no other route in or out — in particular it never
talks to a browser (`server/docs/00-topology.md`, rule 8).

| Direction | Channel | Payload |
|---|---|---|
| station → platform | `gsu/{station_id}/telemetry` | one telemetry object, JSON |
| station → platform | `gsu/{station_id}/audio` | one audio object, JSON |
| station → platform | `gsu/{station_id}/video` | one MJPEG frame, JSON |
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
(`gsu/{station_id}`) pinned to exactly the three channels above.

One thing that pinning does **not** yet do: Redis' `default` user is still open
on the development stack, so an unauthenticated client can publish anywhere. The
per-station principals are real and enforced for anyone using them; closing
`default` is a deployment change and is the last gap on this boundary.

## Video

Motion JPEG, one whole frame per message, on `gsu/{station_id}/video`. See
`schemas/video.schema.json` - the reasoning for independent frames over an
encoded stream is written there, and it is a decision about *this* camera on
*this* link rather than a general preference.

It is the heaviest thing a station sends. 640x480 at 2 fps is roughly half a
megabit per second sustained, which is significant on a metered link, so the
rate is low by default. Publishing continuously to a console nobody is watching
is waste, and asking for video on demand is the intended next step - it needs a
command, and that is an open contract question rather than something to invent.

`captured_at` is when the frame was **taken**. An operator looking at a still
image assumes it is current unless told otherwise, and a frozen frame from four
minutes ago is exactly what gets acted on wrongly.

## Streams with no source

Send `available: false` with a short `unavailable_reason` rather than an empty
payload or silence. See `README.md`. Keep sending it on the normal cadence - a
station that goes quiet is a station that has failed, and "I have no receiver"
is something you have to keep saying.

## Transport security

Both channels are TLS. **All traffic between a station and the platform is
encrypted; there is no unencrypted path and no downgrade.**

The two channels are verified differently, and the distinction matters:

- **Broker** - verified against `broker.ca_pem`, the private CA handed over at
  enrolment. A station trusts exactly one issuer for its data path, which is
  stronger here than public PKI rather than weaker.
- **API** - normally behind a TLS-terminating reverse proxy with a public
  certificate, so verified against the system trust store. A station may pin the
  API to a private CA instead where there is no proxy, but that is configuration
  rather than the default.

The field is `broker.ca_pem` and not `ca_pem` for exactly this reason: it is the
broker's trust root. Using it for the API works today and stops working the
moment a real certificate is in front.

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

Redis pub/sub today; MQTT over TLS is the intended production transport
(`server/docs/01-architecture-notes.md`). Both are fire-and-forget from the
station's point of view, and the contract assumes nothing stronger:

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

Each station authenticates with its own credential — mTLS client certificate
preferred over a bearer token — and the broker ACL pins that identity to
`gsu/{station_id}/#` and `cmd/gsu/{station_id}`.

Enrolment (issuing the id and credential) is not built yet on either side.

## Cadence and bandwidth

These sites are on metered, intermittent links. The platform's console is built
to tolerate gaps, so favour dropping data over queueing it.

| Stream | Cadence | Notes |
|---|---|---|
| `adsb` | 1 Hz | Full current picture each time, not deltas |
| `power` | 1 Hz | |
| `radio` | 1 Hz | |
| `light` | 1 Hz | |
| `weather` | 0.2 Hz | Changes slowly; no reason to send it faster |
| `audio` | while squelch is open only | See below |

**Audio is the platform's largest single consumer** and the one place where
being careless is expensive. Uncompressed it is ~384 kbit/s per listener,
continuously. Two things keep it manageable, and the second is not done:

1. **Send only while the gate is open.** Airband is silent most of the time, so
   this alone is most of the saving. Required.
2. **Compress it.** Opus at 16–24 kbit/s is transparent for voice and would cut
   the rest by more than an order of magnitude. The current format is
   base64-encoded PCM inside JSON, which is convenient and wasteful; expect this
   to change, and keep the encoding behind one function.

## Store and forward

A station must keep working with no link at all: continue sensing, recording and
locally alerting, and reconcile when the link returns. What it buffers and for
how long is the station's business, but note that replaying a backlog of stale
telemetry into a live console is worse than dropping it — the console shows
*current* state. Buffer events and recordings; drop stale telemetry.
