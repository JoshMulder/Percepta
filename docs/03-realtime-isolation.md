# Real-time Multi-Org Isolation

How the guarantee in `00-topology.md` — users see only their org's ground stations, and only
the ones they're granted, and only the operations they're granted — is actually enforced for
*streams*. That is a materially different problem from enforcing it for database queries.

Terminology follows `00-topology.md`: **ground station** (GSU), not "site".

Depends on `02-platform-reconciliation.md` for what DroneOps provides.

---

## 1. Why the DroneOps isolation model doesn't carry over by itself

DroneOps' isolation is strong because enforcement lives **below the application**. Every
query runs through Postgres RLS with `app.current_org` set on the connection. A developer
who forgets a `WHERE organization_id = ?` still cannot leak data — the database refuses. The
default with no org set matches nothing, so mistakes fail closed.

**None of that applies to a WebSocket.** Live telemetry, video, and radio audio never pass
through Postgres. Their isolation depends entirely on application code correctly deciding,
per subscriber, what to send. There is no backstop. One bug in a fan-out loop leaks another
org's camera feed.

So the design goal is not "add auth to the WebSocket". It is: **recreate the fail-closed
chokepoint property for streams.**

There is a concrete example of the anti-pattern in the code we already have.
`Remote-Radio`'s `_broadcast_bytes` iterates `self.clients` and sends to everyone connected.
On the GSU that is correct — one station, one org, one radio, one local consumer. Reused
unchanged in the server it is a cross-org leak. The shape of that function is the thing to
design against.

---

## 2. The rule

> There is no primitive that sends to all connections. Every outbound frame goes to a
> **fan-out group**, and a connection can only be in a group it was explicitly authorised
> into.

Authorisation happens once, at subscribe time. After that, group membership *is* the
permission — so the hot path has no auth decision to get wrong, in the same way RLS moves
the decision out of the query.

---

## 3. Data model

New tables, all org-scoped and all under RLS using the existing `app.current_org` mechanism.

```
ground_stations
  id                uuid pk
  organization_id   uuid fk -> organizations    (RLS key; a GSU has exactly one org)
  name, timezone, location, is_active
  enrolled_at, last_seen_at

devices                           -- one subsystem instance on a GSU
  id                uuid pk
  organization_id   uuid fk       (denormalised for RLS; must match the station's org)
  ground_station_id uuid fk -> ground_stations
  kind              text          -- camera | radio | adsb | weather | light | power | link
  config            jsonb
  is_active

station_grants                    -- the per-user, per-station, per-operation primitive
  id                uuid pk
  organization_id   uuid fk       (RLS key)
  user_id           uuid fk -> users
  ground_station_id uuid fk -> ground_stations
  capabilities      text[]
  granted_by        uuid fk -> users
  expires_at        timestamptz NULL
  unique (user_id, ground_station_id)
```

### Capabilities

```
station.view       see the station exists, its status summary, its alerts
telemetry.view     live sensor streams (weather, power, link, ADS-B)
video.view         live and recorded camera
video.ptz          move the camera
radio.listen       receive airband audio
radio.control      retune / squelch — affects every listener on that station
radio.transmit     reserved, never granted (see §8)
light.control      floodlight
media.review       recorded media archive
config.write       thresholds, device configuration
```

Separating `radio.listen` from `radio.control` matters more than it looks — see §8.

### Grants are explicit

There is deliberately **no org-wide wildcard grant** for ordinary users. Every station a
user can reach is a row naming that station. It costs a little administrative convenience
and buys a property that matters here: the answer to "who can see station 7?" is a single
query with no inference, which is what an access review or an incident investigation
actually needs.

The one implicit path is that **org admins hold every capability on every station in their
own org** — which is what DroneOps' existing `require_admin` already means. So
`station_grants` sits *alongside* `organization_memberships.roles` rather than replacing it,
DroneOps' existing role checks keep working untouched, and there is no fork in the role
model.

---

## 4. Connection lifecycle

**Connect.** The WebSocket resolves identity through the *same* path as
`auth/dependencies.py:get_current_user` — same HttpOnly cookie, same `auth_sessions`
server-side check. It pins `(user_id, organization_id, session_id)` to the connection. This
mirrors `set_request_org_context` pinning org to the DB connection: identity is fixed for
the connection's life and is never taken from a client message.

**Select active station.** Per `00-topology.md` rule 5 an interface displays one GSU at a
time. The client sends `{"type":"select_station","ground_station_id":…}`; the server
authorises it against the user's grants and pins the station **to the connection**, alongside
the org. Any previously pinned station's subscriptions are dropped in the same operation.

The pin is per-connection, not per-session: a user may have several tabs open on different
stations, each its own socket, each independently pinned and authorised. The property worth
having is therefore **at any instant, a given connection is authorised for exactly one (org,
station) pair** — which is what stops a socket being talked into serving a station it was
never authorised into, and keeps the high-rate subscription set bounded.

**Authorisation is always evaluated against grants, never against the pin.** The pin decides
what a socket is currently serving; it is not itself a permission. This matters because the
media-ticket endpoint (§7) is plain HTTP with no socket to consult — it names its station
explicitly and authorises it directly. Both paths funnel through one underlying check,
`capabilities_for(user, org, station)`, so there is a single place where the answer to "may
this person do this here" is computed.

**Subscribe.** Client sends `{"type":"subscribe","stream":…}` — note it does *not* name a
station, because the station is already pinned; a client cannot subscribe to a station it
has not been authorised into. The server calls one function,
`authorize_stream(conn, capability)`, which checks admin-or-grant for the pinned station. On
success the connection joins group `org:{org}:gsu:{station}:{stream}`. On failure it is
refused and audited.

**Fan-out.** Publishers write to a group name. Membership was authorised at subscribe. No
code path enumerates all connections.

**Org switch.** DroneOps puts the active org in the JWT, so switching orgs mints a new
token. The frontend must tear down every subscription and reconnect. Server-side this is
already safe — the connection is pinned to the org present at connect, and a token for a
different org cannot adopt it — but the frontend has to be built knowing it.

---

## 5. Two channel classes

Rule 5 of the topology creates a problem that must not be solved badly: a user watching
station A still needs to know when something happens at station B. If alerting were scoped
to the active station, the one-at-a-time rule would make the platform unsafe rather than
just restrictive.

So there are two distinct channel classes, with different scopes and very different data
rates:

| | **Org status channel** | **Active station channel** |
|---|---|---|
| Scope | every station in the org the user holds `station.view` on | the single pinned station |
| Carries | station online/offline, health summary, alarms, ADS-B proximity alerts, link state | live telemetry, video, radio audio, camera state |
| Rate | low — events and periodic summaries | high — continuous |
| Lifetime | whole session | until the user switches station |

The org status channel is subscribed once at connect, and its membership is the set of
stations the user is granted — so it is authorised per station too, just at a coarser
capability (`station.view`). It is the only place where a connection legitimately receives
data about more than one station, and it carries nothing beyond status and alerts.

This split also happens to be what makes the bandwidth work: a user monitoring six stations
receives six low-rate status feeds, not six video and audio streams.

---

## 6. Revocation must reach open connections

This is the subtlest gap and worth calling out plainly.

DroneOps revokes access by invalidating the row in `auth_sessions`; the *next* HTTP request
then fails. That works because HTTP requests are frequent and short. **A monitoring
WebSocket makes no further requests** — it can stay open for hours. Under the DroneOps model
as-is, logout, sign-out-everywhere, password change, grant revocation, or a station being
deactivated would leave an existing live stream running.

For a platform whose whole purpose is live data, that is not acceptable. Two mechanisms,
deliberately overlapping:

1. **Push.** Publish to a Redis channel on any of: session revoked, grant changed or
   revoked, membership removed, station deactivated, certification expired. Every process
   holding WebSockets subscribes and immediately drops or re-authorises affected
   connections.
2. **Poll.** Independently, every connection revalidates its session and its grants on a
   fixed interval (60s is a reasonable start). Cheap, and it bounds worst-case staleness if
   a push is ever missed.

Push alone fails silently if a process misses the message. Poll alone leaves up to a minute
of unauthorised access. Together the worst case is bounded and the common case is instant.

---

## 7. The GSU boundary

Two directions, two different problems.

### Upward: GSU → server

The GSU is the one component physically outside our control — it sits in a box in a remote
location.

- Each GSU authenticates with **its own credential** (mTLS client cert preferred over a
  bearer token).
- Broker ACLs restrict that identity to publishing under `gsu/{station_id}/#` and nothing
  else.
- **The org is resolved server-side from the device registry, never read from the payload.**
  A GSU that claims to belong to a different org is ignored.

A compromised GSU can therefore forge its own station's data — unavoidable, it owns the
sensors — but cannot publish into another org's namespace or read anything back.

The radio server inherits this. It should be reconfigured to bind **loopback only** rather
than its current `0.0.0.0` default; the onboard computer becomes its only client. Its
complete absence of authentication is then correct by construction rather than a liability,
because nothing else can reach it.

### Downward: server → user

Per topology rule 8, nothing reaches a user directly from a GSU. For telemetry that is
automatic — it all flows through the fan-out groups above. For media it takes deliberate
effort, because the efficient thing to build is exactly the thing that is forbidden.

- **No direct WebRTC.** Even TURN-relayed WebRTC generally prefers a direct peer path, and
  the intent is that no such path exists. The server terminates the GSU's stream and
  re-originates it to viewers.
- **Short-lived stream tickets** for the media path, because the media gateway is not the
  API process and makes its own attach decisions:

```
POST /api/stations/{station_id}/devices/{device_id}/stream-ticket
  → authorize_stream(...)   — same function as §4, same pinned station
  → signed token: {user, org, station, device, capability, exp: now+60s, jti}
```

  The gateway verifies signature and scope only. Tickets are short-lived, single-use (`jti`
  tracked in Redis), and scoped to one device. There are no long-lived or guessable stream
  URLs anywhere in the system. Issuance is audited.

- **Media is pulled, not pushed.** The server knows whether anyone is attached, so a GSU
  uplinks video or audio only while at least one authorised viewer is watching. On a metered
  link this is the difference between a continuous per-station uplink and near zero.
  Continuous recording, where needed, happens locally on the GSU and uploads on event or on
  request.

---

## 8. Airband, receive-only — current scope

Confirmed scope: 108–137 MHz AM, receive only. Transmit comes later against a certified
transceiver.

The existing DSP chain is fit for purpose unchanged and `NullTransmitter` stays in place.
Two things are worth doing now anyway:

- **`radio.transmit` exists in the capability list from day one but is never granted.** The
  UI shows it disabled, mirroring how the radio client already handles `tx_capable`. When
  the certified radio arrives, the permission model, the certification check (DroneOps'
  `UserCertification` with computed expiry) and the exclusive-control lease are all already
  built and tested.
- **Bandwidth work is worth doing before deployment, not after.** 384 kbit/s per listener,
  continuous, is the platform's largest single consumer. Squelch-gating the stream (the
  server already tracks squelch state) plus Opus at 16–24 kbit/s reduces this by well over
  an order of magnitude. Airband is silent most of the time, so the squelch gate alone is
  most of the win. Combined with on-demand pull (§7) the idle cost is zero.

**Tuning is a shared mutating action.** One dongle, one frequency, every listener on that
station hears the result. `radio.control` is therefore separate from `radio.listen`, and
holding it requires an **exclusive lease** (Redis, with a timeout and a visible holder) so
two operators cannot fight over the frequency. This is the same lease primitive `video.ptz`
needs, and later `radio.transmit`.

---

## 9. Platform admin in real time

DroneOps' platform admin bypasses RLS while acting in the Platform org. Extending that
literally to streams would let one account attach to every camera and radio on the platform,
which is a large blast radius for a support function.

Proposed constraint: platform admins may attach cross-org to **read-only** streams, never to
actuator capabilities (`video.ptz`, `radio.control`, `light.control`, `radio.transmit`), and
every cross-org attach is written to the audit log naming the target org and station.
Physical control of a customer's hardware should require a grant in that customer's org,
deliberately made.

---

## 10. Open questions

1. Revalidation interval — 60s proposed; driven by how fast a revocation must take effect.
2. Whether `station_grants.expires_at` is needed in phase one, or whether manual revocation
   plus certification expiry is sufficient.
3. ~~Does an operator need to *hear* radio from one station while *watching* another?~~
   **Decided: no.** One station at a time applies to audio as well. The connection stays
   pinned to a single (org, station) pair with no exceptions, and switching station drops
   the audio subscription along with everything else.
