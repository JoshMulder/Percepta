# Production Readiness

Things flagged during the build that are fine for development and are not fine
for a customer deployment. Written as they came up rather than assembled at the
end, so nothing here is speculative — each item is something that is currently
true of this codebase.

## Blocking

**Basemap attribution is not displayed.** Removed from the map on request. Esri's
and OpenStreetMap's terms both require their notice to be shown, so this needs
resolving alongside the tile sourcing below — either by restoring attribution or
by moving to a provider whose terms do not require it.

**Basemap tile licensing.** `services/basemaps.py` points at the same public
endpoints DroneOps uses: Esri World Imagery, OpenStreetMap, OpenTopoMap. Serving
them through our cache-through proxy is far gentler than pointing every browser
at them, but `scripts/cache_map.py` bulk-prefetches, and OSM's tile usage policy
prohibits that outright. Move to a provider whose terms permit offline caching,
or a self-hosted tile server. The code change is one file; the commercial lead
time is the real cost.

**`COOKIE_SECURE` is false by default.** Local HTTP development needs it off.
Anywhere reachable over a network it must be on, or the session cookie can be
sent in clear.

**No TLS.** The app publishes plain HTTP on 8000. Put a reverse proxy in front
that terminates TLS, and bind the app to loopback (`docker-compose.yaml` has the
line commented, next to the current one).

**No database backups.** DroneOps runs a `postgres-backup-local` container on a
daily schedule; Percepta has nothing equivalent. The Postgres volume is the only
copy of every org, grant and audit record.

**`SECRETS_ENCRYPTION_KEY` must be backed up separately from the database.**
Losing it makes the encrypted columns unrecoverable; storing it beside a dump
defeats the control entirely.

**`scripts/seed_dev.py` must never run against a production database.** It
creates accounts with a known shared password.

## Deployment notes

**Audio autoplay on a fixed console.** Browsers suspend an AudioContext until
the user interacts with the page, so on a reloaded console the airband audio
waits for the first click. This is browser policy and cannot be waived from the
page — Chrome relaxes it for origins with a high Media Engagement Index, which a
console used every shift will accrue, but that is not something to rely on. For
a wall-mounted or kiosk console, either allow sound for the origin in the
browser's site settings, or launch it with
`--autoplay-policy=no-user-gesture-required`.

Serving the console over HTTPS also matters here beyond the usual reasons:
`AudioWorklet` is a secure-context-only API, so on plain HTTP the console falls
back to a scheduled player with slightly higher latency and less tolerance of
jitter.

## Known weak spots

**`audit_logs` is outside row-level security.** It is written during
authentication, before an org context exists — a failed login for an unknown
email has no resolved org at all, so an INSERT policy would reject exactly the
rows most worth keeping. Reads are filtered in the repository instead. This is
the one table where a missing filter leaks across tenants, and it deserves extra
scrutiny in review. See migration `0002`.

**The verify scripts are not a test suite.** `verify_rls`,
`verify_authorization`, `verify_realtime` and `verify_bus` run against a live
database, seed and delete real rows, and have no isolation between them. That
was a deliberate trade for being runnable against any deployment with no image
rebuild. They should become pytest with a transactional fixture, and run in CI.

**A failed realtime bus degrades quietly.** If Redis is unreachable at startup
the hub logs an exception and carries on with in-process fan-out only, which is
wrong for any deployment running more than one worker. The log line is the only
signal — it should be an alert.

## Not built yet

These are absences, not defects, but they are load-bearing for the product:

- **PTZ commands.** Radio tuning, squelch and the floodlight now go through
  `api/commands.py` with capability checks and an audit entry each. PTZ still
  renders without doing anything.
- ~~Exclusive lease for contended hardware~~ — **decided against** on
  2026-07-28. A station sits on one frequency almost all the time, so the
  ceremony would cost more than the contention it prevents. Anyone with
  `radio.control` can tune at will; the audit log answers "who moved it".
  Revisit only for `radio.transmit`, where two transmitters on one channel is a
  real problem rather than a UX one.
- **Media pipeline.** No video ingest, relay or stream tickets. The design is in
  `03-realtime-isolation.md` §7.
- **The ingest is single-instance by lease, and untested above one worker.**
  `station_ingest.py` republishes, so two running at once would double every
  frame on the fan-out; a Redis lease elects one leader and the others idle.
  That is correct by construction and the stand-down path has been exercised by
  hand, but the container still runs a single uvicorn worker, so a real
  multi-worker failover has never happened. Exercise it before scaling out, and
  expect a gap of up to one lease period (15s) when a leader dies.
- **Backups are not scheduled and do not leave the host.**
  `scripts/backup.sh` works and the restore has been rehearsed
  (`06-backup-and-restore.md`), but nothing runs it on a timer, nothing alerts
  if it stops producing files, and the dumps sit on the same disk as the
  database they protect. That covers a bad migration; it does not cover the
  machine. The CA key and `SECRETS_ENCRYPTION_KEY` also need somewhere to live
  that is not beside the dump.
- **Certificates expire and nothing renews them.** The private CA is good for
  ten years, the server certificates for 825 days. There is no renewal path and
  no warning: the first sign will be every station failing to connect at once.
  Renewing the server certificates is safe (stations pin the CA, not the leaf);
  renewing the CA is not, and means re-enrolling every station.
- **TLS is server-authenticated only.** Stations prove who they are with a
  bearer credential over a verified channel, not with a client certificate.
  mTLS is the documented upgrade (`contract/enrolment.md` §3) and changes the
  credential type without changing the lifecycle.
- **The console's certificate is from a private CA**, so a browser shows a
  warning until someone trusts it. Fine for operators on known machines, wrong
  for a public deployment - that wants a real domain and a public certificate,
  while stations keep pinning the private CA.
- **The seeded development password is in the repository.**
  `backend/scripts/seed_dev.py` hardcodes `percepta-dev-2026` for
  `admin@`, `operator@` and `viewer@percepta.local`, and that file is pushed to
  GitHub. It is deliberate and the script's own docstring says it has no place
  in a production bring-up - but it means those three accounts must never exist
  on anything real, and there is currently **no other way to create the first
  admin**. A production bring-up needs one: an invite flow, or a one-shot
  bootstrap command that takes a password from the environment and prints
  nothing. Until that exists, standing up a real deployment means either running
  the dev seed (wrong) or inserting a user by hand (undocumented).
- **Enrolment gaps.** No console UI for issuing codes (the API exists at
  `/api/stations/{id}/enrolment`, nothing renders it). No configuration
  delivery - `config_version` is issued at enrolment but `config.set` is not
  implemented. No mTLS or CA; credentials are bearer secrets, which
  `../../contract/enrolment.md` §3 recommends as the starting point but not the
  destination. Credentials expire after 90 days and nothing yet alerts on a
  station that has stopped renewing, which §6 is explicit is how remote sites
  fail.
- **MFA.** The `mfa_required` / `mfa_secret` columns and `pyotp` are present;
  the login flow does not enforce them.
- **Platform admin.** Modelled in the design, not implemented — no cross-org
  read path exists.
- **Real radio hardware.** The `audio` stream carries simulated airband from
  `services/airband_demo.py`; no dongle is integrated. Audio is squelch-gated
  but still uncompressed PCM as base64 - binary frames and Opus remain the
  obvious saving. The obligations the real integration inherits from
  Remote-Radio's handover - out-of-channel noise floor measurement, graceful
  shutdown, station-side settings persistence, a second dongle for ADS-B - are
  in `05-radio-integration.md`.
- **Transmit.** Ungrantable, and must stay so until the stuck-PTT protections in
  `05-radio-integration.md` exist. This is the one item on this page that is a
  safety matter rather than an engineering one.
