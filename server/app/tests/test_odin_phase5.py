"""The phase-5 reads and the deliberate reach.

Three properties, and each of them fails SILENTLY if it is wrong — which is why
they are tested here rather than left to the route working once by hand.

  1. A telemetry attach is revoked by both stop levers, and revoking it LEAVES
     THE GROUP. Clearing the field alone stops the connection counting as a
     subscriber while it goes on receiving frames, and nothing in the UI says so.
  2. The event browser's cursor pages a batch of identical timestamps without
     dropping or repeating a row. `received_at` is stamped once per arriving
     batch of up to a hundred, so ties are the normal case on a real site and
     never happen on a bench box sending one event at a time.
  3. The audit read is org-scoped in code. `audit_logs` has NO row-level
     security, so a forgotten predicate returns every tenant's history rather
     than returning nothing — the opposite failure direction from every other
     table in this system.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.auth.password import hash_password
from backend.auth.platform import PLATFORM_ORGANIZATION_ID
from backend.database.models.audit_log import AuditLog
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.station_event import StationEvent
from backend.database.models.user import User
from backend.realtime.connection import Connection
from backend.realtime.hub import AuthorizationError, Hub


class _FakeWS:
    client_state = None


def _platform_org(db: Session) -> Organization:
    existing = db.get(Organization, PLATFORM_ORGANIZATION_ID)
    if existing is not None:
        return existing
    org = Organization(id=PLATFORM_ORGANIZATION_ID, name="Platform")
    db.add(org)
    db.commit()
    return org


def _operator(db: Session, roles: list[str], email: str) -> User:
    _platform_org(db)
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name="Person",
        first_name="A",
        last_name="Person",
        password_hash=hash_password("not-used-by-these-tests"),
    )
    db.add(user)
    db.flush()
    db.add(
        OrganizationMembership(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=PLATFORM_ORGANIZATION_ID,
            roles=roles,
        )
    )
    db.commit()
    return user


def _conn(user: User) -> Connection:
    return Connection(
        ws=_FakeWS(),
        identity=Identity(
            user_id=user.id,
            organization_id=PLATFORM_ORGANIZATION_ID,
            session_id=uuid.uuid4(),
            roles=(UserRole.WATCH.value,),
            is_platform_admin=True,
        ),
    )


@pytest.fixture()
def hub() -> Hub:
    return Hub()


@pytest.fixture()
def watcher(db: Session) -> User:
    return _operator(db, [UserRole.WATCH.value], "p5-watch@example.test")


# ------------------------------------------------ the deliberate attach


def _telemetry_groups(hub: Hub, conn: Connection) -> set[str]:
    return {g for g in hub.groups.groups_of(conn) if g.endswith(":telemetry")}


def test_attach_joins_the_tenants_own_telemetry_group(
    db: Session, hub: Hub, watcher: User, station: GroundStation
) -> None:
    conn = _conn(watcher)
    assert asyncio.run(hub.attach_station(conn, station.id)) == station.id
    assert _telemetry_groups(hub, conn) == {
        f"org:{station.organization_id}:gsu:{station.id}:telemetry"
    }
    assert conn.attached == station.id


def test_attaching_elsewhere_leaves_the_previous_station(
    db: Session, hub: Hub, watcher: User, org: Organization, station: GroundStation
) -> None:
    """One at a time. Accumulating would cost a site's full ADS-B per station an
    operator merely glanced at."""
    second = GroundStation(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Second",
        timezone="Pacific/Auckland",
    )
    db.add(second)
    db.commit()

    conn = _conn(watcher)
    asyncio.run(hub.attach_station(conn, station.id))
    asyncio.run(hub.attach_station(conn, second.id))

    assert _telemetry_groups(hub, conn) == {
        f"org:{org.id}:gsu:{second.id}:telemetry"
    }


def test_attaching_none_detaches(
    db: Session, hub: Hub, watcher: User, station: GroundStation
) -> None:
    conn = _conn(watcher)
    asyncio.run(hub.attach_station(conn, station.id))
    assert asyncio.run(hub.attach_station(conn, None)) is None
    assert _telemetry_groups(hub, conn) == set()
    assert conn.attached is None


def test_a_deactivated_station_cannot_be_attached(
    db: Session, hub: Hub, watcher: User, station: GroundStation
) -> None:
    station.is_active = False
    db.commit()
    conn = _conn(watcher)
    with pytest.raises(AuthorizationError):
        asyncio.run(hub.attach_station(conn, station.id))


def test_a_tenant_user_cannot_attach(
    db: Session, hub: Hub, org: Organization, station: GroundStation
) -> None:
    outsider = User(
        id=uuid.uuid4(),
        email="tenant-p5@example.test",
        display_name="Tenant",
        first_name="T",
        last_name="Enant",
        password_hash=hash_password("not-used-by-these-tests"),
    )
    db.add(outsider)
    db.flush()
    db.add(
        OrganizationMembership(
            id=uuid.uuid4(),
            user_id=outsider.id,
            organization_id=org.id,
            roles=[UserRole.ADMIN.value],
        )
    )
    db.commit()

    conn = Connection(
        ws=_FakeWS(),
        identity=Identity(
            user_id=outsider.id,
            organization_id=org.id,
            session_id=uuid.uuid4(),
            roles=(UserRole.ADMIN.value,),
            is_platform_admin=False,
        ),
    )
    with pytest.raises(AuthorizationError):
        asyncio.run(hub.attach_station(conn, station.id))


def test_the_tenant_can_reach_an_attached_connection(
    db: Session, hub: Hub, watcher: User, station: GroundStation
) -> None:
    """An operator with a drawer open and NO audio guarded has an empty watch
    set. Revocation walks connections_for_station, so without an explicit branch
    the tenant's own stop lever would miss them entirely."""
    conn = _conn(watcher)
    asyncio.run(hub.register(conn))
    asyncio.run(hub.attach_station(conn, station.id))

    assert not conn.watch  # nothing guarded — the case that would be missed
    assert conn in hub.connections_for_station(station.id)
    assert conn in hub.connections_for_organization(station.organization_id)


def test_revalidation_drops_an_attach_and_leaves_the_group(
    db: Session, hub: Hub, watcher: User, station: GroundStation
) -> None:
    """THE test this file exists for.

    Clearing `conn.attached` without leaving the group would stop the connection
    counting as a subscriber while it went on receiving a deactivated tenant's
    telemetry — so this asserts on GROUP MEMBERSHIP, not on the field.
    """
    conn = _conn(watcher)
    asyncio.run(hub.register(conn))
    asyncio.run(hub.attach_station(conn, station.id))

    station.is_active = False
    db.commit()

    assert asyncio.run(hub.revalidate(conn)) is True
    assert conn.attached is None
    assert _telemetry_groups(hub, conn) == set(), (
        "the telemetry group outlived the field that recorded it"
    )


def test_losing_the_watch_role_drops_the_attach(
    db: Session, hub: Hub, watcher: User, station: GroundStation
) -> None:
    conn = _conn(watcher)
    asyncio.run(hub.register(conn))
    asyncio.run(hub.attach_station(conn, station.id))

    membership = (
        db.query(OrganizationMembership)
        .filter_by(user_id=watcher.id, organization_id=PLATFORM_ORGANIZATION_ID)
        .one()
    )
    membership.roles = [UserRole.VIEWER.value]
    db.commit()

    assert asyncio.run(hub.revalidate(conn)) is True
    assert conn.attached is None
    assert _telemetry_groups(hub, conn) == set()


def test_a_healthy_attach_survives_revalidation(
    db: Session, hub: Hub, watcher: User, station: GroundStation
) -> None:
    """A sweep that quietly drops every attach reads exactly like one that
    works, until somebody notices the drawer has stopped updating."""
    conn = _conn(watcher)
    asyncio.run(hub.register(conn))
    asyncio.run(hub.attach_station(conn, station.id))

    assert asyncio.run(hub.revalidate(conn)) is True
    assert conn.attached == station.id
    assert len(_telemetry_groups(hub, conn)) == 1


# ------------------------------------------------------ the event browser


def _seed_events(
    db: Session, station: GroundStation, *, count: int, at: datetime
) -> None:
    """`count` events all sharing ONE received_at, to the microsecond.

    This is what a real station does: services/station_events stamps
    `received_at` once per arriving batch of up to a hundred. A bench box
    sending one event at a time never produces it, which is why a bare
    timestamp cursor survives development and fails in the field.
    """
    for i in range(count):
        db.add(
            StationEvent(
                id=uuid.uuid4(),
                seq=i,
                organization_id=station.organization_id,
                ground_station_id=station.id,
                event_id=f"batched-{i}",
                type="uplink.up",
                severity="info",
                message=str(i),
                at=at,
                received_at=at,
                clock="ok",
            )
        )
    db.commit()


def test_the_cursor_pages_a_tied_batch_without_loss_or_repeats(
    db: Session, station: GroundStation, watch
) -> None:
    client, _ = watch
    at = datetime.now(UTC) - timedelta(minutes=5)
    _seed_events(db, station, count=150, at=at)

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        url = f"/api/odin/events?limit=50&station_id={station.id}"
        if cursor:
            url += f"&cursor={cursor}"
        page = client.get(url).json()
        seen.extend(e["id"] for e in page["events"])
        cursor = page["next_cursor"]
        if not cursor:
            break

    assert len(seen) == 150, "rows were dropped or the paging never terminated"
    assert len(set(seen)) == 150, "rows were repeated across pages"


def test_the_browser_will_not_answer_for_a_deactivated_station(
    db: Session, station: GroundStation, watch
) -> None:
    """The quiet way round the tenant's stop lever, and therefore the easy one
    to leave broken."""
    client, _ = watch
    _seed_events(db, station, count=3, at=datetime.now(UTC) - timedelta(minutes=1))
    assert client.get(f"/api/odin/events?station_id={station.id}").json()["events"]

    station.is_active = False
    db.commit()
    assert client.get(f"/api/odin/events?station_id={station.id}").json()["events"] == []


def test_noise_is_excluded_only_when_asked(
    db: Session, station: GroundStation, watch
) -> None:
    client, _ = watch
    now = datetime.now(UTC) - timedelta(minutes=1)
    for i, kind in enumerate(("adsb.proximity", "uplink.down")):
        db.add(
            StationEvent(
                id=uuid.uuid4(),
                seq=i,
                organization_id=station.organization_id,
                ground_station_id=station.id,
                event_id=f"noise-{i}",
                type=kind,
                severity="warning",
                message=kind,
                at=now,
                received_at=now,
                clock="ok",
            )
        )
    db.commit()

    # By default everything, because "show me everything" must mean it.
    types = {e["type"] for e in client.get("/api/odin/events").json()["events"]}
    assert types == {"adsb.proximity", "uplink.down"}

    opted = {
        e["type"]
        for e in client.get("/api/odin/events?exclude_noise=true").json()["events"]
    }
    assert opted == {"uplink.down"}


# --------------------------------------------------------- the audit read


def _audit_row(db: Session, *, org_id: uuid.UUID | None, action: str) -> None:
    db.add(
        AuditLog(
            id=uuid.uuid4(),
            organization_id=org_id,
            actor_user_id=None,
            action=action,
            target_type=None,
            target_id=None,
        )
    )
    db.commit()


def test_the_audit_read_is_scoped_by_hand(
    db: Session, org: Organization, platform_admin
) -> None:
    """audit_logs has NO row-level security. A forgotten predicate here returns
    everything rather than nothing — the inverse of every other table."""
    client, _ = platform_admin
    other = Organization(id=uuid.uuid4(), name="Somebody Else")
    db.add(other)
    db.commit()

    _audit_row(db, org_id=org.id, action="host_shell_open")
    _audit_row(db, org_id=other.id, action="host_shell_open")

    scoped = client.get(f"/api/odin/audit?organization_id={org.id}").json()
    assert {r["organization_id"] for r in scoped["rows"]} == {str(org.id)}


def test_org_less_rows_are_admitted_only_on_request(
    db: Session, org: Organization, platform_admin
) -> None:
    """A failed login for an address belonging to nobody has no organisation,
    and is exactly what somebody investigating an org wants beside its history —
    but it is not that org's row, so admitting it is a choice."""
    client, _ = platform_admin
    _audit_row(db, org_id=org.id, action="host_shell_open")
    _audit_row(db, org_id=None, action="login_failed")

    without = client.get(f"/api/odin/audit?organization_id={org.id}").json()
    assert {r["action"] for r in without["rows"]} == {"host_shell_open"}

    with_unscoped = client.get(
        f"/api/odin/audit?organization_id={org.id}&include_unscoped=true"
    ).json()
    assert {r["action"] for r in with_unscoped["rows"]} == {
        "host_shell_open",
        "login_failed",
    }


def test_the_reach_group_covers_the_actions_it_exists_for(
    db: Session, org: Organization, platform_admin
) -> None:
    """A prefix filter on 'odin.%' would look right and miss every one of these:
    the actions that matter predate the dotted convention."""
    client, _ = platform_admin
    for action in ("host_shell_open", "console_open", "odin.watch.join"):
        _audit_row(db, org_id=org.id, action=action)
    _audit_row(db, org_id=org.id, action="station.created")

    rows = client.get("/api/odin/audit?group=reach").json()["rows"]
    assert {r["action"] for r in rows} == {
        "host_shell_open",
        "console_open",
        "odin.watch.join",
    }


def test_an_unknown_group_is_refused_by_name(db: Session, platform_admin) -> None:
    client, _ = platform_admin
    response = client.get("/api/odin/audit?group=nonsense")
    assert response.status_code == 400
    # The refusal names the groups that exist, because a caller who guessed
    # wrong cannot discover them from a bare 400.
    assert "reach" in response.json()["detail"]


def test_a_watch_operator_cannot_read_the_audit_trail(db: Session, watch) -> None:
    """The split the whole role separation exists for: a watch position does not
    carry root, and this table is the record OF root access."""
    client, _ = watch
    assert client.get("/api/odin/audit").status_code == 403
