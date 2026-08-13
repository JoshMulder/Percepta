"""The Odin watch role: what an operator on shift may and may not reach.

This suite gates the authorisation split. Before it, platform access was a single
undivided key — one membership row of the platform organisation, and the same row
that let somebody read the fleet also opened a root PTY on any customer's station
(api/host.py), the station settings proxy (api/console.py), publication of a
signed image every station installs (api/releases.py), and org and user CRUD.
That was tolerable only while the platform org held nobody but the people who
build the product. Odin is watched by operators on shift.

Both directions matter and both are asserted here. Closing the escalation while
breaking an administrator's ability to descend into a tenant and help would be
its own outage, so the support workflow is pinned as hard as the refusals.

There is one subtlety worth stating, because it is where a plausible-looking fix
goes wrong. The escalation had TWO doors, reached by different routes:
`auth/identity.py` mints a guest identity for a platform member inside a customer
org, and `auth/authorization.py::effective_roles` answers the same question for
the capability lookup. Closing either alone leaves the other open, so the tests
below go at the functions directly rather than through a route, which would only
ever exercise whichever door that route happens to use.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.auth.authorization import capabilities_for, effective_roles
from backend.auth.capabilities import Capability
from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.platform import PLATFORM_ORGANIZATION_ID
from backend.auth.password import hash_password
from backend.database.models.enums import UserRole
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.user import User
from backend.database.dependencies import get_db
from backend.database.session import set_request_org_context


def _platform_org(db: Session) -> Organization:
    existing = db.get(Organization, PLATFORM_ORGANIZATION_ID)
    if existing is not None:
        return existing
    organization = Organization(id=PLATFORM_ORGANIZATION_ID, name="Platform")
    db.add(organization)
    db.commit()
    return organization


def _platform_member(db: Session, *, roles: list[str], email: str) -> User:
    """Somebody with a membership of the platform org carrying `roles`."""
    _platform_org(db)
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name="Platform Person",
        first_name="Platform",
        last_name="Person",
        # The hasher enforces a 12-character minimum; a short one fails here
        # rather than in the assertion, which is a confusing place to find out.
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


def _as(user_id: uuid.UUID, roles: tuple[str, ...]) -> TestClient:
    """A client whose session is active in the PLATFORM org with `roles`.

    Mirrors the conftest client, but the identity says platform rather than
    tenant — which is the state an Odin operator is in for their whole shift.
    """
    from backend.main import app

    identity = Identity(
        user_id=user_id,
        organization_id=PLATFORM_ORGANIZATION_ID,
        session_id=uuid.uuid4(),
        roles=roles,
        is_platform_admin=True,
    )

    def _identity(session: Session = Depends(get_db)) -> Identity:
        set_request_org_context(
            session, organization_id=PLATFORM_ORGANIZATION_ID, bypass=True
        )
        return identity

    app.dependency_overrides[get_identity] = _identity
    test_client = TestClient(app)
    test_client.__enter__()
    return test_client


@pytest.fixture()
def watch(db: Session):
    """An operator on shift: platform access, watch role, nothing else."""
    user = _platform_member(db, roles=[UserRole.WATCH.value], email="watch@example.test")
    from backend.main import app

    client = _as(user.id, (UserRole.WATCH.value,))
    try:
        yield client, user
    finally:
        client.__exit__(None, None, None)
        app.dependency_overrides.pop(get_identity, None)


@pytest.fixture()
def platform_admin(db: Session):
    user = _platform_member(db, roles=[UserRole.ADMIN.value], email="padmin@example.test")
    from backend.main import app

    client = _as(user.id, (UserRole.ADMIN.value,))
    try:
        yield client, user
    finally:
        client.__exit__(None, None, None)
        app.dependency_overrides.pop(get_identity, None)


# --------------------------------------------------------------- the refusals


class TestAWatchOperatorCannotReachAdministration:
    """Everything platform membership used to carry, and no longer does."""

    def test_cannot_open_a_root_shell_on_a_customer_station(self, watch, station):
        # The sharpest one. api/host.py hands out a ticket for an interactive PTY
        # as root on the box, and its only gate was platform membership.
        #
        # 403 exactly, not "anything but 200": the first draft of this test
        # pointed at a URL that does not exist, and a 404 from a missing route
        # reads identically to a refusal. A test that cannot tell those apart
        # would have passed against a completely unguarded endpoint.
        client, _ = watch
        r = client.post(
            f"/api/platform/stations/{station.id}/host-shell-ticket"
        )
        assert r.status_code == 403, r.text

    def test_cannot_reach_the_station_settings_proxy(self, watch, station):
        client, _ = watch
        r = client.get(
            f"/api/platform/stations/{station.id}/console/settings"
        )
        assert r.status_code == 403, r.text

    def test_cannot_publish_a_signed_release(self, watch):
        # A release is installed by every station in the fleet.
        client, _ = watch
        r = client.post(
            "/api/releases",
            json={"tag": "v9.9.9", "image": "registry.example/gsu:v9.9.9", "notes": ""},
        )
        assert r.status_code == 403, r.text

    def test_cannot_create_or_delete_organisations(self, watch):
        client, _ = watch
        assert client.post("/api/platform/organizations", json={"name": "New"}).status_code == 403
        assert client.get("/api/platform").status_code == 403

    def test_cannot_administer_users(self, watch, admin, org):
        client, _ = watch
        r = client.post(
            "/api/platform/users",
            json={"email": "x@example.test", "display_name": "X", "organization_id": str(org.id)},
        )
        assert r.status_code == 403, r.text


class TestAWatchOperatorCanWatch:
    """The read surface the wall is built on. A refusal here is an outage."""

    def test_can_read_the_fleet(self, watch):
        client, _ = watch
        assert client.get("/api/platform/fleet").status_code == 200

    def test_can_read_fleet_aircraft(self, watch):
        client, _ = watch
        assert client.get("/api/platform/adsb").status_code == 200

    def test_can_load_map_tiles(self, watch):
        # Missed in the first pass of the guard split, and the symptom is a fleet
        # map with every marker on a blank background.
        client, _ = watch
        r = client.get("/api/tiles/osm/3/4/5.png")
        assert r.status_code != 403, r.text


# ------------------------------------------- the escalation, at both its doors


class TestDescendingIntoATenantGrantsNothing:
    def test_effective_roles_gives_a_watch_operator_nothing_in_a_customer_org(
        self, db: Session, org: Organization
    ):
        user = _platform_member(db, roles=[UserRole.WATCH.value], email="w2@example.test")
        assert effective_roles(db, user_id=user.id, organization_id=org.id) == set()

    def test_capabilities_for_gives_a_watch_operator_no_actuator(
        self, db: Session, org: Organization, station
    ):
        user = _platform_member(db, roles=[UserRole.WATCH.value], email="w3@example.test")
        granted = capabilities_for(
            db, user_id=user.id, organization_id=org.id, ground_station_id=station.id
        )
        for forbidden in (
            Capability.LIGHT_CONTROL,
            Capability.RADIO_CONTROL,
            Capability.CONFIG_WRITE,
            Capability.STATION_UPDATE,
        ):
            assert forbidden not in granted, f"{forbidden} leaked to a watch operator"

    def test_a_bare_platform_membership_is_not_enough(
        self, db: Session, org: Organization
    ):
        # The literal shape of the old bug: a row with no admin role at all.
        user = _platform_member(db, roles=[], email="bare@example.test")
        assert effective_roles(db, user_id=user.id, organization_id=org.id) == set()


class TestTheAdministratorSupportWorkflowStillWorks:
    """The other direction. Closing the hole must not cost an admin the ability
    to descend into a customer's org and fix their station."""

    def test_an_admin_still_becomes_admin_inside_a_customer_org(
        self, db: Session, org: Organization
    ):
        user = _platform_member(db, roles=[UserRole.ADMIN.value], email="a2@example.test")
        assert effective_roles(db, user_id=user.id, organization_id=org.id) == {
            UserRole.ADMIN.value
        }

    def test_an_admin_keeps_the_administration_surface(self, platform_admin):
        client, _ = platform_admin
        assert client.get("/api/platform").status_code == 200

    def test_an_admin_keeps_the_watch_surface_too(self, platform_admin):
        # Admin is a superset of watch, not a sibling of it.
        client, _ = platform_admin
        assert client.get("/api/platform/fleet").status_code == 200

    def test_a_member_of_neither_org_gets_nothing(self, db: Session, org: Organization):
        outsider = User(
            id=uuid.uuid4(),
            email="outsider@example.test",
            display_name="Outsider",
            first_name="Out",
            last_name="Sider",
            password_hash=hash_password("not-used-by-these-tests"),
        )
        db.add(outsider)
        db.commit()
        assert effective_roles(db, user_id=outsider.id, organization_id=org.id) == set()
