# Ground station unit — onboard computer

**Start here.** This directory is yours. Everything you need to build against is
in `../contract/`, and you should not need to read `../server/` except where this
file points you at it.

## What you are building

The software that runs on the computer inside a ground station box: a remote,
unattended enclosure containing a security camera, an airband receiver, an ADS-B
receiver, a weather station, a floodlight, a solar array and a Starlink terminal.

It talks to all of that hardware locally, and to the Percepta platform over a
single authenticated link. Nothing else reaches it — no browser, no operator,
no other station.

## The three things that shape every decision

**1. The link drops, routinely.** Starlink has obstruction dropouts and
satellite handovers. The station must keep sensing, recording and locally
alerting with no connectivity at all, and reconcile when it returns. Nothing the
station needs to do correctly may require the platform to be reachable.

**2. Nobody is there.** The nearest person may be hours away. A failure mode that
needs someone to power-cycle something is a site outage, not an inconvenience —
this is why, for instance, the radio process must be stopped via its shutdown
endpoint and never killed (`../server/docs/05-radio-integration.md`).

**3. Bandwidth is metered.** Favour dropping data over queueing it. Telemetry is
current state, not a ledger; replaying an hour of stale readings into a live
console is worse than a gap.

## Your brief, precisely

`../server/app/backend/scripts/simulate_station.py` is a working station. It
speaks the real protocol on the real bus, and the console cannot tell it from
hardware.

**Replace it. Keep the console working. Pass conformance.**

```bash
python ../contract/conformance/check_station.py --station <uuid>
```

That script talks to the broker, not to anyone's code. The simulator passes it
today and so must yours.

## You are on a different machine to the platform

That is fine, and mostly does not matter — but it changes three things.

### Work standalone first

The conformance script talks to *a* broker, not to the platform's. So bring up
your own and develop against it with no network path to the platform at all:

```bash
cd station && docker compose up -d          # a Redis on 127.0.0.1:6380
python ../contract/conformance/check_station.py --station <any-uuid>
```

Publish to `gsu/{uuid}/telemetry`, subscribe to `cmd/gsu/{uuid}`, and the script
will tell you what is missing or malformed. Everything except end-to-end
integration can be finished this way, and it keeps the two machines uncoupled
while both are moving fast.

### Sharing code

Both machines pull from the same git remote; `contract/` travels with it. Clone,
then work only inside `station/`:

```bash
git clone <remote> percepta && cd percepta
```

A contract change is a commit both sides pull — which is the point. It is
visible, reviewable and dated, rather than a conversation someone missed.

### Integrating, later

Only when you want end-to-end: point at the platform's broker instead of your
own. That needs, from the platform side, all of which should be asked for rather
than assumed:

- the broker's address, reachable from your machine
- credentials for it
- a **station UUID that exists in the platform's registry**, and an **enrolment
  code** for it — the platform resolves the organisation from that id, and drops
  everything from a station that is not enrolled or whose credential was revoked

Production is MQTT over TLS with a per-station client certificate. Development
is Redis, and the difference is deliberately confined to one place in your code:
keep the transport behind a small interface and none of the rest cares.

## Read in this order

1. `../contract/README.md` — the boundary, and three rules that are not visible
   in the schemas
2. `../contract/enrolment.md` — how the box gets its identity, and everything
   it is told afterwards. Read before designing the boot sequence: it constrains
   time sync, local storage and the setup flow.
3. `../contract/transport.md` — channels, cadence, delivery expectations
4. `../contract/schemas/` — every payload, with the reasoning in the field
   descriptions
5. `../server/docs/00-topology.md` — the canonical system definition
6. `../server/docs/05-radio-integration.md` — before touching the radio; it
   carries hard-won hardware behaviour and one safety requirement

## Known gaps you will hit

- **Enrolment is built on the platform, not on the box.** `POST /api/enrol` with
  a code an admin issues you, and you get back a credential, your broker
  username, and the exact topics you may use. That is yours to implement:
  `../contract/enrolment.md` §10 lists it, and §11 records where the platform
  deviated from the spec and why. Read §6 before designing the boot sequence —
  clock, expiry and renewal are what strand a remote site.
- **The broker's `default` user is still open.** Your per-station principal
  exists and is genuinely pinned to your own channels, but nothing yet forces
  anyone to use it, so unauthenticated clients still work. Build as though they
  do not: authenticate as `gsu:{station_id}` from the start, because when
  `default` is closed, code that never authenticated stops working everywhere at
  once.
- **Five decisions in `../contract/enrolment.md` §9 need a human** — compute
  platform, who installs, token lifetime, broker hosting, update path. Do not
  invent answers to those.
- **Transmit is not implemented anywhere**, and must not be until the
  fail-released design in `05-radio-integration.md` exists.

## Working agreement

- Do not edit `../server/`. Do not edit `../contract/` unilaterally — a change
  there changes someone else's build, so propose it.
- Additive contract changes are cheap; renames and removals are breaking.
- Commit inside `station/` only.

---

# The implementation

Everything above is the brief. What follows is what was built against it.

```
gsu/
  agent.py       the loop: sense, alert, record, then publish
  transport/     the only code that knows the broker is Redis (mqtt.py is a
                 documented stub, not a plausible-looking untested client)
  devices/       the supported-device registry, the inventory (intent vs fact),
                 and the drivers: MAVLink/ping RX for ADS-B, NMEA/Airmar for
                 weather, serial I/O under both
  radio/         the receiver, and the squelch and noise-floor logic that
                 contract/README.md rule 3 makes station-side correctness
  sensors/       interfaces, and simulated implementations behind them
  console.py     the local setup and device-selection app (enrolment.md §5, §7)
  enrolment.py   claim, renew with backoff, and alarm early
```

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# First boot: claim a code. Or leave it and use the console.
.venv/bin/python -m gsu enrol --token XXXX-XXXX-XXXX

# Run. Console on http://127.0.0.1:8088
GSU_BROKER_URL=redis://127.0.0.1:6380/0 .venv/bin/python -m gsu run

.venv/bin/python -m gsu devices    # what is fitted, and what was found
.venv/bin/python -m gsu bench      # what a tick costs — run this on the Pi
.venv/bin/python -m unittest discover -s tests -t . -q
```

`GSU_BROKER_URL` overrides only the broker *address*; the username and topics
still come from enrolment. It exists because the platform currently hands out a
container-internal hostname.

Other environment: `GSU_HOME` (state directory), `GSU_PLATFORM_URL`,
`GSU_SETUP_HOST`/`GSU_SETUP_PORT`/`GSU_SETUP=0`, `GSU_AIRBAND_TRAFFIC`
(`off`/`low`/`busy`), `GSU_ENROL_TOKEN`.

## Read next

- **DECISIONS.md** — every assumption that needs a human, and the five open
  decisions from `contract/enrolment.md` §9, still open.
- **CONTRACT-QUESTIONS.md** — nine things the contract could not express when
  this was built. Four are settled (`available: false`, optional
  `humidity_pct`, positionless contacts stay dropped, and the conformance
  harness); the rest are open. Nothing in `contract/` was edited from this side.
- **HARDWARE.md** — what the Pi 2B, the single RTL2838 and the Airmar 110WX can
  and cannot carry, measured where measurable and marked where not.
