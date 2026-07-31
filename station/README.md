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
cd station && docker compose -f dev-broker.compose.yml up -d   # Redis on :6380
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
- **The broker's `default` user is now closed** and the transport is TLS only.
  This station authenticates as `gsu:{station_id}` and verifies the broker
  against the CA it was pinned at enrolment — see `gsu/tls.py`. It was written
  that way while `default` was still open, which is why closing it changed
  nothing here.
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
  tls.py         the pinned CA, and the rules about when this box may talk at
                 all. No mode disables verification; no path downgrades
  transport/     the only code that knows the broker is Redis (mqtt.py is a
                 documented stub, not a plausible-looking untested client)
  devices/       the supported-device registry, the inventory (intent vs fact),
                 and the drivers: MAVLink/ping RX for ADS-B, NMEA/Airmar for
                 weather, serial I/O under both
  radio/         the receiver, and the squelch and noise-floor logic that
                 contract/README.md rule 3 makes station-side correctness
  camera/        the Pi CSI camera under libcamera, a synthetic test card, and
                 the H.264 encoders — hardware and software, probed not assumed
  media/         fragmented MP4, muxed here, and a WebSocket client written out
                 rather than depended on
  video.py       the setup page's camera preview: one frame, on demand, and
                 published nowhere at all
  stream.py      the live H.264 stream, which runs only while somebody is
                 watching and stops when the platform stops asking
  sensors/       interfaces, and simulated implementations behind them
  console.py     the setup GUI: enrol, choose what is fitted, see what the box
                 thinks it is (enrolment.md §5, §7). The whole install for
                 somebody with a phone and no terminal
  setup_access.py  who may reach that page, from where, and for how long — the
                 four controls, and why each one is a default and not a setting
  enrolment.py   claim, renew with backoff, and alarm early
  clock.py       plausibility, and what is disciplining this clock
deploy/          systemd unit, installer, environment, udev rule, and the
                 device inventory for the Pi described in HARDWARE.md
```

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# First boot: claim a code. Or leave it and use the console.
.venv/bin/python -m gsu enrol --token XXXX-XXXX-XXXX

# Run. Console on http://127.0.0.1:8088
GSU_BROKER_URL=redis://127.0.0.1:6380/0 .venv/bin/python -m gsu run

.venv/bin/python -m gsu preflight  # everything that must be true before it works
.venv/bin/python -m gsu devices    # what is fitted, and what was found
.venv/bin/python -m gsu bench      # what a tick costs — run this on the Pi
.venv/bin/python -m gsu camera     # take a few frames, and what each one costs
.venv/bin/python -m gsu stream --seconds 30   # run the H.264 encoder, measure it
.venv/bin/python -m unittest discover -s tests -t . -q
```

## Video, which is one thing and one owner

**The live stream is the only video this station sends.** H.264 or HEVC,
1080p30, and **off until the platform asks**. `video.start` carries a lease and the
stream stops when the platform stops renewing it, because the failure to design
for is a console closing while the station keeps paying for a picture nobody can
see. Which encoder does the work — a hardware block or x264 on the CPU — is
probed at start-up and reported in telemetry, so moving between boards is a
setting rather than a rewrite.

It is verified against the running platform as **fragmented MP4 over a
WebSocket** to `wss://…/media/ingest`, authenticated with the same credential as
the broker and opened only while streaming. The station muxes the fMP4 itself,
so all three encoders — hardware, software and synthetic — produce identical
container output. Measured at 1080p30: 459 fragments, none dropped
(HARDWARE.md §9).

**There used to be a second half: a snapshot channel on
`gsu/{station_id}/video`, one JPEG at 2 fps.** It is gone. Two readers of one
CSI sensor was the cause of a camera wedge that survived four fixes, and
removing one reader deletes the class of bug rather than narrowing it —
DECISIONS.md item 45 for the reasoning, CONTRACT-QUESTIONS.md item 17 for what
the platform stops receiving. The setup page keeps a preview, taken on demand
while somebody is looking and sent nowhere.

**Who owns the camera is now an explicit, reported thing.**
`gsu/camera/ownership.py` hands the sensor to one named holder at a time, and
`video.sensor` in the health frame says who has it. A camera that is *busy* and
a camera that is *broken* had looked identical from the platform for the whole
life of that bug.

`gsu camera` and `gsu stream` need no platform, no network and no enrolment,
which is the point of them: they are how the first person with a Pi finds out
whether the camera and the encoder work.

**To put one on a Raspberry Pi, read `DEPLOYMENT.md`.** It goes from a blank SD
card to an enrolled station, running as a **container** (`deploy/install.sh`,
`deploy/Dockerfile`, `deploy/docker-compose.yml`). Running it as a plain systemd
service is fully supported too — `install.sh --path systemd`, DEPLOYMENT.md
Appendix B.

Containers won on one constraint: these stations are hard to reach physically,
so an update is the riskiest routine operation there is. `deploy/gsu-update.sh`
pulls on a jittered timer, **proves the new image enrols and publishes before
keeping it**, and rolls back to the image already on disk if it does not —
no download, over a link that may be why you are rolling back. DEPLOYMENT.md §14
and DECISIONS.md items 35a–c and 39; the reversal history is kept deliberately.

`GSU_BROKER_URL` overrides only the broker *address* — an address and nothing
else, never credentials — and the username and topics still come from enrolment.
It exists because the platform hands out an address that may only be routable
from inside its own network.

Other environment: `GSU_HOME` (state directory), `GSU_PLATFORM_URL`,
`GSU_SETUP_HOST`/`GSU_SETUP_PORT`/`GSU_SETUP=0`,
`GSU_SETUP_PASSWORD_HASH`/`GSU_SETUP_WINDOW_MINUTES`, `GSU_AIRBAND_TRAFFIC`
(`off`/`low`/`busy`), `GSU_ENROL_TOKEN`.

## The setup GUI, and what stops it being the weakest thing here

A station that boots unconfigured serves a web page: enter the enrolment code,
pick the device fitted in each slot from the same registry `gsu devices` reads,
and read back what the box thinks it is — including *why* the camera is on the
slow capture path, which is the question that otherwise costs an SSH session.
The platform's address is shown and **is not editable**: there is one platform,
it is fixed in the environment file, and an address that can be retyped on a
roof is a station that enrols against nothing.

It is also an HTML form on a box at the far end of the internet, which is the
shape of every device that has ever been mass-compromised. So four controls
stand in front of it, each a default rather than something to remember:

- **loopback unless told otherwise** — `GSU_SETUP_HOST` still defaults to
  `127.0.0.1`, reached over an SSH tunnel, which needs no password because SSH
  has already authenticated the caller;
- **a per-box password, or no LAN listener at all.** Set `GSU_SETUP_PASSWORD_HASH`
  (`python -m gsu setup-password`) or a non-loopback `GSU_SETUP_HOST` is ignored.
  This is structural, not a check: there is no path that binds a routable socket
  without a secret in front of it, so forgetting one gives an unreachable page;
- **private source addresses only**, hand-written rather than `is_private` —
  carrier-grade NAT is *not* local on a Starlink site;
- **a window that closes.** Once enrolled and idle, the LAN socket is closed and
  rebound to loopback. Reopening is deliberate: reboot, or `touch
  $GSU_HOME/setup-open`.

Plus CSRF on every form, a `Host` check that defeats DNS rebinding, bounded
request bodies, per-peer lockout, and no secret ever rendered back into the
page. The reasoning for all of it is in `gsu/setup_access.py`; the residual risk
— it is plain HTTP, so anyone already on the setup network can read the password
off the wire — is stated there too rather than left to be discovered.

Trust is **two roots, not one**: the broker is always pinned to the private CA
from `broker.ca_pem` (persisted 0600, pre-provision with `GSU_CA_FILE`), and the
platform API is verified against the system CA bundle unless `GSU_API_CA_FILE`
pins it — because the API is expected behind a reverse proxy with a public
certificate. `GSU_REQUIRE_TLS=1` refuses plaintext outright. **Nothing here can
turn verification off**, and nothing falls back to plaintext when TLS fails —
see `gsu/tls.py` and DECISIONS.md items 22 and 36.

## Read next

- **DEPLOYMENT.md** — the runbook: blank SD card to enrolled station, how to
  tell it is working, how to read the logs, how to recover.
- **DECISIONS.md** — every assumption that needs a human, and the five open
  decisions from `contract/enrolment.md` §9, still open. Items 21–34 are the
  deployment session and all need review.
- **CONTRACT-QUESTIONS.md** — ten things the contract could not express when
  this was built. Four are settled (`available: false`, optional
  `humidity_pct`, positionless contacts stay dropped, and the conformance
  harness); the rest are open, and item 10 is new now that TLS has landed.
  Nothing in `contract/` was edited from this side.
- **HARDWARE.md** — what the Pi 2B, the single RTL2838 and the Airmar 110WX can
  and cannot carry, measured where measurable and marked where not. §7 is the
  register of what has actually been run against hardware and what has not.
