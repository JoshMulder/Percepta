"""Platform admins renaming organisations.

`api/platform.py` is the cross-tenant surface, reachable only while the caller's
active organisation IS the platform organisation. Renaming is the one field of
an organisation it changes; these check that it does, refuses a clash with a
different org, leaves an audit trail, and that an ordinary org session cannot
reach it at all.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.password import hash_password
from backend.auth.platform import PLATFORM_ORGANIZATION_ID, PLATFORM_ORGANIZATION_NAME
from backend.database.dependencies import get_db
from backend.database.models.audit_log import AuditLog
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.user import User
from backend.database.session import set_request_org_context


def _org_ids(client) -> set[str]:
    """The org ids the platform overview currently lists."""
    body = client.get("/api/platform")
    assert body.status_code == 200, body.text
    return {o["id"] for o in body.json()["organizations"]}


def _org_row(client, org_id: str) -> dict | None:
    """One org's row from the overview, or None if not listed."""
    body = client.get("/api/platform")
    assert body.status_code == 200, body.text
    return next((o for o in body.json()["organizations"] if o["id"] == org_id), None)


@pytest.fixture()
def platform_client(db: Session) -> TestClient:
    """The app as a platform administrator.

    Modelled on conftest's `client`, but the active organisation is the platform
    organisation and the session bypasses RLS — which is exactly what the real
    platform auth path does, and what makes `api/platform.py` reachable.
    """
    from backend.main import app

    if db.get(Organization, PLATFORM_ORGANIZATION_ID) is None:
        db.add(Organization(id=PLATFORM_ORGANIZATION_ID, name=PLATFORM_ORGANIZATION_NAME))
    admin = User(
        id=uuid.uuid4(),
        email="platform@example.test",
        display_name="Platform Admin",
        first_name="Platform",
        last_name="Admin",
        password_hash=hash_password("not-used-by-these-tests"),
    )
    db.add(admin)
    db.flush()
    db.add(OrganizationMembership(
        id=uuid.uuid4(),
        user_id=admin.id,
        organization_id=PLATFORM_ORGANIZATION_ID,
        roles=[UserRole.ADMIN.value],
    ))
    db.commit()

    identity = Identity(
        user_id=admin.id,
        organization_id=PLATFORM_ORGANIZATION_ID,
        session_id=uuid.uuid4(),
        roles=(UserRole.ADMIN.value,),
        is_platform_admin=True,
    )

    def _identity(session: Session = Depends(get_db)) -> Identity:
        set_request_org_context(
            session, organization_id=PLATFORM_ORGANIZATION_ID, bypass=True
        )
        return identity

    app.dependency_overrides[get_identity] = _identity
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_identity, None)


def test_rename_changes_the_name(platform_client, org, db):
    response = platform_client.patch(
        f"/api/platform/organizations/{org.id}", json={"name": "Renamed Co"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed Co"
    db.expire_all()
    assert db.get(Organization, org.id).name == "Renamed Co"


def test_the_name_is_trimmed(platform_client, org, db):
    assert platform_client.patch(
        f"/api/platform/organizations/{org.id}", json={"name": "  Spaced Co  "}
    ).status_code == 200
    db.expire_all()
    assert db.get(Organization, org.id).name == "Spaced Co"


def test_rename_leaves_an_audit_trail(platform_client, org, db):
    assert platform_client.patch(
        f"/api/platform/organizations/{org.id}", json={"name": "Audited Co"}
    ).status_code == 200
    db.expire_all()
    rows = db.execute(
        select(AuditLog).where(AuditLog.action == "platform.organization.renamed")
    ).scalars().all()
    assert any(
        r.target_id == str(org.id)
        and (r.detail or {}).get("from") == "Test Organisation"
        and (r.detail or {}).get("to") == "Audited Co"
        for r in rows
    ), "the rename left no trail"


def test_a_clash_with_a_different_org_is_refused(platform_client, org, db):
    db.add(Organization(id=uuid.uuid4(), name="Taken Ltd"))
    db.commit()
    # Case-insensitive, like create: a different case of a taken name still clashes.
    response = platform_client.patch(
        f"/api/platform/organizations/{org.id}", json={"name": "taken ltd"}
    )
    assert response.status_code == 409, response.text
    db.expire_all()
    assert db.get(Organization, org.id).name == "Test Organisation", "renamed anyway"


def test_recasing_its_own_name_is_allowed(platform_client, org, db):
    """The uniqueness check excludes the org itself, so fixing the capitalisation
    of a name is not a clash with the name it already has."""
    response = platform_client.patch(
        f"/api/platform/organizations/{org.id}", json={"name": "test organisation"}
    )
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(Organization, org.id).name == "test organisation"


def test_an_unknown_organisation_is_a_404(platform_client):
    response = platform_client.patch(
        f"/api/platform/organizations/{uuid.uuid4()}", json={"name": "Nobody"}
    )
    assert response.status_code == 404, response.text


def test_a_blank_name_is_refused(platform_client, org, db):
    # An empty string fails the schema's min_length; whitespace-only passes it and
    # is refused in the handler after stripping. Both are 422, neither renames.
    assert platform_client.patch(
        f"/api/platform/organizations/{org.id}", json={"name": ""}
    ).status_code == 422
    assert platform_client.patch(
        f"/api/platform/organizations/{org.id}", json={"name": "   "}
    ).status_code == 422
    db.expire_all()
    assert db.get(Organization, org.id).name == "Test Organisation"


def test_an_ordinary_org_session_cannot_rename(client, org, db):
    """The default client is an org admin, not a platform admin — 403, and the
    name is untouched."""
    response = client.patch(
        f"/api/platform/organizations/{org.id}", json={"name": "Sneaky Co"}
    )
    assert response.status_code == 403, response.text
    db.expire_all()
    assert db.get(Organization, org.id).name == "Test Organisation"


def test_the_platform_org_cannot_be_renamed(platform_client, db):
    from backend.auth.platform import PLATFORM_ORGANIZATION_ID

    response = platform_client.patch(
        f"/api/platform/organizations/{PLATFORM_ORGANIZATION_ID}",
        json={"name": "Something Else"},
    )
    assert response.status_code == 409, response.text
    db.expire_all()
    assert db.get(Organization, PLATFORM_ORGANIZATION_ID).name == PLATFORM_ORGANIZATION_NAME


# --- removal ---------------------------------------------------------------


def test_remove_deactivates_and_marks_the_org(platform_client, org, db):
    before = _org_row(platform_client, str(org.id))
    assert before is not None and before["is_active"] is True
    response = platform_client.delete(f"/api/platform/organizations/{org.id}")
    assert response.status_code == 200, response.text
    db.expire_all()
    # A soft delete: the row is still there, just inactive — and still listed so
    # it can be reactivated, but marked removed.
    assert db.get(Organization, org.id).is_active is False
    after = _org_row(platform_client, str(org.id))
    assert after is not None and after["is_active"] is False


def test_remove_keeps_the_orgs_data(platform_client, org, station, db):
    """The whole point of a soft delete: a non-empty org can be removed without
    taking its stations (and their history) with it."""
    station_id = station.id
    assert platform_client.delete(
        f"/api/platform/organizations/{org.id}"
    ).status_code == 200
    db.expire_all()
    assert db.get(GroundStation, station_id) is not None, "the station was destroyed"


def test_remove_leaves_an_audit_trail(platform_client, org, db):
    assert platform_client.delete(
        f"/api/platform/organizations/{org.id}"
    ).status_code == 200
    db.expire_all()
    rows = db.execute(
        select(AuditLog).where(AuditLog.action == "platform.organization.removed")
    ).scalars().all()
    assert any(
        r.target_id == str(org.id) and (r.detail or {}).get("name") == "Test Organisation"
        for r in rows
    ), "the removal left no trail"


def test_a_removed_orgs_membership_is_hidden(platform_client, org, db):
    """A member of an org that has since been removed should not read as still
    having access to it in the overview."""
    user = User(
        id=uuid.uuid4(),
        email="member@example.test",
        display_name="A Member",
        first_name="A",
        last_name="Member",
        password_hash=hash_password("not-used-by-these-tests"),
    )
    db.add(user)
    db.flush()
    db.add(OrganizationMembership(
        id=uuid.uuid4(), user_id=user.id, organization_id=org.id,
        roles=[UserRole.OPERATOR.value],
    ))
    db.commit()

    assert platform_client.delete(
        f"/api/platform/organizations/{org.id}"
    ).status_code == 200

    body = platform_client.get("/api/platform").json()
    row = next(u for u in body["users"] if u["user_id"] == str(user.id))
    assert all(m["organization_id"] != str(org.id) for m in row["memberships"]), \
        "a removed org still showed as current access"


def test_the_platform_org_cannot_be_removed(platform_client, db):
    from backend.auth.platform import PLATFORM_ORGANIZATION_ID

    response = platform_client.delete(
        f"/api/platform/organizations/{PLATFORM_ORGANIZATION_ID}"
    )
    assert response.status_code == 409, response.text
    db.expire_all()
    assert db.get(Organization, PLATFORM_ORGANIZATION_ID).is_active is True


def test_removing_an_unknown_org_is_a_404(platform_client):
    response = platform_client.delete(f"/api/platform/organizations/{uuid.uuid4()}")
    assert response.status_code == 404, response.text


def test_a_second_removal_is_a_404(platform_client, org):
    assert platform_client.delete(
        f"/api/platform/organizations/{org.id}"
    ).status_code == 200
    # Already inactive — reads as absent, like everywhere else.
    assert platform_client.delete(
        f"/api/platform/organizations/{org.id}"
    ).status_code == 404


def test_an_ordinary_org_session_cannot_remove(client, org, db):
    response = client.delete(f"/api/platform/organizations/{org.id}")
    assert response.status_code == 403, response.text
    db.expire_all()
    assert db.get(Organization, org.id).is_active is True


# --- reactivation ----------------------------------------------------------


def test_reactivate_restores_a_removed_org(platform_client, org, db):
    assert platform_client.delete(
        f"/api/platform/organizations/{org.id}"
    ).status_code == 200
    db.expire_all()
    assert db.get(Organization, org.id).is_active is False

    response = platform_client.post(
        f"/api/platform/organizations/{org.id}/reactivate"
    )
    assert response.status_code == 200, response.text
    assert response.json()["reactivated"] is True
    db.expire_all()
    assert db.get(Organization, org.id).is_active is True


def test_reactivate_leaves_an_audit_trail(platform_client, org, db):
    platform_client.delete(f"/api/platform/organizations/{org.id}")
    platform_client.post(f"/api/platform/organizations/{org.id}/reactivate")
    db.expire_all()
    rows = db.execute(
        select(AuditLog).where(AuditLog.action == "platform.organization.reactivated")
    ).scalars().all()
    assert any(r.target_id == str(org.id) for r in rows), "no reactivation trail"


def test_reactivating_an_active_org_is_a_noop(platform_client, org):
    response = platform_client.post(
        f"/api/platform/organizations/{org.id}/reactivate"
    )
    assert response.status_code == 200, response.text
    assert response.json()["reactivated"] is False


def test_reactivating_an_unknown_org_is_a_404(platform_client):
    assert platform_client.post(
        f"/api/platform/organizations/{uuid.uuid4()}/reactivate"
    ).status_code == 404


def test_an_ordinary_org_session_cannot_reactivate(client, org):
    assert client.post(
        f"/api/platform/organizations/{org.id}/reactivate"
    ).status_code == 403


# --- fleet dashboard -------------------------------------------------------


def test_fleet_lists_stations_across_orgs(platform_client, station):
    body = platform_client.get("/api/platform/fleet")
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["stats"]["stations_total"] >= 1
    row = next(s for s in data["stations"] if s["id"] == str(station.id))
    # The fixture station has never been heard from and belongs to Test Org.
    assert row["status"] == "never"
    assert row["organization_name"] == "Test Organisation"


def test_fleet_omits_a_removed_orgs_stations(platform_client, station, org):
    assert platform_client.delete(
        f"/api/platform/organizations/{org.id}"
    ).status_code == 200
    data = platform_client.get("/api/platform/fleet").json()
    assert str(station.id) not in {s["id"] for s in data["stations"]}, \
        "a removed org's station still showed in the fleet"


def test_fleet_adsb_is_empty_without_snapshots(platform_client, station):
    body = platform_client.get("/api/platform/adsb")
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["aircraft"] == []
    assert data["total_contacts"] == 0


def test_an_ordinary_org_session_cannot_read_the_fleet(client):
    assert client.get("/api/platform/fleet").status_code == 403
    assert client.get("/api/platform/adsb").status_code == 403
