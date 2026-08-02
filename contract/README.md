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
| `schemas/audio.schema.json` | The Opus audio a station publishes while asked, and gated |
| `schemas/events.schema.json` | Things that happened, buffered through an outage and acknowledged |
| `schemas/command.schema.json` | Every command a station must accept |
| `conformance/check_station.py` | Runs against a live station and reports what it got wrong |

Those files are the contract, and they are frozen at a version. **`NOTES.md` is
not** — it carries where each side implements this, what is built so far, and
the traps that have cost somebody an hour. That split is deliberate: a document
that names source files cannot be frozen, because the files move.

## Declaring a stream unavailable

A station with no receiver for a stream sends `available: false` and a short
`unavailable_reason` instead of the stream's usual fields. Absent means `true`,
so nothing written before this changes meaning.

This exists because an empty payload and a dead sensor are otherwise
indistinguishable, and **only the station knows which it is**. An empty
`aircraft` array means *clear airspace*; a station with no ADS-B receiver must
not be able to say that. The console renders a `NO ADS-B` badge rather than an
empty map, and strikes through individual readings that have no sensor behind
them - which is deliberately different from the dashes it shows while waiting.

Conformance accepts a declared-unavailable stream in place of a payload, and
skips commands against it. A station is not failed for lacking hardware; it is
failed for pretending.

**Not every absent value is unavailability.** A field the instrument simply does
not measure - humidity on an Airmar without the RH module - is omitted, and is
optional in the schema for that reason. Reserve `available: false` for a stream
with no source at all.

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

**3. The airband noise floor is measured outside the channel.** Median of the
spectrum 15–50 kHz either side, converted to in-channel power. Measuring inside
the channel has a specific failure: a weak signal arriving while the estimate is
stale-high gets treated as noise, the floor drifts up toward it, and the squelch
latches shut permanently. That is the regression to test, and it cannot be
exercised by anything that is *told* the floor rather than measuring it.

## Version

**This is contract 2.0.** It is stamped in each schema's `$id` and
`contractVersion`, the platform states it at enrolment, and a station declares
what it speaks in `health.contract_version`.

**Why 2.0 before anything shipped.** A draft 1.0 existed for a few days and
this supersedes it incompatibly: the relay frame carries a stream code instead
of a channel name, five ADS-B fields gained unit suffixes, `broker.username`
and the topic fields left the enrolment response, and a hundred-odd bounds
narrowed what had been accepted. Every one of those is breaking by the rule
below, and the version exists precisely so nobody has to guess which set a box
holds. Nothing is deployed, so the bump costs nothing today — and a version
that lied about this would have cost a visit to every site that was.

**Declared, never negotiated.** The platform records what a station says, logs a
mismatch, and carries on. Nothing is refused over a version string: these are
unattended boxes on hillsides, and turning a compatibility note into a refusal
turns it into a site visit. The field exists so the first question of any remote
debugging session — *which contract is that box on* — has an answer, which it
did not before. `config_version` is site policy and `agent_version` is a build
string; neither is this.

- **Minor** (2.0 → 2.1) — additive. A new optional field, a new telemetry kind,
  a new command, a new event type. Both sides already tolerate what they do not
  recognise, so old and new interoperate in both directions and no coordination
  is needed.
- **Major** (2.x → 3.0) — something changed meaning, changed shape, or went
  away. Both sides need looking at, and the two versions are not assumed to
  interoperate.

Design for minor. A major bump on a fleet that is hard to reach physically is
the most expensive thing in this document.

## Changing the contract

1. Raise it with the other side before writing code against the change.
2. Update the schema, the prose and the version in the same commit.
3. Additive changes are cheap — consumers ignore what they do not know. Renames
   and removals are not; treat them as breaking and stage them.
4. Run `conformance/check_station.py` on both sides afterwards.

Editorial changes — rewording, reorganising, moving commentary to `NOTES.md` —
do not bump the version. The version describes what crosses the boundary, not
how well it is written down.

## Read next

- `NOTES.md` — where each side implements this, what is built, and the traps.
  Not normative, and the place to look when something is not working.
