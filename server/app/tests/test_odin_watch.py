"""The cross-tenant listening watch: who can hear whose radio, and who can stop it.

This is the leak surface. Everywhere else in the platform, isolation is enforced
by row-level security in the database — a query that forgets to scope itself
returns nothing, because the policy defaults to an empty org and fails closed.
The watch deliberately steps around that, twice (realtime/hub.py), because
reading another tenant's station is the entire feature. What replaces RLS there
is hand-written code, so it is tested here directly rather than through a route:
a route only ever exercises whichever door it happens to use, and this has
several.

Four properties, and the last two are the ones that would rot quietly:

  1. Only watch staff can guard anything.
  2. A watch joins the TENANT'S OWN audio group and nothing else — in
     particular not :telemetry, which would drag that site's ADS-B down a queue
     shared with the audio.
  3. The tenant's stop levers reach a connection that is not theirs. Deactivating
     a station has to stop a listen by somebody they have never met.
  4. Our OWN stop lever works mid-shift. Taking an operator off the rota has to
     stop the audio they are already hearing, not merely the next session they
     start.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.orm import Session

from backend.auth.capabilities import Capability
from backend.auth.identity import Identity
from backend.auth.odin import ODIN_READ, odin_capabilities_for
from backend.auth.password import hash_password
from backend.auth.platform import PLATFORM_ORGANIZATION_ID
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.user import User
from backend.realtime.connection import Connection
from backend.realtime.hub import WATCH_MAX, AuthorizationError, Hub


class _FakeWS:
    """The hub never touches the socket — only the sender task does, and that is
    endpoint.py's. A stand-in keeps these tests off the network."""

    client_state = None


def _platform_org(db: Session) -> Organization:
    existing = db.get(Organization, PLATFORM_ORGANIZATION_ID)
    if existing is not None:
        return existing
    org = Organization(id=PLATFORM_ORGANIZATION_ID, name="Platform")
    db.add(org)
    db.commit()
    return org


def _member(db: Session, *, organization_id: uuid.UUID, roles: list[str], email: str):
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name="Person",
        first_name="A",
        last_name="Person",
        # 12-character minimum in the hasher; a short one fails here rather than
        # in the assertion, which is a confusing place to find out.
        password_hash=hash_password("not-used-by-these-tests"),
    )
    db.add(user)
    db.flush()
    db.add(
        OrganizationMembership(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=organization_id,
            roles=roles,
        )
    )
    db.commit()
    return user


def _conn(user: User, organization_id: uuid.UUID, roles: tuple[str, ...]) -> Connection:
    return Connection(
        ws=_FakeWS(),
        identity=Identity(
            user_id=user.id,
            organization_id=organization_id,
            session_id=uuid.uuid4(),
            roles=roles,
            is_platform_admin=organization_id == PLATFORM_ORGANIZATION_ID,
        ),
    )


@pytest.fixture()
def operator(db: Session) -> User:
    _platform_org(db)
    return _member(
        db,
        organization_id=PLATFORM_ORGANIZATION_ID,
        roles=[UserRole.WATCH.value],
        email="odin-watch@example.test",
    )


@pytest.fixture()
def hub() -> Hub:
    """A fresh hub, not the module singleton. These tests join and leave groups;
    doing that on the shared instance would leak membership into whatever ran
    next, and a stale group member is exactly the bug this file is about."""
    return Hub()


# ------------------------------------------------------------------ the ceiling


def test_odin_read_grants_no_actuator() -> None:
    """The one thing that must never follow from "I can see your site"."""
    forbidden = {
        Capability.LIGHT_CONTROL,
        Capability.RADIO_CONTROL,
        Capability.CONFIG_WRITE,
        Capability.STATION_UPDATE,
    }
    assert not ODIN_READ.intersection(forbidden)
    assert Capability.RADIO_LISTEN in ODIN_READ


def test_deactivated_station_grants_nothing(db: Session, station: GroundStation) -> None:
    """A tenant's stop lever, tested where it is actually read."""
    assert odin_capabilities_for(db, station_id=station.id) == ODIN_READ
    station.is_active = False
    db.commit()
    assert odin_capabilities_for(db, station_id=station.id) == frozenset()


# --------------------------------------------------------------------- the door


def test_watch_join_reaches_another_tenants_audio_group(
    db: Session, hub: Hub, operator: User, station: GroundStation
) -> None:
    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    group = asyncio.run(hub.watch_join(conn, station.id))

    # The org in the group name is the STATION'S, resolved from the row — not the
    # operator's platform org, which is what a client-supplied org would have
    # produced and what would have made this a private fan-out nobody publishes
    # to.
    assert group == f"org:{station.organization_id}:gsu:{station.id}:audio"
    assert station.id in conn.watch


def test_watch_joins_audio_only(
    db: Session, hub: Hub, operator: User, station: GroundStation
) -> None:
    """Not :telemetry. Joining it to light a squelch lamp would put that site's
    full ADS-B into the same drop-oldest queue as the Opus, and the operator
    would hear a clipped over indistinguishable from a quiet channel."""
    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    asyncio.run(hub.watch_join(conn, station.id))

    joined = {g for g in hub.groups.groups_of(conn) if f":gsu:{station.id}:" in g}
    assert joined == {f"org:{station.organization_id}:gsu:{station.id}:audio"}


def test_a_tenant_user_cannot_watch(
    db: Session, hub: Hub, org: Organization, station: GroundStation
) -> None:
    """The message exists on the shared socket, so the refusal has to be in the
    hub rather than only on the Odin route."""
    outsider = _member(
        db, organization_id=org.id, roles=[UserRole.ADMIN.value], email="t@example.test"
    )
    conn = _conn(outsider, org.id, (UserRole.ADMIN.value,))
    with pytest.raises(AuthorizationError):
        asyncio.run(hub.watch_join(conn, station.id))
    assert not conn.watch


def test_a_platform_member_without_a_watch_role_cannot_watch(
    db: Session, hub: Hub, station: GroundStation
) -> None:
    _platform_org(db)
    billing = _member(
        db,
        organization_id=PLATFORM_ORGANIZATION_ID,
        roles=[UserRole.VIEWER.value],
        email="billing@example.test",
    )
    conn = _conn(billing, PLATFORM_ORGANIZATION_ID, (UserRole.VIEWER.value,))
    with pytest.raises(AuthorizationError):
        asyncio.run(hub.watch_join(conn, station.id))


def test_a_deactivated_station_cannot_be_guarded(
    db: Session, hub: Hub, operator: User, station: GroundStation
) -> None:
    station.is_active = False
    db.commit()
    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    with pytest.raises(AuthorizationError):
        asyncio.run(hub.watch_join(conn, station.id))


# ---------------------------------------------------------------- the guard set


def test_watch_set_replaces_rather_than_accumulates(
    db: Session, hub: Hub, operator: User, org: Organization, station: GroundStation
) -> None:
    """The property reconnection depends on: the client's picture always wins."""
    second = GroundStation(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Second",
        timezone="Pacific/Auckland",
    )
    db.add(second)
    db.commit()

    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    assert asyncio.run(hub.watch_set(conn, [station.id, second.id])) == {
        station.id,
        second.id,
    }

    # The same message with a smaller set is how a channel is released.
    assert asyncio.run(hub.watch_set(conn, [second.id])) == {second.id}
    assert not [
        g for g in hub.groups.groups_of(conn) if f":gsu:{station.id}:" in g
    ], "releasing a channel must leave its group, not merely forget the id"


def test_watch_set_caps_the_channel_count(
    db: Session, hub: Hub, operator: User, org: Organization
) -> None:
    stations = []
    for i in range(WATCH_MAX + 3):
        s = GroundStation(
            id=uuid.uuid4(),
            organization_id=org.id,
            name=f"S{i}",
            timezone="Pacific/Auckland",
        )
        db.add(s)
        stations.append(s)
    db.commit()

    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    guarded = asyncio.run(hub.watch_set(conn, [s.id for s in stations]))
    assert len(guarded) == WATCH_MAX


def test_one_unavailable_station_does_not_fail_the_set(
    db: Session, hub: Hub, operator: User, org: Organization, station: GroundStation
) -> None:
    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    guarded = asyncio.run(hub.watch_set(conn, [station.id, uuid.uuid4()]))
    assert guarded == {station.id}


# ------------------------------------------------- the tenant's stop lever


def test_the_watched_tenant_can_reach_the_watcher(
    db: Session, hub: Hub, operator: User, station: GroundStation
) -> None:
    """Revocation walks connections_for_station. A watch connection is pinned to
    no station and can see none, so without an explicit branch the tenant's own
    lever would miss it entirely and the listen would continue until the sweep."""
    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    asyncio.run(hub.register(conn))
    asyncio.run(hub.watch_join(conn, station.id))

    assert conn in hub.connections_for_station(station.id)
    assert conn in hub.connections_for_organization(station.organization_id)


# --------------------------------------------------- our own stop lever


def test_revalidation_drops_a_deactivated_station(
    db: Session, hub: Hub, operator: User, station: GroundStation
) -> None:
    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    asyncio.run(hub.watch_join(conn, station.id))

    station.is_active = False
    db.commit()

    dropped = hub._revalidate_watch(db, conn)
    assert dropped == frozenset({station.id})
    assert not conn.watch


def test_revalidation_drops_everything_when_the_rota_changes(
    db: Session, hub: Hub, operator: User, station: GroundStation
) -> None:
    """Taking somebody off the watch rota has to stop what they are already
    hearing. A socket outlives a shift."""
    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    asyncio.run(hub.watch_join(conn, station.id))

    membership = (
        db.query(OrganizationMembership)
        .filter_by(user_id=operator.id, organization_id=PLATFORM_ORGANIZATION_ID)
        .one()
    )
    membership.roles = [UserRole.VIEWER.value]
    db.commit()

    assert hub._revalidate_watch(db, conn) == frozenset({station.id})
    assert not conn.watch


def test_revalidation_leaves_a_healthy_watch_alone(
    db: Session, hub: Hub, operator: User, station: GroundStation
) -> None:
    """The failure that would be invisible: a sweep that quietly drops every
    guard reads exactly like a watch that works, until an operator notices the
    strip has gone silent."""
    conn = _conn(operator, PLATFORM_ORGANIZATION_ID, (UserRole.WATCH.value,))
    asyncio.run(hub.watch_join(conn, station.id))

    assert hub._revalidate_watch(db, conn) == frozenset()
    assert conn.watch == {station.id}
