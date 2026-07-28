# Reconciliation: DroneOps and Remote-Radio

What the two existing codebases actually are, what carries over to the DITB platform, and
where the earlier assumptions in `01-architecture-notes.md` were wrong.

Sources read: `/home/percepta/droneops` (commit 9a9321b), `/home/percepta/Remote-Radio`
(commit 6f1edd0).

---

## Part 1 — DroneOps

### What it actually is

The README undersells it. It calls itself a "flight log analyzer", but the model layer
describes a full agricultural drone operations platform: jobs, clients, crew assignment,
checklists, risk assessments, incidents, safety hazard register, maintenance events,
equipment, documents, airspace authorisations, financials with Xero integration, calendar,
and reporting.

For our purposes the important characterisation is: **it is an upload-and-analyse platform,
not a real-time control platform.** Data arrives as files, gets parsed by a background
worker, and is queried afterwards. There is no device connectivity layer, no live telemetry,
no command path to hardware. The only thing resembling liveness is `worker_heartbeat`.

That is not a criticism — it is exactly the right shape for what it does. But it means the
entire real-time half of the DITB platform is new construction, not adaptation.

### Stack

| Layer | Choice |
|---|---|
| API | FastAPI 0.139, Pydantic 2 |
| ORM / migrations | SQLAlchemy 2.0 (`Mapped` / `mapped_column`), Alembic |
| Database | PostgreSQL via psycopg3, PgBouncer-aware |
| Cache / queue | Redis 5.2 |
| Object storage | boto3 against S3-compatible (MinIO / S3 / R2) |
| Auth extras | passlib+bcrypt, python-jose, pyotp (TOTP MFA), segno (QR) |
| Secrets at rest | Fernet, via `core/crypto.py` |
| Frontend | Hand-rolled vanilla JS — `app.js` is ~16.7k lines, plus `api.js`, one "island" |
| Deploy | Docker Compose, `scripts/bootstrap.sh` |

### The tenancy model — the best thing here, take it wholesale

`database/session.py` is genuinely well built and should be copied almost verbatim:

- **Postgres row-level security**, not application-level filtering. Two engines against two
  DB roles: a privileged owner (migrations, workers, startup sweeps) that bypasses RLS, and
  a least-privilege `NOSUPERUSER NOBYPASSRLS` app role that RLS actually constrains.
- Org context rides on the **DBAPI connection's `.info`**, not a ContextVar — deliberately,
  because FastAPI runs sync dependencies and endpoints in separate threadpool executions
  that don't share context. Applied transaction-locally via `SET LOCAL` on every `begin`,
  which stays correct under PgBouncer transaction pooling.
- **Fails closed**: with no org set, the policies match nothing. An unscoped query returns
  nothing rather than everything.
- Connection `.info` is wiped on pool checkin so one request's tenant can never leak into
  the next.

Auth (`auth/dependencies.py`) is JWT in an HttpOnly cookie with a Bearer fallback, backed by
a server-side `auth_sessions` table — so logout, sign-out-everywhere, and password change
produce *real* revocation rather than waiting out the JWT expiry.

Platform admin is modelled as membership in a special Platform org, with god mode active
only while the session's current org *is* Platform. Switched into a customer org, a platform
admin sees exactly what that org's members see and does not appear in their user list.

### Where DroneOps does *not* give us what DITB needs

**Permissions are org-wide and flat.** `organization_memberships.roles` is an
`ARRAY(String)` of values from a fixed `UserRole` enum (admin, pilot, ground_crew,
payload_operator, billing, health_and_safety, job_approver). Enforcement is a handful of
FastAPI dependencies — `require_admin`, `require_billing_access`, `require_job_approver` —
each hardcoding "admin OR this one role".

DITB needs capability scoped to **(org → site → subsystem)**: cleared to transmit on the
radio at site A, view-only at sites B through E. Nothing in the current model expresses
"this role, but only at this resource". RLS gives us org isolation and nothing finer.

This is the single largest piece of new design in the platform layer. It is an extension,
not an inheritance. Options range from adding a site-scoped grant table alongside the
existing org roles (least disruptive, keeps DroneOps' role checks working unchanged) to a
full capability model. Worth deciding deliberately rather than drifting into.

**The frontend is a concern.** A 16.7k-line hand-rolled `app.js` with no framework works for
forms, tables and maps over static data. Live video tiles, streaming telemetry, a real-time
map, and radio audio with PTT are a materially different UI problem. I would not extend
`app.js`; I'd expect the DITB console to be a separate frontend. Flagging early because
"shared patterns" could reasonably be read as including the frontend, and I don't think it
should.

### Corrections to `01-architecture-notes.md`

1. **The credential-with-expiry model already exists.** I proposed it for VHF transmit as
   new design. DroneOps has `CertificationType` (org-configurable, admin-managed pick list)
   and `UserCertification` (completed_date + validity_months → *computed* expiry, unique per
   org/user/type, with valid / expiring-soon / expired states). Adding "Radio Operator
   Certificate" is an admin action, not a code change. This drops out of the build entirely.
   - Minor inconsistency worth knowing: `user_certification.py` still carries a hardcoded
     `CERTIFICATION_TYPES` tuple that the newer configurable `CertificationType` model
     supersedes. Not our problem, but don't copy the dead tuple.
2. **The MQTT caveat resolves in favour of a free choice.** I flagged that DroneOps might
   already have a device-transport convention worth deferring to. It has none. MQTT stands
   on its own merits.
3. **Redis and S3-compatible storage are already in the stack.** Both are load-bearing for
   DITB and I'd assumed they were new: Redis for control leases, live telemetry fan-out and
   operator presence; object storage for the video and media archive.
4. **Audit log needs extending, not building.** `audit_logs` is append-only with a JSONB
   `detail` column, but is currently read by platform admins only and scoped to auth and
   admin events. DITB needs org-visible operational audit — every command issued to physical
   hardware. Same table shape, different read path and retention.

---

## Part 2 — Remote-Radio

### What it actually is

A single-station NZ **airband** receiver: an RTL-SDR (Nooelec NESDR Nano 2) with a Python
DSP chain — 240 ksps sampling, offset tuning to dodge the DC spike, decimation to 24 kHz, AM
envelope detection, squelch with hang time and click-free ramping. FastAPI serves a
WebSocket carrying JSON control/status as text frames and int16 PCM as binary frames. About
1,060 lines of Python. The DSP work is careful and the reasoning is documented.

### Five things that make integration harder than it looks

**1. It is receive-only.** `NullTransmitter` refuses every PTT request. `SerialPttTransmitter`
is a sketch, not an implementation — the real thing needs a certified airband transceiver
keyed over serial RTS/DTR, with mic audio routed from the client through the host sound card
into the rig. **"Integrate the VHF radio" is currently "integrate a receiver."** If two-way
is in scope for phase one, that is a hardware procurement and licensing task, and the
software half is not written.

**2. It is an *airband* receiver, specifically.** `MIN_FREQ`/`MAX_FREQ` hard-limit tuning to
108–137 MHz — the aeronautical band, AM. You said "VHF radio" generically. If the security
use case needs marine, land-mobile, or business VHF, that is a different band, different
modulation (FM), different hardware, and a different licence. The DSP chain is AM
envelope-detection and would need an FM demodulator added. **Worth confirming before any
adapter work.**

**3. The radio is a single shared physical resource.** One dongle, one tuned frequency, one
audio stream broadcast to all connected clients. Any client can retune it, and doing so
changes what *everyone* hears. In a multi-org platform this is the crux of the integration:
tuning is a mutating action with blast radius across every listener at that site. It needs a
control lease (Redis, with a timeout), and the UI has to show who currently holds it.
**Superseded 2026-07-28:** no lease was built — a station sits on one frequency almost
all the time, so the ceremony would cost more than the contention. See
`04-production-readiness.md`.

**4. There is no authentication of any kind.** The `/ws` endpoint accepts any connection and
honours any command from it; the server binds `0.0.0.0:8000` by default. That is a
reasonable design for a trusted LAN and it is *not* currently exposed — Starlink CGNAT means
no inbound connections reach it. But it must stay behind the site controller and must never
be published directly. The `/shutdown` endpoint is correctly restricted to loopback.

**5. Bandwidth is a real problem.** 24 kHz × 16-bit mono = **384 kbit/s per listener,
continuously**, uncompressed, whether or not anyone is speaking. On a metered Starlink link
with several operators monitoring, that is the platform's single largest bandwidth consumer
— larger than intermittent video. Two mitigations are already latent in the existing code:
- The server tracks squelch state, so it can send audio *only while the squelch is open*.
  Airband is silent most of the time; this alone is a large win.
- Opus at 16–24 kbit/s is transparent for voice and would cut the rest by roughly 20×.

Neither is built. Both are straightforward. This is the highest-value change to the radio
server for remote deployment, and it is worth doing before anything else.

### Deployment shape

The dongle is physically attached to the site, so the radio server runs **at the edge**, one
instance per site. This independently confirms the site-controller tier in
`01-architecture-notes.md`. Its `state.json` persistence and single-station assumptions are
fine under that model — each site owns its own radio process, and the cloud addresses it as
a device rather than talking to the SDR directly.

---

## Decisions taken

1. **Airband only** — 108–137 MHz AM. The existing DSP chain, band limits and AM envelope
   detection are all fit for purpose unchanged. No FM demodulator needed.
2. **Receive only for now**; a certified transceiver is interfaced later. `NullTransmitter`
   stays. The `Transmitter` interface is left exactly as it is — it is already the right
   seam — and the permission/lease model around transmit is designed in advance so the
   hardware arrival is a wiring job, not a redesign.
3. **`site_grants` alongside org roles**, not replacing them. See `03-realtime-isolation.md`
   §3.
4. **Entirely new frontend.**

The two items that still apply from the radio findings, both unaffected by receive-only
scope: the shared-tuner control lease, and the 384 kbit/s bandwidth problem. Both are
covered in `03-realtime-isolation.md` §8.

## Still open

- Remaining hardware unknowns from `01-architecture-notes.md` §6 — camera, ADS-B receiver,
  weather station, solar controller, floodlight interface, site compute.
- Scale: sites per org, orgs, concurrent operators.
- Whether one user needs simultaneous live streams from two orgs — see
  `03-realtime-isolation.md` §10.
