# Decisions taken building both ends against contract 2.0

Written while the contract was frozen at `a3c785a` and both ends were still
speaking the pre-2.0 wire format. Every entry is a judgement call made without
being able to ask, so each one records **what was decided, what the
alternatives were, and what would change the answer** — that last part matters
most, because a decision whose reasoning is not written down gets re-litigated
by whoever meets it next.

Scope set by the brief: no Raspberry Pi 2B and no CSI camera; both ends deploy
as plain Docker images; bootstrap is help with environment variables and
`docker compose up`; updating is `git pull` and a container restart.

---

## 1. The station's transport abstraction becomes stream-based, not topic-based

**Decided.** `Transport.publish(topic, payload)` becomes
`publish(stream, payload)` where `stream` is one of `t`, `a`, `e`. The command
direction stops being a subscription and becomes a single `on_command`
callback.

Contract 2.0 took channel names off the wire deliberately — a station never
names a channel and never sends its own id, so confinement is structural rather
than checked. The old abstraction was built around the thing the contract
deleted: it took a topic string, the topic strings came from the enrolment
response, and enrolment no longer issues them. Keeping a topic-shaped interface
and translating to stream codes inside the relay would have left every caller
still holding a channel name that means nothing, and left the enrolment
response still needing to carry names the contract removed.

*What would change this:* nothing short of the contract reintroducing named
channels, which would be a 3.0.

## 2. The Redis transport is deleted rather than kept for a bench

**Decided.** `gsu/transport/redis_transport.py` goes, and `build_transport`
accepts only `ws://` and `wss://`.

It existed so a station on a LAN could talk to Redis directly, and it is the
only reason the topic-based abstraction had a second implementation to justify
it. Contract 2.0 defines exactly one transport. A second one that matches no
contract document is not a bench convenience, it is a second wire format to
keep working, and it is the one that would rot silently because nothing tests
it.

The bench case is better served than it was: `contract/conformance/
check_station.py` *is* a platform — it speaks the 2.0 relay, needs no Redis, no
database and no station id, and it says whether the station is conformant
rather than merely whether bytes moved. That is a strictly better bench than
pointing a station at a Redis container.

*What would change this:* a deployment where the station genuinely cannot reach
443 but can reach 6380. That has never been the case here — the reasoning in
`relay.py` is that 443 is the only port open at every site.

## 3. The platform accepts the WebSocket and *then* closes 4401

**Decided, and this was a live bug rather than a design choice.**

`transport.md` gained the rule in the freeze: at connect, a refused credential
means completing the handshake and then closing 4401, never rejecting the
upgrade with HTTP 401. The platform was doing exactly the wrong thing —
`broker.py` called `websocket.close(code=4401)` before `accept()`, and Starlette
answers that with an HTTP 403 rejection, so no close code ever reaches the
station.

The consequence is the one the clean-room seat predicted: a box whose
credential expired while it was offline gets no 4401, never calls `/renew`, and
reconnects on a five-minute backoff for ever. The seven-day renewal grace in
`enrolment.md` §4 exists to make that a non-event, and this defeated it
entirely. It is a truck roll per station, and it would have shipped.

## 4. Opus is a ctypes binding to libopus, not a pip dependency

**Decided.** `gsu/radio/opus.py` binds `opus_encoder_create`, `opus_encode` and
`opus_encoder_destroy` through `ctypes`, and the image installs `libopus0` from
apt.

This is the same shape as `gsu/radio/rtl2832.py`, which drives the SDR through
libusb via ctypes for the same reason: the station's stated property is that it
boots with what is in its image and never installs anything. The alternatives
were `opuslib` (a thin ctypes wrapper — so the same apt dependency plus a pip
package to add nothing) and PyAV (a large compiled dependency carrying all of
ffmpeg's API surface to use four functions).

The encoder API is genuinely small. Binding it costs less than carrying a
dependency to do it.

*What would change this:* needing to *decode* Opus on the station, or needing
container formats. Neither is in the contract — audio is raw packets, no
container, by design.

## 5. Container targets are amd64 and arm64 only

**Decided.** `linux/arm/v7` is dropped from the build.

It existed for the Pi 2B, which the brief removes. It was also the single
largest source of complexity in the deployment story: numpy publishes no armv7
wheels, so the Dockerfile carried a long explanation of piwheels and a
recommendation to run the agent under systemd against the system numpy instead
— which meant two deployment paths that could fail differently, and nobody
knowing which one they were debugging.

Dropping armv7 collapses that to one path. numpy is a wheel on both remaining
architectures and nothing compiles.

## 6. Deployment is one compose file per end and an env-var helper

**Decided.** `bootstrap.sh` on both ends does two things: help write a `.env`,
and run `docker compose up -d`. It installs nothing, configures no systemd unit,
and provisions no host packages.

The station's existing bootstrap was a full installer — udev rules, systemd
units, host package installation, five compose files. All of it was in service
of a box where the agent ran on the host. With the agent in a container and the
Pi 2B gone, the host needs Docker and nothing else.

Updating is `git pull && docker compose up -d --build`. That is stated in one
place in each README rather than in sixteen sections of a deployment document.

## 7. The station's event store is SQLite, not a JSON file

**Decided.** Events are held in SQLite under `$GSU_HOME`.

The contract requires events to survive a reboot, be evicted oldest-first under
a cap, be batched under three simultaneous limits, and carry a `seq` counter
that must *not* be derived from the rows still present. That last rule is the
one that decides it: the counter has to be durable independently of the data,
which is a second file to keep consistent with the first, and doing that
correctly under power loss is what a database is for. An unattended box on
solar loses power without warning; that is the normal case, not the exception.

SQLite is in the standard library, so this adds no dependency.

## 8. `events.ack` is implemented as "the batch is dealt with"

**Decided**, following the rule the freeze wrote into `transport.md`.

The platform acks the highest `seq` in the batch once it has stored every event
it accepted, and an event it refuses — a reserved `platform.` type, a payload
that fails the schema — counts as dealt with. Both independent clean-room
platform builds deadlocked on the other reading, and it is not a subtle
deadlock: one unstorable event at the head of a batch stops that site's history
for ever.

The platform records what it refused on its own side, so nothing is lost
silently, it is just not the station's problem any more.

## 9. Station identity in the container is a bind-mounted state directory

**Decided.** `$GSU_HOME` (`/var/lib/percepta-gsu`) is a named volume, and the
credential lives in it.

The alternative is passing the credential as an environment variable, which
would make `docker compose up` on a fresh box a complete deployment. It is
rejected because the credential is rotated by the station itself — renewal
writes a new secret, and an environment variable cannot be written back to. A
station whose credential lives in the environment silently stops renewing and
dies at expiry, which is the failure this contract spends a whole section
preventing.

The enrolment *token* is an environment variable, because it is single-use and
short-lived. That is the intended shape: the token is what a technician types,
the credential is what the station keeps.

---

## 10. Audio is Opus on the wire and PCM on disk

**Decided.** The station encodes Opus for the uplink and keeps writing PCM
WAVs locally.

They are different problems. The wire is metered and somebody pays for it by
the gigabyte — measured on the way in, 400 ms of speech is 19 200 bytes of
PCM16 against 1 087 bytes of Opus, which is 21.7 kbit/s where the contract's
bandwidth section claims 16–24. Local storage is not metered, and a WAV can be
opened by anything, which is the entire reason for keeping a recording.

So the receiver holds the raw block as `last_pcm` and the payload builder never
sees the recording path. Decoding what was just encoded, to get back to the
samples that produced it, would be an absurd way to write a file.

The encoder is held for the life of the receiver rather than built per
transmission: Opus carries prediction state between frames, and a fresh encoder
each time throws that away and costs bitrate for nothing.

A box without `libopus` reports it once and publishes no audio, while going on
receiving, squelching and recording. Falling back to PCM was rejected — the
platform's schema would refuse it, and from the field that is indistinguishable
from a quiet channel, which is the exact failure this contract exists to delete.

---

## Still owed when this was written

Recorded honestly rather than as a plan, because the brief was to build both
ends and this is what is not yet built. Each is a real gap, not a tidy-up.

- **The console cannot play audio.** `web/src/useAudio.ts` decodes base64 PCM16
  into an `AudioWorklet`, and the station now sends Opus packets. The fix is
  WebCodecs `AudioDecoder`, which takes exactly the raw packets this contract
  carries with no container — but there is no Node on the build machine, so
  writing it would mean shipping a rewrite of the audio path that has never
  been type-checked, let alone run. **Until this lands the console is silent.**
- **`simulate_station.py` still emits PCM**, and `NOTES.md` designates it the
  reference implementation. It is now wrong in the one way that file has been
  wrong before — demonstrating a format the contract does not define. It needs
  either the same ctypes binding (which means `libopus0` in the server image)
  or an honest `available: false` for its audio stream.
- The TypeScript changes in this branch — the ADS-B renames — are reviewed
  rather than compiled, for the same reason.
- Nothing yet exercises the renewal-shortens-a-lease rule end to end.
- `_selfstation.py` remains a test fixture, not a reference implementation.
