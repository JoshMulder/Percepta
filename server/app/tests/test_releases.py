"""The station-image release catalog, and updating to the latest one.

What this catalog is for: an operator should never handle a digest. A platform
admin records a signed release once, and a console offers "update to vX" on a
station row — the platform resolves the digest server-side. It is deliberately
*not* the trust anchor: the station's own updater cosign-verifies whatever digest
it is handed against its enrolment-pinned keys, so the worst a wrong entry here
can do is offer an image the box will refuse.

The split these assert: publishing and reading the whole catalog are platform
work; reading "what is the latest tag" is open to any signed-in user, because it
is the same version string a station already reports running — and that view
withholds the digest.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from unittest import mock

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.auth.dependencies import get_identity
from backend.auth.password import hash_password
from backend.auth.platform import (
    PLATFORM_ORGANIZATION_ID,
    PLATFORM_ORGANIZATION_NAME,
)
from backend.database.dependencies import get_db
from backend.database.models.enums import UserRole
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.release import Release
from backend.database.models.user import User
from backend.database.session import set_request_org_context

IMAGE = "registry.percepta.nz/percepta-gsu"
DIGEST = "sha256:" + "b" * 64
OTHER_DIGEST = "sha256:" + "c" * 64


@pytest.fixture()
def platform_client(db: Session) -> TestClient:
    """The app as a platform administrator — the only role that may publish.

    Modelled on the one in test_platform_organizations: the active organisation
    is the platform organisation and the session bypasses RLS, which is what the
    real platform auth path does.
    """
    from backend.main import app

    if db.get(Organization, PLATFORM_ORGANIZATION_ID) is None:
        db.add(Organization(
            id=PLATFORM_ORGANIZATION_ID, name=PLATFORM_ORGANIZATION_NAME
        ))
    admin = User(
        id=uuid.uuid4(),
        email="releases@example.test",
        display_name="Release Manager",
        first_name="Release",
        last_name="Manager",
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


def add_release(db, *, tag: str, digest: str = DIGEST, ago_seconds: int = 0) -> Release:
    """A published release, straight into the table.

    Timestamps are set explicitly rather than left to the default: "latest" is
    defined by created_at, and two rows inserted in the same test are otherwise
    ordered by microseconds nobody controls.
    """
    row = Release(
        id=uuid.uuid4(),
        image=IMAGE,
        digest=digest,
        tag=tag,
        notes=None,
        created_at=datetime.now(UTC) - timedelta(seconds=ago_seconds),
    )
    db.add(row)
    db.commit()
    return row


class TestPublishing:
    def test_a_platform_admin_can_publish(self, platform_client):
        response = platform_client.post(
            "/api/releases",
            json={"image": IMAGE, "digest": DIGEST, "tag": "v0.3.0",
                  "notes": "Camera PTS fix."},
        )
        assert response.status_code == 201, response.text
        out = response.json()
        assert out["tag"] == "v0.3.0"
        assert out["digest"] == DIGEST
        assert out["notes"] == "Camera PTS fix."
        assert out["published_by"] == "Release Manager"

    def test_an_org_admin_may_not_publish(self, client):
        """Publishing decides what an entire fleet is offered; it is not a
        tenant's call."""
        response = client.post(
            "/api/releases",
            json={"image": IMAGE, "digest": DIGEST, "tag": "v0.3.0"},
        )
        assert response.status_code == 403, response.text

    def test_an_org_admin_may_not_read_the_catalog(self, client):
        response = client.get("/api/releases")
        assert response.status_code == 403, response.text

    def test_a_malformed_digest_is_refused(self, platform_client):
        """The digest is the pin the station verifies. A typo caught here is a
        clear error; caught later it is an update that silently never lands."""
        response = platform_client.post(
            "/api/releases",
            json={"image": IMAGE, "digest": "sha256:nope", "tag": "v0.3.0"},
        )
        assert response.status_code == 422, response.text

    def test_the_catalog_lists_newest_first(self, platform_client, db):
        add_release(db, tag="v0.1.0", ago_seconds=200)
        add_release(db, tag="v0.2.0", ago_seconds=100)
        response = platform_client.get("/api/releases")
        assert response.status_code == 200, response.text
        assert [r["tag"] for r in response.json()] == ["v0.2.0", "v0.1.0"]


class TestLatest:
    def test_any_signed_in_user_may_read_it(self, client, db):
        add_release(db, tag="v0.3.0")
        response = client.get("/api/releases/latest")
        assert response.status_code == 200, response.text
        assert response.json()["tag"] == "v0.3.0"

    def test_it_withholds_the_digest(self, client, db):
        """An operator never handles one — the one-click update resolves it
        server-side — so it is not in the operator-facing view."""
        add_release(db, tag="v0.3.0")
        assert "digest" not in client.get("/api/releases/latest").json()

    def test_nothing_published_is_not_an_error(self, client):
        """The console shows no pill; it must not show a broken panel."""
        response = client.get("/api/releases/latest")
        assert response.status_code == 200, response.text
        assert response.json()["tag"] is None

    def test_the_most_recently_published_wins(self, client, db):
        add_release(db, tag="v0.1.0", ago_seconds=200)
        add_release(db, tag="v0.3.0", digest=OTHER_DIGEST, ago_seconds=1)
        add_release(db, tag="v0.2.0", ago_seconds=100)
        assert client.get("/api/releases/latest").json()["tag"] == "v0.3.0"


class TestUpdateToLatest:
    def test_it_dispatches_the_resolved_digest(self, client, db, station):
        """The point of the whole feature: the operator sends no digest, and the
        station is told exactly the one that was published."""
        add_release(db, tag="v0.3.0", digest=OTHER_DIGEST)
        with mock.patch(
            "backend.api.commands.publish_sync", return_value=True
        ) as pub:
            response = client.post(f"/api/stations/{station.id}/update/latest")
        assert response.status_code == 202, response.text
        assert response.json()["tag"] == "v0.3.0"
        _, command = pub.call_args.args
        assert command == {
            "kind": "system.update",
            "image": IMAGE,
            "digest": OTHER_DIGEST,
            "tag": "v0.3.0",
        }

    def test_it_uses_the_newest_release(self, client, db, station):
        add_release(db, tag="v0.1.0", ago_seconds=200)
        add_release(db, tag="v0.3.0", digest=OTHER_DIGEST, ago_seconds=1)
        with mock.patch(
            "backend.api.commands.publish_sync", return_value=True
        ) as pub:
            client.post(f"/api/stations/{station.id}/update/latest")
        _, command = pub.call_args.args
        assert command["digest"] == OTHER_DIGEST

    def test_with_nothing_published_it_refuses_rather_than_guessing(
        self, client, station
    ):
        with mock.patch("backend.api.commands.publish_sync") as pub:
            response = client.post(f"/api/stations/{station.id}/update/latest")
        assert response.status_code == 409, response.text
        pub.assert_not_called()

    def test_an_unreachable_station_is_a_503(self, client, db, station):
        add_release(db, tag="v0.3.0")
        with mock.patch("backend.api.commands.publish_sync", return_value=False):
            response = client.post(f"/api/stations/{station.id}/update/latest")
        assert response.status_code == 503, response.text
