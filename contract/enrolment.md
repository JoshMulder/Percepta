# Enrolment and provisioning

How a physical box becomes a ground station the platform will accept data from,
and everything it needs to know to operate afterwards.

**The platform side is built** (`server/app/backend/services/enrolment.py`,
`api/enrolment.py`, `api/station_enrolment.py`). The station side is not. What
follows is still the specification; §11 records where the implementation differs
from it and why, and every difference there is deliberate.

It was written before either side existed because enrolment is where the
platform's tenancy guarantee is actually established — every other isolation
control assumes the station's identity is trustworthy, and this is the step that
makes it so.

---

## 1. What this has to get right

**The organisation is decided by a person, not by a device.** An admin creates
the station record in an org; the box never says which tenant it belongs to,
before or after enrolment. This is the rule the rest of the platform rests on
(`contract/README.md`), and enrolment is where it could most easily be lost.

**A stolen box compromises only itself.** No secret is shared between stations.
There is no fleet-wide key, no image containing credentials, and nothing that
lets one recovered enclosure produce a second working station.

**Installation is done by a technician, not a developer.** On a hillside, in
weather, possibly with a phone rather than a laptop. Any step requiring a
terminal, a config file edit, or a copied UUID is a step that will be done wrong.

**Nothing reaches inward.** Starlink is CGNAT; the platform can never initiate a
connection to a station. Every exchange here is station-initiated, including
renewal and revocation checks.

**The link is unreliable during installation too.** Enrolment must be resumable:
a technician who loses signal halfway must be able to retry without an admin
issuing anything new.

---

## 2. Lifecycle

```
  admin                      technician                    station
    │                            │                            │
    ├─ create station ──────────►│                            │
    │  (org, name, location)     │                            │
    │                            │                            │
    ├─ issue enrolment token ───►│  short-lived, single-use    │
    │                            │                            │
    │                            ├─ enter token on the box ──►│
    │                            │                            │
    │                            │       ┌────────────────────┤ POST /api/enrol
    │                            │       │  token + public key│
    │                            │       ▼                    │
    │                            │   platform verifies,       │
    │                            │   binds to the station     │
    │                            │   record, returns          │
    │                            │   credential + config      │
    │                            │                            │
    │◄─ station appears online ──┴───────────────────────────►│ operating
```

**Create.** An org admin creates the station record: name, timezone, location,
map extent. It exists, belongs to exactly one org, and has no credential — so it
can be configured and granted to users before any hardware exists.

**Issue.** The admin generates an enrolment token for that record. Single-use,
short-lived (**24 hours** by default), bound to that one station id. Displayed
once and stored only as a hash — the same treatment DroneOps gives its calendar
feed token, and for the same reason: it is both a secret and a lookup key, so it
is hashed for lookup and never recoverable afterwards. Losing it means issuing
another, which is cheap and auditable.

**Claim.** The box generates its own keypair, keeps the private half, and sends
the token plus its public key to the enrolment endpoint. The platform verifies
the token, marks it used, records the credential against the station, and
returns everything in §4.

**Operate.** The station authenticates with its credential on every connection.
The broker ACL pins it to its own channels.

**Renew.** Credentials expire. The station renews using its *current* credential,
well before expiry — see §6, because this is where remote sites fail.

**Revoke / replace.** An admin can revoke a station's credential at any time.
Replacing hardware reuses the same station record — same id, same history, same
grants — with a fresh token. The record outlives the box.

---

## 3. Credential: start simple, keep the upgrade path

Two options, and the recommendation is deliberate.

**Now: a per-station bearer credential.** A long random secret, unique per
station, used as the broker password with the station id as the username. Stored
hashed on the platform, in the OS keystore or a permissions-restricted file on
the station.

**Later: mTLS.** The station generates a keypair and CSR, the platform's CA
signs it, the broker authenticates on the certificate and derives the ACL from
its subject. Stronger — the secret never crosses the wire even once, and
expiry/revocation are built in.

**Recommendation: build the flow for mTLS and start with the bearer credential.**
Standing up and operating a CA before a single station exists in the field is
effort spent ahead of the risk it addresses, and a CA that nobody has rehearsed
renewing is its own outage. The enrolment exchange below is deliberately shaped
so that swapping the credential type changes the payload and nothing else — not
the lifecycle, not the endpoints, not the technician's steps.

**Either way the private half is generated on the box and never leaves it.** A
platform that can hand out a station's credential is a platform whose operator
can impersonate a customer's station.

---

## 4. The exchange

### `POST /api/enrol`

Unauthenticated — the token *is* the authentication. Rate limited by source, and
tokens are single-use, so a brute-force attempt is bounded by the entropy of the
token rather than by the rate limit alone.

```jsonc
// request
{
  "token": "…",                      // as issued, single use
  "public_key": "…",                 // PEM; omitted while using bearer credentials
  "hardware": {                      // for the fleet inventory, not for trust
    "model": "…",
    "serial": "…",
    "os": "…",
    "agent_version": "…"
  }
}
```

```jsonc
// 200
{
  "station_id": "uuid",
  "credential": {
    "type": "bearer",
    "secret": "…",
    "expires_at": "…",
    "renew_after": "…"               // when to start renewing; the platform
                                     // owns this policy and states it, rather
                                     // than each station hardcoding half a life
  },
  "broker": {
    "url": "mqtts://broker.example:8883",
    "ca_pem": "…",                   // pinned; the station verifies the platform
                                     // against this CA and no other. SENT.
                                     // Persist it beside the credential and use
                                     // it for the broker and this API both.
    "username": "gsu:{station_id}",  // the broker principal to authenticate as
    "telemetry_topic": "gsu/{station_id}/telemetry",
    "audio_topic": "gsu/{station_id}/audio",
    "command_topic": "cmd/gsu/{station_id}"
  },
  "station": {
    "name": "Kaikoura Ridge",
    "timezone": "Pacific/Auckland",
    "latitude": -42.4004,
    "longitude": 173.68
  },
  "config_version": 3
}
```

Failures, and the response the technician sees:

| Cause | Status | Shown as |
|---|---|---|
| Token unknown, expired or already used | `404` | "This code is not valid. Ask for a new one." |
| Token valid, station already enrolled | `409` | "This station is already set up." |
| Malformed request | `422` | — |

**Deliberately not distinguished:** unknown, expired and already-used all return
the same thing. Telling an attacker which of those a guess was is free
information about the token space, and none of the three changes what the
technician does.

**Idempotent within the window.** A retry with the same token *from the same
station* after a dropped connection returns the same credential rather than
failing — the failure mode this prevents is a technician stuck on a hillside
with a used token and no way to finish.

### `POST /api/enrol/renew`

Authenticated with the current credential as `Authorization: Bearer <secret>`.
Returns the same shape as a claim. The old credential remains valid for an
overlap period (§6).

The station does **not** send its own id. It is derived from the credential, so
a box holding a valid secret still cannot assert which station it is.

### `GET /api/enrol/status`

Authenticated the same way. Thin by design — it is for a box confirming it is
still trusted, not an API surface.

```jsonc
{
  "station_id": "uuid",
  "name": "Kaikoura Ridge",
  "config_version": 3,
  "credential_expires_at": "…",
  "renew_now": false,
  "server_time": "…"       // a reference clock, never an authority
}
```

`server_time` exists for a specific failure: a box with no battery-backed clock
cannot evaluate its own expiry, and one that wrongly believes it has expired
behaves as badly as one that wrongly believes it has not. §6 still holds — the
platform must never be a station's only clock.

---

## 5. What the technician actually does

The design target is: **power it on, enter a code, watch for a green light.**

- The token is short enough to read aloud and type — group it (`XXXX-XXXX-XXXX`)
  rather than presenting a UUID.
- Entry is via a local setup page the box serves on its own Wi-Fi or Ethernet,
  not a terminal.
- The page shows what happened in the technician's terms: reached the platform,
  enrolled, sensors found, first telemetry sent. Not a log.
- The station id is **never** typed by a human. It comes back in the response.
  Anything a human retypes is something a human eventually mistypes, and this is
  the field where a mistake attaches a box to the wrong customer.

---

## 6. Clock, expiry and the failure this causes

**A remote station with a wrong clock cannot authenticate**, and if the
credential has already expired it cannot renew either. That is a site visit.

- The station syncs time before enrolling and refuses to enrol with an
  implausible clock, saying so.
- **A GPS receiver is the intended long-term time source.** It solves this
  properly on hardware with no battery-backed clock, which is the case on the
  current Raspberry Pi. Until one is fitted, an RTC module is the cheap interim;
  NTP alone does not help a box that cannot reach the network because its clock
  is wrong.
- If the hardware has no battery-backed clock, this must be stated in the
  station's own docs — it changes the boot sequence.
- **Renew early and often.** Begin at half the credential's life, retry with
  backoff, and treat failure to renew as a health alarm reported over telemetry
  long before it becomes an outage.
- **Overlap.** A renewed credential does not instantly invalidate the previous
  one; both work for a defined window, so a station that renews and then loses
  power mid-swap is not locked out.
- **Never let the platform be the only clock authority.** If the platform is
  unreachable the station keeps operating locally, and a credential nearing
  expiry with no way to renew is an alarm, not a shutdown.

---

## 7. Configuration after enrolment

Enrolment returns identity and enough to connect. Everything else is
configuration, and it changes over a station's life.

**Delivered on the command channel**, versioned, station-initiated:

- The station reports its `config_version` in telemetry.
- The platform sends `config.set` when its version is newer.
- The station applies, persists, and reports the new version. The platform never
  assumes the change took — same rule as every other command.

What configuration covers:

| | |
|---|---|
| Device inventory | Which sensors are present, and how to reach them: camera address and ONVIF credentials, Modbus unit ids, serial ports, receiver band |
| Thresholds | Proximity alert distance and altitude, low-battery levels, wind alarms |
| Retention | How much video and audio to keep locally, and for how long |
| Duty cycling | What to shed, and at what state of charge |
| Cadence | Telemetry rates, if a site needs to differ |

**Sensor credentials are secrets.** Camera passwords sent as configuration are
secrets at rest on both sides — encrypted in the platform's database, and stored
by the station no less carefully than its own credential.

**The station owns the truth about what is attached.** It reports the devices it
actually found; the platform's `devices` table should reconcile against that
rather than assume. A camera that has failed and a camera that was never fitted
look identical in a database and completely different at the site.

---

## 8. Revocation and decommissioning

- **Revoke** — the credential stops working at the broker immediately, and the
  station is told if it is still connected. It keeps recording locally; it is
  cut off, not disabled.
- **Replace hardware** — same station record, new token. Grants, history,
  configuration and map extent all survive. The record is the site; the box is
  a part.
- **Decommission** — the record is deactivated. Everything that references it
  keeps working: `station_grants` cascade, history is retained under the org's
  policy, and audit rows deliberately keep the station id even after the station
  is gone.

---

## 9. Open decisions

These need a human, and the station agent should not invent answers.

1. **Compute platform.** Determines whether a hardware-backed keystore is
   available, whether there is a real-time clock, and how the setup page is
   served.
2. **Who installs, and with what.** Phone or laptop changes the setup-page
   design; a subcontractor rather than staff changes how much the token is
   trusted to.
3. **Token lifetime.** 24 hours suits install-on-the-day. A box shipped to a
   site and installed a fortnight later needs longer, or a token issued at
   install time by someone remote.
4. **Broker: managed or self-hosted**, and therefore whether mTLS is a
   configuration change or a project.
5. **Software update path.** Not enrolment, but the same trust root, and a
   station that cannot be updated safely is a station that cannot be fixed.

---

## 10. What each side builds

**Platform — built**
- `station_enrolment_tokens` and `station_credentials`, both hashed, both under
  RLS. A credential decides which tenant a box may publish as, so a query
  against it that escaped its org scope would be the worst leak in the schema
- `POST /api/enrol`, `/api/enrol/renew`, `GET /api/enrol/status`
- Admin API to issue and revoke, behind `config.write`, at
  `/api/stations/{id}/enrolment`
- Broker principals derived from station identity, one per station, pinned to
  its own channels (`services/broker_acl.py`)
- Every issue, claim, renew, revoke and rejection audited
- `scripts/verify_enrolment.py` exercises the lifecycle against a running stack

**Platform — still owed**
- **Locking down the broker's default user.** Per-station principals exist and
  are enforced *for anyone who uses them*; Redis' `default` user is still open
  and unauthenticated, so the pinning is not yet a boundary. This is the last
  gap between the model described here and reality
- Console UI for issuing codes — the API is there, nothing renders it
- Configuration delivery (§7). `config_version` is issued; `config.set` is not
- mTLS, and the CA to go with it (§3)

**Station**
- Keypair or credential generation, and secure local storage
- Setup page and the claim exchange, resumable
- Renewal with overlap, and a health alarm when it is failing
- Time sync, and refusing to enrol with an implausible clock
- Config apply, persist, and report version
- Device discovery, and reporting what is actually attached

---

## 11. Where the implementation differs from this document

Each of these is deliberate. Raise them rather than silently matching the code
if you disagree — that is what the contract process is for.

**A retry re-issues rather than returning the same credential.** §4 says a retry
from the same station returns the same credential. The platform cannot: secrets
are stored hashed and are unrecoverable, and making them recoverable would mean
the operator could impersonate a customer's station. A retry inside the token's
lifetime therefore issues a *fresh* credential and revokes the previous one.
This still satisfies what the clause is for — a technician who loses signal
mid-enrolment can finish without an admin issuing anything new. The cost is that
an accidental re-claim cuts off a box that had already succeeded, which requires
someone to physically re-enter the code.

**Tokens are not strictly single-use.** Same reason. A token stays claimable
until it expires or is revoked; `claim_count` records how often it was used, and
issuing a new code revokes any outstanding one.

**`ca_pem` is sent.** Both the broker and the API serve TLS from one private CA,
and the plaintext listeners are disabled outright rather than merely
discouraged - a station misconfigured to `redis://` or `http://` fails instead
of sending its credential in clear and appearing to work.

**Two fields were added**: `credential.renew_after` and `broker.username`. Both
are additive and safe to ignore.

**Rate limiting fails open.** If Redis is unavailable, enrolment proceeds. An
outage in an unrelated component should not strand a technician on site, and the
actual defence against guessing is the token's ~58 bits of entropy, not the
limit.
