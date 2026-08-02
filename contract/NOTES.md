# Notes on implementing this contract

**Nothing here is normative.** The contract is `README.md`, `enrolment.md`,
`transport.md` and `schemas/`; if this file disagrees with any of them, they
win and this is out of date.

It exists because those documents were carrying two different kinds of writing.
One is the boundary itself, which is agreed between two teams and frozen at a
version. The other is *where each side implements it and what it cost to find
out* — file paths, build status, and a handful of traps that have each taken an
hour off somebody. That second kind is genuinely useful and it churns every time
code moves, so keeping it inside the frozen document meant the frozen document
was never quite frozen.

Read the contract to build against. Read this when something is not working.

---

## Where each side implements it

Paths move. If one is wrong, the contract still stands.

### Platform

| What | Where |
|---|---|
| Enrolment endpoints (`/api/enrol`, `/renew`, `/status`) | `server/app/backend/api/enrolment.py` |
| Token and credential lifecycle | `server/app/backend/services/enrolment.py` |
| Admin issue and revoke | `server/app/backend/api/station_enrolment.py` |
| Channel names, one definition | `server/app/backend/services/station_topics.py` |
| Broker principals and ACLs | `server/app/backend/services/broker_acl.py` |
| The 443 relay (`WS /broker`) | `server/app/backend/api/broker.py` |
| Ingest: station → org, onto the fan-out | `server/app/backend/services/station_ingest.py` |
| Commands out | `server/app/backend/api/commands.py` |
| Audio demand leases | `server/app/backend/services/audio_demand.py` |
| Media ingest and viewers | `server/app/backend/api/media.py`, `realtime/media.py` |
| Lifecycle exercised against a live stack | `server/app/backend/scripts/verify_enrolment.py` |

### Station

| What | Where |
|---|---|
| Enrolment, renewal, backoff | `station/gsu/enrolment.py` |
| Credential and pinned CA at rest | `station/gsu/credentials.py` |
| Trust rules; no path downgrades | `station/gsu/tls.py` |
| Command dispatch | `station/gsu/commands.py` |
| The sensing loop and the health frame | `station/gsu/agent.py` |
| Squelch, noise floor, audio and spectrum leases | `station/gsu/radio/receiver.py` |
| Live H.264 and the media uplink | `station/gsu/stream.py`, `gsu/transport/stream.py` |
| Camera ownership, one holder at a time | `station/gsu/camera/ownership.py` |
| Clock discipline | `station/gsu/clock.py` |

## The reference implementation

`server/app/backend/scripts/simulate_station.py` is a working station: it speaks
this protocol on the real bus, across the real ingest, and the console cannot
tell it from hardware.

That makes the station brief precise rather than vague — **replace the
simulator, keep the console working, pass conformance.** When in doubt about
what a field should contain in practice, that file shows one answer.

**It is an example and not the specification.** It has been wrong before in a
way worth remembering: it published audio on every open squelch with no lease
handling, so the designated reference for a rule the contract marks *Required*
demonstrated precisely the behaviour that rule forbids, and anyone copying it
inherited a station that streamed to nobody. When the two disagree, the contract
is right and the simulator has a bug.

## Build status against contract 1.0

Both sides were built against the contract as it stood before 1.0 was fixed, so
each owes some work. This is the list, and it is the only part of this file that
should ever be empty.

**Station**
- Audio is still base64 PCM. 1.0 fixes the format as Opus
  (`schemas/audio.schema.json`). `tests/test_station.py` carries an expected
  failure that names this; when Opus lands the test reports an unexpected
  success, which is the signal to delete the marker.
- No events channel: nothing publishes `gsu/{id}/events`, and there is no
  `events.ack` handler. Events are already recorded and buffered locally, so
  what is owed is delivery, batching and retention-until-acknowledged.
- Does not declare `health.contract_version`.

**Platform**
- No events ingest: nothing subscribes to `gsu/*/events`, stores a batch
  durably, acknowledges it, or deduplicates on `id`. The rule that a
  backlog arriving after an outage must not raise as live alerts is unbuilt.
- The broker ACL and the relay's permitted set do not yet grant the events
  channel. Both derive from `services/station_topics.py`, which is the one
  place to change — a topic granted in one and not the other produces a
  station that enrols perfectly and publishes nothing.
- Audio is relayed as PCM and the console decodes PCM.
- Does not record `health.contract_version` per station.

**Console**
- Needs an Opus decoder. WebCodecs `AudioDecoder` takes the raw packets this
  contract carries with no container; a wasm decoder is the alternative if the
  browser matrix is wider than one fixed console. Worth settling early — it is
  the one open choice that could still feed back into the schema cheaply.

## Traps, each of which has cost somebody an hour

**redis-py lets a URL override keyword arguments.** `ConnectionPool.from_url`
ends with `kwargs.update(url_options)`, so connecting with a credential-carrying
URL *and* `username=`/`password=` authenticates as whoever the URL names, not
who you passed. This is why the contract requires `broker.url` to be
credential-free, and why the station strips and warns regardless.

**`redis-cli` accepts a CA that Python refuses.** A CA without
`basicConstraints` and `keyUsage` works on the command line and is rejected by
Python's `ssl` with *"CA cert does not include key usage extension"*. Testing
TLS with `redis-cli` alone proves nothing about a Python client, and the failure
arrives as every station in the fleet failing at once.

**The command channel is slash-separated.** `cmd/gsu/{id}`, not `cmd:gsu:{id}` —
the colon form is the platform's *internal* fan-out naming. Getting it wrong
produces a station that is subscribed, receiving and dropping everything, which
from the outside is indistinguishable from one that ignores its operator.

**A topic granted in one place and not another fails silently.** The ACL, the
relay's permitted set, the enrolment response and the ingest's subscription must
agree. When they do not, the publish is refused, the station reports it and
carries on, and what an operator sees is a box that enrolled perfectly and
publishes nothing. Nothing crashes and nothing logs an error at either end.

**Two USB-UART adapters swap between boots.** Bind serial devices by their
`by-id` path, never `/dev/ttyUSB0`, or each driver eventually reads the other's
traffic and both instruments present as failed.

## Wider context

- `server/docs/00-topology.md` — the canonical system definition. Read first.
- `server/docs/03-realtime-isolation.md` — why the platform is shaped this way.
- `server/docs/05-radio-integration.md` — what the radio hardware demands,
  including the fail-released requirement that gates transmit.
- `station/DECISIONS.md` — the station's own assumptions and open questions.
