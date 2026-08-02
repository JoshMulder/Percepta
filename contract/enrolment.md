# Enrolment and provisioning

How a physical box becomes a ground station the platform will accept data from,
and everything it needs to know to operate afterwards.

**Both sides are built** — the platform in
`server/app/backend/services/enrolment.py`, `api/enrolment.py` and
`api/station_enrolment.py`, the station in `station/gsu/enrolment.py` and
`gsu/credentials.py` — and this document describes the behaviour as it runs.

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
    ├─ issue enrolment token ───►│  short-lived, one station   │
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

**Issue.** The admin generates an enrolment token for that record. Short-lived
(**24 hours** by default), bound to that one station id. Displayed once and
stored only as a hash — it is both a secret and a lookup key, so it is hashed
for lookup and never recoverable afterwards. Losing it means issuing another,
which is cheap and auditable, and issuing a new code revokes any outstanding
one.

A token stays claimable until it expires or is revoked, rather than being
strictly single-use, and `claim_count` records how often it was used. This is
what makes enrolment resumable: a technician who loses signal mid-claim retries
with the same code and no admin involvement. Each claim issues a *fresh*
credential and revokes the previous one — secrets are stored hashed and are
unrecoverable, so the same credential cannot be returned twice, and making it
recoverable would mean the operator could impersonate a customer's station. The
cost is that an accidental re-claim cuts off a box that had already succeeded,
which requires someone to physically re-enter the code.

**Claim.** The box sends the token to the enrolment endpoint — plus its public
key on the mTLS path (§3), which the platform accepts and ignores while
credentials are bearer secrets, so the station side can send it from the start.
The platform verifies the token, records the credential against the station,
and returns everything in §4.

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

Unauthenticated — the token *is* the authentication. Rate limited by source,
though the real defence against guessing is the token's ~58 bits of entropy,
not the limit: rate limiting deliberately **fails open** if its backing store is
unavailable, because an outage in an unrelated component should not strand a
technician on site.

```jsonc
// request
{
  "token": "…",                      // as issued; claimable until it expires
                                     // or is revoked, so a dropped connection
                                     // can be retried (§2)
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
    "url": "wss://platform.example/broker",
                                     // Carries no credentials, ever. redis-py
                                     // lets a URL's credentials silently
                                     // override keyword arguments - see
                                     // transport.md - so the address is an
                                     // address and identity travels separately.
    "ca_pem": "…",                   // the CA to pin, when ca_mode is
                                     // "pinned". Persist it beside the
                                     // credential. It is the broker's trust
                                     // root, not the API's. Must carry
                                     // basicConstraints CA:TRUE and keyUsage
                                     // keyCertSign: redis-cli accepts a CA
                                     // without them and Python's ssl refuses
                                     // it, so a bare CA passes a command-line
                                     // check and fails every real station.
    "ca_mode": "pinned" | "system",  // HOW to verify the broker. "pinned" =
                                     // against ca_pem and nothing else.
                                     // "system" = against the OS trust store,
                                     // because this platform is behind a
                                     // publicly trusted certificate and its
                                     // own private CA is not on the wire.
                                     // The platform is the only party that
                                     // knows which, so it says: a null ca_pem
                                     // alone cannot tell "not sent yet" from
                                     // "use the public roots", and a station
                                     // that guesses is wrong in one direction
                                     // or the other. Absent (an older
                                     // platform) means "pinned", so a station
                                     // that has nothing to pin refuses rather
                                     // than downgrading.
    "username": "gsu:{station_id}",  // the broker principal to authenticate as
    "telemetry_topic": "gsu/{station_id}/telemetry",
    "audio_topic": "gsu/{station_id}/audio",
    "command_topic": "cmd/gsu/{station_id}",
    "media_url": "wss://platform.example/media/ingest"
                                     // where the live H.264 goes. Not a
                                     // broker channel - video is bulk data
                                     // and never touches the broker - but the
                                     // same credential opens it, so it is
                                     // stated here rather than built out of
                                     // the API's address by the station.
                                     // Absent means derive it from the API
                                     // host, which is what an older platform
                                     // leaves a station to do.
  },
  "station": {
    // What the station is told it is, at the moment it enrols. The name is
    // settled here and changed only by an admin. The position is the
    // station's *initial* value: from here on the box owns it and reports it
    // in health.position, and the console renders it read-only. See §7.
    "name": "Kaikoura Ridge",
    "timezone": "Pacific/Auckland",
    "latitude": -42.4004,
    "longitude": 173.68,
    "elevation_m": 310,              // part of the position. The station's
                                     // barometric altitude correction is
                                     // computed from this and refuses without
                                     // it rather than assuming sea level.
    "organization": "Coastal Aero",  // which tenant this box now belongs to,
                                     // echoed back so the person at the box
                                     // can see they enrolled it into the
                                     // right one - a code carries no visible
                                     // clue whose it is.
    "locality": "Kaikoura, Canterbury"
                                     // where the position is, in words,
                                     // derived by the platform - so a person
                                     // on site can tell at a glance that the
                                     // coordinates are where they stand.
  },
  "config_version": 3
}
```

Failures, and the response the technician sees:

| Cause | Status | Shown as |
|---|---|---|
| Token unknown, expired or revoked | `404` | "This code is not valid. Ask for a new one." |
| Station already in service, and this code predates that | `409` | "This station is already set up." |
| Too many attempts from one source | `429` | "Too many attempts. Wait a minute and try again." |
| Malformed request | `422` | — |

**`429` is retryable and must be treated as such.** It is the one failure here
that clears on its own, and a station that gave up on it would strand the
technician the rate limit was never aimed at. Back off and try again.

**Deliberately not distinguished:** unknown, expired and revoked all return the
same thing. Telling an attacker which of those a guess was is free information
about the token space, and none of the three changes what the technician does.

**A retry supersedes.** Claiming the same token again issues a fresh credential
and revokes the earlier one (§2) — a technician who loses signal mid-enrolment
finishes without an admin issuing anything new. The `409` exists for the other
case: a station already in service under a *different* token, where re-enrolling
would cut off working hardware.

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

**How the trust roots get onto the box.** The broker's CA bootstraps itself:
`broker.ca_pem` arrives inside the enrolment response and is persisted beside
the credential. The API is the chicken-and-egg — the first `POST /api/enrol`
carries the token over a connection the station cannot yet have been told how to
verify. Where the platform sits behind a publicly trusted certificate, the
system trust store covers it and nothing need be carried. Where the platform
serves its own private CA, that CA must reach the box out of band — copied on at
install, its fingerprint checked by eye — because trusting the first thing the
network shows you would let whoever intercepts that first call issue the
credential. The station refuses to enrol over a link it cannot verify, rather
than trusting on first use.

---

## 6. Clock, expiry and the failure this causes

**A remote station with a wrong clock cannot authenticate**, and if the
credential has already expired it cannot renew either. That is a site visit.

- The station syncs time before enrolling and refuses to enrol with an
  implausible clock, saying so.
- **A GPS receiver is the intended long-term time source.** It solves this
  properly on hardware with no battery-backed clock. Until one is fitted, an RTC
  module is the cheap interim; NTP alone does not help a box that cannot reach
  the network because its clock is wrong.
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

**Today the station owns all of it, and nothing is pushed.**

- The station reports its `config_version` in telemetry, and the platform
  records it against the station as a display of what is running.
- `config.set` exists and the station implements it, but **the platform never
  sends one.** The only settings it holds that the station also has are
  position and elevation, and those must not be settable from two ends: two
  editable copies of one fact disagree, and the disagreement is invisible from
  both.
- **Position flows the other way, and only the other way.** The station
  reports `health.position` (latitude, longitude, optional `elevation_m`, and
  `source` saying whether it was typed or fixed by GPS); the platform stores
  what it is told, derives the locality from it, and renders it read-only.
  Enrolment supplies the starting value because a box has to have one before
  anybody has stood at it. Omitting the field changes nothing stored; `null`
  retracts. This is the one case where the station is the author of something
  the platform also displays, and it is settled that way because the person at
  the site is the one who knows.
- So site policy — alert thresholds, retention, stream settings — is typed on
  the setup page by somebody at the box. Every threshold in it has to work with
  the platform unreachable, which is the same reason it lives there.

This paragraph used to say the platform sends `config.set` when its version is
newer. It could not: `config_version` was written by nothing, so the platform's
copy sat at 1 for ever and was never newer than anything.

**If the platform is given policy of its own** — fleet-wide alert thresholds
are the obvious candidate — that is when to build the push, and it needs a
stated answer to which side wins when both have edited. The station's half is
already there: `config.set` applies, persists and reports the new version, and
the platform never assumes the change took.

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

## 10. What each side built

**Platform**
- `station_enrolment_tokens` and `station_credentials`, both hashed, both under
  RLS. A credential decides which tenant a box may publish as, so a query
  against it that escaped its org scope would be the worst leak in the schema
- `POST /api/enrol`, `/api/enrol/renew`, `GET /api/enrol/status`
- Admin API to issue and revoke, behind `config.write`, at
  `/api/stations/{id}/enrolment`
- Broker principals derived from station identity, one per station, pinned to
  its own channels (`services/broker_acl.py`), with the broker's `default` user
  closed — `server/docker-compose.yaml` passes `--requirepass` and the stack
  will not start without it, so per-station principals are a second layer
  rather than the only one
- Every issue, claim, renew, revoke and rejection audited
- `scripts/verify_enrolment.py` exercises the lifecycle against a running stack

**Station**
- Credential storage in a permissions-restricted file, with the pinned CA
  beside it (`gsu/credentials.py`; the seam for a hardware keystore)
- Setup page and the claim exchange, resumable
- Renewal with overlap, and a health alarm when it is failing
  (`gsu/enrolment.py`)
- Time sync, and refusing to enrol with an implausible clock (`gsu/clock.py`)
- Config apply, persist, and report version
- Device discovery, and reporting what is actually attached
