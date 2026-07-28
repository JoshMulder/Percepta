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

## Not yet built on the platform side

Today the simulator publishes *directly* onto the platform's internal fan-out
channels (`rt:g:org:{org}:gsu:{station}:{stream}`), skipping this boundary
entirely. That is a development shortcut, not the design.

The platform therefore owes an **ingest**: subscribe to `gsu/+/telemetry`,
resolve station → organisation from the registry, and republish onto the internal
fan-out. Until that exists, a station built to this contract has nothing
listening to it.

**Platform agent: this is your task, and it blocks the station agent's first
end-to-end test.** Track it as such rather than letting the station side
discover it.

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
