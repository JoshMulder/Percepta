"""Prove that tenant isolation actually holds at the database.

/api/health reports `rls_enabled`, but that only says APP_DB_PASSWORD is set -
that the API tier connects as the least-privilege role. It says nothing about
whether the policies exist, whether they are FORCEd, or whether they bite. Those
are the things that actually keep one org's ground stations away from another,
and they deserve a test rather than an assumption.

Run it any time, against any deployment:

    docker compose exec app python -m backend.scripts.verify_rls

Creates two throwaway orgs, asserts four properties, and removes them again. It
writes only rows it owns and cleans up in a finally block.

The four properties:

  1. FAIL CLOSED   no org context set  -> zero rows, not all rows
  2. ISOLATION     org A context       -> A's rows only, never B's
  3. WRITE GUARD   org A context       -> cannot insert a row belonging to B
  4. BYPASS        platform god-mode   -> sees across orgs, as designed

Setup runs on the privileged connection. In the official Postgres image the
POSTGRES_USER role is the bootstrap superuser, and a superuser bypasses RLS even
with FORCE enabled - which is exactly why the checks below must run on the app
engine instead, and why doing this test on the privileged connection would prove
nothing at all.
"""

import logging
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from backend.core.config import settings
from backend.database.session import (
    PrivilegedSessionLocal,
    SessionLocal,
    set_request_org_context,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("verify_rls")

ORG_A = uuid.uuid4()
ORG_B = uuid.uuid4()
STATION_A = uuid.uuid4()
STATION_B = uuid.uuid4()
EVENT_A = uuid.uuid4()
EVENT_B = uuid.uuid4()

_failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    if passed:
        log.info("  PASS  %s", label)
    else:
        _failures.append(label)
        log.error("  FAIL  %s %s", label, f"({detail})" if detail else "")


def seed() -> None:
    now = datetime.now(UTC)
    with PrivilegedSessionLocal() as db:
        for org_id, name in ((ORG_A, "rls-check-A"), (ORG_B, "rls-check-B")):
            db.execute(
                text(
                    "INSERT INTO organizations (id, created_at, updated_at, name, "
                    "mfa_required, is_active) VALUES (:id, :t, :t, :name, false, true)"
                ),
                {"id": org_id, "t": now, "name": f"{name}-{org_id.hex[:8]}"},
            )
        for st_id, org_id, name in (
            (STATION_A, ORG_A, "station-A"),
            (STATION_B, ORG_B, "station-B"),
        ):
            db.execute(
                text(
                    "INSERT INTO ground_stations (id, created_at, updated_at, "
                    "organization_id, name, timezone, is_active) "
                    "VALUES (:id, :t, :t, :org, :name, 'UTC', true)"
                ),
                {"id": st_id, "t": now, "org": org_id, "name": name},
            )
        # One event per org too, to prove station_events isolation (0015). These
        # need no separate cleanup: ground_stations delete CASCADEs to them.
        for ev_id, org_id, st_id in (
            (EVENT_A, ORG_A, STATION_A),
            (EVENT_B, ORG_B, STATION_B),
        ):
            db.execute(
                text(
                    "INSERT INTO station_events (id, created_at, updated_at, "
                    "organization_id, ground_station_id, event_id, seq, at, "
                    "received_at, type, severity) VALUES (:id, :t, :t, :org, :st, "
                    ":evid, 1, :t, :t, 'test.proximity', 'info')"
                ),
                {"id": ev_id, "t": now, "org": org_id, "st": st_id,
                 "evid": f"ev-{ev_id.hex[:8]}"},
            )
        db.commit()


def cleanup() -> None:
    with PrivilegedSessionLocal() as db:
        db.execute(
            text("DELETE FROM ground_stations WHERE organization_id IN (:a, :b)"),
            {"a": ORG_A, "b": ORG_B},
        )
        db.execute(
            text("DELETE FROM organizations WHERE id IN (:a, :b)"),
            {"a": ORG_A, "b": ORG_B},
        )
        db.commit()


def _visible_station_ids(db) -> set[uuid.UUID]:
    rows = db.execute(
        text("SELECT id FROM ground_stations WHERE id IN (:a, :b)"),
        {"a": STATION_A, "b": STATION_B},
    ).scalars()
    return set(rows)


def _visible_event_ids(db) -> set[uuid.UUID]:
    rows = db.execute(
        text("SELECT id FROM station_events WHERE id IN (:a, :b)"),
        {"a": EVENT_A, "b": EVENT_B},
    ).scalars()
    return set(rows)


def main() -> int:
    if not settings.rls_enabled:
        log.error(
            "APP_DB_PASSWORD is unset, so the app tier connects as the schema "
            "owner and RLS is bypassed by design. There is nothing to verify. "
            "Set APP_DB_PASSWORD and restart."
        )
        return 2

    log.info("Verifying row-level security (app role: %s)", settings.app_db_user)
    seed()

    try:
        # 1. Fail closed --------------------------------------------------
        log.info("\n1. No org context (must return nothing, not everything)")
        with SessionLocal() as db:
            visible = _visible_station_ids(db)
            check(
                "unscoped query returns zero rows",
                visible == set(),
                f"saw {len(visible)} rows",
            )

        # 2. Isolation ----------------------------------------------------
        log.info("\n2. Org A context (must see A only)")
        with SessionLocal() as db:
            set_request_org_context(db, organization_id=ORG_A, bypass=False)
            visible = _visible_station_ids(db)
            check("sees its own station", STATION_A in visible)
            check(
                "cannot see the other org's station",
                STATION_B not in visible,
                "CROSS-TENANT LEAK",
            )

        # 3. Write guard --------------------------------------------------
        log.info("\n3. Org A context (must not be able to write into org B)")
        with SessionLocal() as db:
            set_request_org_context(db, organization_id=ORG_A, bypass=False)
            rejected = False
            try:
                db.execute(
                    text(
                        "INSERT INTO ground_stations (id, created_at, updated_at, "
                        "organization_id, name, timezone, is_active) "
                        "VALUES (:id, now(), now(), :org, 'smuggled', 'UTC', true)"
                    ),
                    {"id": uuid.uuid4(), "org": ORG_B},
                )
                db.commit()
            except Exception:
                rejected = True
                db.rollback()
            check("insert into another org is rejected", rejected, "WRITE LEAK")

        # 4. Platform bypass ----------------------------------------------
        log.info("\n4. Platform god-mode (must see across orgs, by design)")
        with SessionLocal() as db:
            set_request_org_context(db, organization_id=ORG_A, bypass=True)
            visible = _visible_station_ids(db)
            check("bypass sees both orgs", {STATION_A, STATION_B} <= visible)

        # 5. station_events -----------------------------------------------
        # The table 0011 created without a policy and 0015 closed. It holds
        # airband transcripts and proximity events, so a cross-org leak here is
        # one tenant reading another's captures — worth its own checks.
        log.info("\n5. station_events isolation (the table 0015 closed)")
        with SessionLocal() as db:
            check(
                "unscoped events query returns zero rows",
                _visible_event_ids(db) == set(),
                f"saw {len(_visible_event_ids(db))} rows",
            )
        with SessionLocal() as db:
            set_request_org_context(db, organization_id=ORG_A, bypass=False)
            events = _visible_event_ids(db)
            check("sees its own events", EVENT_A in events)
            check(
                "cannot see the other org's events",
                EVENT_B not in events,
                "CROSS-TENANT EVENT LEAK",
            )
        with SessionLocal() as db:
            set_request_org_context(db, organization_id=ORG_A, bypass=False)
            rejected = False
            try:
                db.execute(
                    text(
                        "INSERT INTO station_events (id, created_at, updated_at, "
                        "organization_id, ground_station_id, event_id, seq, at, "
                        "received_at, type, severity) VALUES (:id, now(), now(), "
                        ":org, :st, 'smuggled', 2, now(), now(), 'x', 'info')"
                    ),
                    {"id": uuid.uuid4(), "org": ORG_B, "st": STATION_B},
                )
                db.commit()
            except Exception:
                rejected = True
                db.rollback()
            check("event insert into another org is rejected", rejected, "WRITE LEAK")
    finally:
        cleanup()

    log.info("")
    if _failures:
        log.error("FAILED: %d check(s) did not pass: %s", len(_failures), _failures)
        return 1
    log.info("All checks passed - tenant isolation is enforced by the database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
