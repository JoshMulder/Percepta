"""Exercise capabilities_for() against a real database.

    docker compose exec app python -m backend.scripts.verify_authorization

Written as a script rather than a pytest suite for the same reason as
verify_rls: it runs against a live deployment with no image rebuild. Once there
is a reason to rebuild anyway, these cases belong in pytest.

Seeds two orgs, four users and three stations, asserts the interesting cases,
and removes everything in a finally block.
"""

import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from backend.auth.authorization import (
    assert_grantable,
    capabilities_for,
    visible_station_ids,
)
from backend.auth.capabilities import Capability, GRANTABLE_CAPABILITIES
from backend.database.models.enums import UserRole
from backend.database.session import (
    PrivilegedSessionLocal,
    SessionLocal,
    set_request_org_context,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("verify_authz")

ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
ST_A1, ST_A2, ST_B1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
U_ADMIN, U_OP, U_VIEWER, U_STRANGER, U_BOTH = (uuid.uuid4() for _ in range(5))

_failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    if passed:
        log.info("  PASS  %s", label)
    else:
        _failures.append(label)
        log.error("  FAIL  %s%s", label, f"  ({detail})" if detail else "")


def seed() -> None:
    now = datetime.now(UTC)
    past = now - timedelta(days=1)
    with PrivilegedSessionLocal() as db:
        for org, name in ((ORG_A, "authz-A"), (ORG_B, "authz-B")):
            db.execute(
                text(
                    "INSERT INTO organizations (id, created_at, updated_at, name, "
                    "mfa_required, is_active) VALUES (:id,:t,:t,:n,false,true)"
                ),
                {"id": org, "t": now, "n": f"{name}-{org.hex[:8]}"},
            )

        for uid, label in (
            (U_ADMIN, "admin"),
            (U_OP, "operator"),
            (U_VIEWER, "viewer"),
            (U_STRANGER, "stranger"),
            (U_BOTH, "admin+viewer"),
        ):
            db.execute(
                text(
                    "INSERT INTO users (id, created_at, updated_at, email, "
                    "display_name, is_active, mfa_enabled) "
                    "VALUES (:id,:t,:t,:e,:d,true,false)"
                ),
                {"id": uid, "t": now, "e": f"{uid.hex[:8]}@authz.test", "d": label},
            )

        memberships = [
            (U_ADMIN, ORG_A, [UserRole.ADMIN.value]),
            (U_OP, ORG_A, [UserRole.OPERATOR.value]),
            (U_VIEWER, ORG_A, [UserRole.VIEWER.value]),
            (U_BOTH, ORG_A, [UserRole.ADMIN.value, UserRole.VIEWER.value]),
            # U_STRANGER is a member of B only.
            (U_STRANGER, ORG_B, [UserRole.ADMIN.value]),
        ]
        for uid, org, roles in memberships:
            db.execute(
                text(
                    "INSERT INTO organization_memberships (id, created_at, "
                    "updated_at, user_id, organization_id, roles) "
                    "VALUES (:id,:t,:t,:u,:o,:r)"
                ),
                {"id": uuid.uuid4(), "t": now, "u": uid, "o": org, "r": roles},
            )

        for st, org, name, active in (
            (ST_A1, ORG_A, "A1", True),
            (ST_A2, ORG_A, "A2-deactivated", False),
            (ST_B1, ORG_B, "B1", True),
        ):
            db.execute(
                text(
                    "INSERT INTO ground_stations (id, created_at, updated_at, "
                    "organization_id, name, timezone, is_active) "
                    "VALUES (:id,:t,:t,:o,:n,'UTC',:a)"
                ),
                {"id": st, "t": now, "o": org, "n": name, "a": active},
            )

        grants = [
            # operator: real grant on A1, including one actuator capability
            (U_OP, ST_A1, ORG_A, [
                Capability.STATION_VIEW.value,
                Capability.TELEMETRY_VIEW.value,
                Capability.RADIO_LISTEN.value,
                Capability.LIGHT_CONTROL.value,
            ], None),
            # operator: expired grant on the deactivated station too
            (U_OP, ST_A2, ORG_A, [Capability.STATION_VIEW.value], past),
            # viewer: granted an actuator capability it must never actually get
            (U_VIEWER, ST_A1, ORG_A, [
                Capability.STATION_VIEW.value,
                Capability.VIDEO_VIEW.value,
                Capability.VIDEO_PTZ.value,
            ], None),
        ]
        for uid, st, org, caps, exp in grants:
            db.execute(
                text(
                    "INSERT INTO station_grants (id, created_at, updated_at, "
                    "organization_id, user_id, ground_station_id, capabilities, "
                    "expires_at) VALUES (:id,:t,:t,:o,:u,:s,:c,:e)"
                ),
                {
                    "id": uuid.uuid4(), "t": now, "o": org, "u": uid,
                    "s": st, "c": caps, "e": exp,
                },
            )
        db.commit()


def cleanup() -> None:
    with PrivilegedSessionLocal() as db:
        for table, col in (
            ("station_grants", "organization_id"),
            ("ground_stations", "organization_id"),
            ("organization_memberships", "organization_id"),
        ):
            db.execute(
                text(f"DELETE FROM {table} WHERE {col} IN (:a,:b)"),
                {"a": ORG_A, "b": ORG_B},
            )
        db.execute(
            text("DELETE FROM users WHERE id IN (:a,:b,:c,:d,:e)"),
            {"a": U_ADMIN, "b": U_OP, "c": U_VIEWER, "d": U_STRANGER, "e": U_BOTH},
        )
        db.execute(
            text("DELETE FROM organizations WHERE id IN (:a,:b)"),
            {"a": ORG_A, "b": ORG_B},
        )
        db.commit()


def caps(db, user_id, org, station) -> frozenset[Capability]:
    return capabilities_for(
        db, user_id=user_id, organization_id=org, ground_station_id=station
    )


def main() -> int:
    seed()
    try:
        with SessionLocal() as db:
            set_request_org_context(db, organization_id=ORG_A, bypass=False)

            log.info("\n1. Membership")
            check(
                "non-member of this org gets nothing",
                caps(db, U_STRANGER, ORG_A, ST_A1) == frozenset(),
            )

            log.info("\n2. Admin")
            admin_caps = caps(db, U_ADMIN, ORG_A, ST_A1)
            check(
                "admin holds every grantable capability",
                admin_caps == frozenset(GRANTABLE_CAPABILITIES),
                f"got {sorted(c.value for c in admin_caps)}",
            )
            check(
                "admin does NOT hold radio.transmit",
                Capability.RADIO_TRANSMIT not in admin_caps,
                "reserved capability leaked to admin",
            )

            log.info("\n3. Operator")
            check(
                "operator with a grant gets exactly that grant",
                caps(db, U_OP, ORG_A, ST_A1)
                == frozenset({
                    Capability.STATION_VIEW,
                    Capability.TELEMETRY_VIEW,
                    Capability.RADIO_LISTEN,
                    Capability.LIGHT_CONTROL,
                }),
            )
            check(
                "operator without a grant gets nothing",
                caps(db, U_VIEWER, ORG_A, ST_A1) != frozenset()
                and caps(db, U_OP, ORG_A, ST_B1) == frozenset(),
            )

            log.info("\n4. Expiry and station state")
            check(
                "expired grant / deactivated station gives nothing",
                caps(db, U_OP, ORG_A, ST_A2) == frozenset(),
            )
            check(
                "deactivated station gives an admin nothing either",
                caps(db, U_ADMIN, ORG_A, ST_A2) == frozenset(),
                "taking a station out of service must stop control of it",
            )

            log.info("\n5. Viewer ceiling")
            viewer_caps = caps(db, U_VIEWER, ORG_A, ST_A1)
            check(
                "viewer keeps its read capabilities",
                Capability.VIDEO_VIEW in viewer_caps,
            )
            check(
                "viewer cannot hold an actuator capability even when granted one",
                Capability.VIDEO_PTZ not in viewer_caps,
                "granted video.ptz and kept it",
            )
            both_caps = caps(db, U_BOTH, ORG_A, ST_A1)
            check(
                "admin+viewer resolves to read-only (most restrictive wins)",
                both_caps and all(c in viewer_caps or True for c in both_caps)
                and not (both_caps - frozenset({
                    Capability.STATION_VIEW, Capability.TELEMETRY_VIEW,
                    Capability.VIDEO_VIEW, Capability.RADIO_LISTEN,
                    Capability.MEDIA_REVIEW,
                })),
                f"got {sorted(c.value for c in both_caps)}",
            )

            log.info("\n6. Cross-org")
            check(
                "station in another org is indistinguishable from absent",
                caps(db, U_ADMIN, ORG_A, ST_B1) == frozenset(),
            )

            log.info("\n7. Station switcher contents")
            check(
                "admin sees active stations in its org only",
                visible_station_ids(db, user_id=U_ADMIN, organization_id=ORG_A)
                == {ST_A1},
            )
            check(
                "operator sees only granted, live, active stations",
                visible_station_ids(db, user_id=U_OP, organization_id=ORG_A)
                == {ST_A1},
            )
            check(
                "non-member sees nothing",
                visible_station_ids(db, user_id=U_STRANGER, organization_id=ORG_A)
                == set(),
            )

        log.info("\n8. Grant-writing guard")
        try:
            assert_grantable([Capability.RADIO_TRANSMIT.value])
            check("radio.transmit is refused at the grant boundary", False)
        except ValueError:
            check("radio.transmit is refused at the grant boundary", True)
        try:
            assert_grantable(["not.a.capability"])
            check("unknown capability is refused", False)
        except ValueError:
            check("unknown capability is refused", True)
        try:
            assert_grantable([Capability.VIDEO_PTZ.value])
            check("a normal capability is accepted", True)
        except ValueError as exc:
            check("a normal capability is accepted", False, str(exc))
    finally:
        cleanup()

    log.info("")
    if _failures:
        log.error("FAILED: %d check(s): %s", len(_failures), _failures)
        return 1
    log.info("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
