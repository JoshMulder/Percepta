# The station ↔ platform contract

Everything that crosses the boundary between a **ground station unit** and the
**Percepta platform**. Two teams build against this and neither needs to read
the other's code.

```
percepta/
  server/     the platform          — owned by the platform agent
  station/    the onboard computer  — owned by the station agent
  contract/   this                  — owned jointly, changed deliberately
```

## The rule

**Neither side edits the other's directory.** Both read this one. A change here
is a change to someone else's build, so it is proposed and agreed rather than
made in passing — if you find yourself widening a schema to make your own code
compile, that is the moment to stop and raise it.

## What is in here

| File | What it fixes |
|---|---|
| `enrolment.md` | How a box becomes a station, and everything it is told afterwards |
| `transport.md` | Channels, identity, direction, delivery expectations |
| `schemas/telemetry.schema.json` | Every telemetry payload a station may publish |
| `schemas/command.schema.json` | Every command a station must accept |
| `conformance/check_station.py` | Runs against a live station and reports what it got wrong |

## The reference implementation already exists

`server/app/backend/scripts/simulate_station.py` is a working station: it speaks
this protocol on the real bus, and the console cannot tell it from hardware.

That makes the station brief precise rather than vague — **replace the
simulator, keep the console working, pass conformance.** When in doubt about
what a field should contain, that file is the answer, and this contract is what
stops it being the *only* answer.

## Three rules that are not visible in the schemas

Each of these has already cost somebody something.

**1. A station never asserts its own organisation.** Enrolment is where this is
established — see `enrolment.md`. Nothing a station publishes
is trusted to say which tenant it belongs to; the platform resolves that from
its device registry, keyed on the authenticated identity. A compromised station
must be able to forge its own readings — unavoidable, it owns the sensors — and
nothing else. Do not add an `organization_id` field. It would be ignored, and
its presence would invite someone to start trusting it.

**2. Transmit fails released.** Not implemented yet, and gated behind an
ungrantable capability, but design for it now: a stuck PTT jams an aeronautical
frequency across its whole coverage area, and this platform's sites are
unattended on links that drop routinely. Loss of the operator's connection must
release PTT immediately, enforced *at the station*, with a hardware watchdog and
a maximum transmission time that do not depend on the platform being reachable.
See `server/docs/05-radio-integration.md`.

**3. The airband noise floor is measured outside the channel.** Median of the
spectrum 15–50 kHz either side, converted to in-channel power. Measuring inside
the channel has a specific failure: a weak signal arriving while the estimate is
stale-high gets treated as noise, the floor drifts up toward it, and the squelch
latches shut permanently. That is the regression to test, and the simulator
cannot exercise it because it is *told* the floor rather than measuring it.

## Changing the contract

1. Raise it with the other side before writing code against the change.
2. Update the schema and `transport.md` in the same commit.
3. Additive changes (a new optional field, a new telemetry kind) are cheap —
   consumers ignore what they do not know. Renames and removals are not; treat
   them as breaking and stage them.
4. Run `conformance/check_station.py` on both sides afterwards.

## Wider context

- `server/docs/00-topology.md` — the canonical system definition. Read first.
- `server/docs/03-realtime-isolation.md` — why the platform is shaped this way.
- `server/docs/05-radio-integration.md` — what the radio hardware demands.
