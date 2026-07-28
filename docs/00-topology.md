# System Topology (canonical)

The authoritative description of how the pieces relate. Everything else in `docs/` is
subordinate to this — where another document disagrees, this one wins.

```
   ┌──────────────────────────┐         ┌──────────────────────────┐
   │   Ground Station Unit    │         │   Ground Station Unit    │
   │  ┌────────────────────┐  │         │  ┌────────────────────┐  │
   │  │ camera  ADS-B      │  │         │  │ camera  ADS-B      │  │
   │  │ radio   weather    │  │   ...   │  │ radio   weather    │  │
   │  │ light   solar/power│  │         │  │ light   solar/power│  │
   │  └─────────┬──────────┘  │         │  └─────────┬──────────┘  │
   │            │             │         │            │             │
   │    ┌───────▼────────┐    │         │    ┌───────▼────────┐    │
   │    │ onboard compute│    │         │    │ onboard compute│    │
   │    └───────┬────────┘    │         │    └───────┬────────┘    │
   └────────────┼─────────────┘         └────────────┼─────────────┘
                │                                    │
                │   authenticated, outbound only     │
                │   (Starlink, CGNAT — no inbound)   │
                └──────────────┬─────────────────────┘
                               │
                    ┌──────────▼───────────┐
                    │     Main Server      │
                    │  ─────────────────   │
                    │  identity & orgs     │
                    │  per-station grants  │
                    │  authorised re-      │
                    │    broadcast         │
                    │  history & audit     │
                    └──────────┬───────────┘
                               │
              authorised, per-user, per-station
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   ┌────▼─────┐          ┌─────▼────┐           ┌─────▼────┐
   │  user    │          │  user    │           │  user    │
   │  (org A) │          │  (org A) │           │  (org B) │
   └──────────┘          └──────────┘           └──────────┘
```

## Rules

1. **Ground Station Unit (GSU)** — sensors plus an onboard computer. The onboard computer
   is the only thing that talks to the outside world.
2. **Each GSU belongs to exactly one org.** That binding lives in the server's device
   registry and is never read from anything the GSU sends.
3. **The GSU authenticates to the server** and pushes data outbound. Starlink CGNAT means
   inbound connections are impossible anyway, so outbound-only is both the security model
   and the network reality.
4. **Users belong to orgs.** An org may have many GSUs.
5. **An interface displays one GSU at a time** and switches between them — a second switcher
   below the existing org switcher. The constraint binds a single interface, not the user:
   one person may have several tabs or browsers open on different stations at once.
6. **Permissions are per user, per ground station, per operation.** Access to an org does
   not imply access to all of its stations, and access to a station does not imply every
   operation on it.
7. **The server re-broadcasts.** All authorisation happens here.
8. **Nothing flows directly from a GSU to a user.** No peer-to-peer, no direct media path,
   no GSU-hosted endpoint a browser ever reaches. Every byte a user sees has passed through
   the server and been authorised there.

## Consequences worth stating explicitly

**Rule 8 rules out direct WebRTC.** `01-architecture-notes.md` originally offered
browser-to-edge WebRTC as one of two media options. That is now excluded — even
TURN-relayed WebRTC is usually configured to prefer a direct peer path, and the intent here
is that no such path exists. Media is GSU → server → viewer, with the server terminating and
re-originating the stream.

**Rule 5 binds the connection, not the session.** The active station is pinned on each
realtime connection, so at any moment a given socket is authorised for exactly one (org,
station) pair and cannot be asked for another. A user with three tabs open has three
connections, each independently pinned and independently authorised.

Pinning it on the *session* instead would have been a stronger-sounding constraint and a
worse one: a session spans tabs, so switching station in one tab would yank the others. It
would also buy nothing — a user who can reach two stations by switching back and forth can
already see both, so forcing them through one socket protects nothing.

What the pin is actually for: it means a socket cannot be talked into serving a station it
was not authorised into, and it keeps the high-rate subscription set bounded and explicit.
Authorisation itself is always evaluated against the user's grants, never against the pin —
see `03-realtime-isolation.md` §4.

**But rule 5 creates a problem it must not solve badly.** If a user is watching station A,
they still need to know when something happens at station B. Alerting cannot be scoped to
the active station. This forces two distinct channel classes — see
`03-realtime-isolation.md` §5.

**Rules 5 and 8 together make on-demand media the obvious default.** Since the server is
the only consumer of a GSU's media and it knows whether anyone is currently watching, a GSU
has no reason to push video or audio uplink when nobody is attached. On a metered link that
is the difference between a continuous multi-hundred-kbit/s uplink per station and near
zero. Telemetry and alerts stay always-on; media is pulled when a viewer attaches. Anything
needing a continuous record is recorded locally at the GSU and uploaded on event or on
request.
