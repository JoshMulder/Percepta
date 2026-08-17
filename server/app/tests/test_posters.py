"""The wall's stills: who may put one there, and who may look at one.

Both halves of this path carry a secret belonging to somebody who is not the
caller — a customer's camera frame — so the tests worth writing are the
refusals.

THE STATION HALF. The credential is the only thing that says which station a
picture belongs to. A box holding a valid secret must not be able to post as a
different station, and it cannot, because it never gets to say which station it
is — the id is derived. That is the same rule the media socket follows, and the
first test here is what keeps it from being "simplified" into a body field.

THE OPERATOR HALF. A cross-tenant read, so the ODIN ceiling is re-checked per
request rather than trusted from a roster that is up to thirty seconds old.
Deactivating a station is how a tenant stops being watched, and a wall that kept
serving pictures for half a minute afterwards would be a wall that ignores them.

And one performance property is pinned as a test because it is invisible
otherwise: `resolve` must not stamp `last_used_at`. Getting that wrong is an
UPDATE and a COMMIT on one hot row per station per minute, for ever, and nothing
about the feature would look broken.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.database.models import GroundStation, Organization, StationCredential
from backend.database.models.enums import UserRole
from backend.core.crypto import lookup_hash
from backend.services import enrolment

SECRET = "a-station-secret-that-is-long-enough-to-be-real"

#: The smallest thing a JPEG parser would accept as a start and an end. Nothing
#: in this path decodes it — the platform stores bytes and hands them back —
#: which is itself deliberate: a picture the platform re-encoded would be a
#: picture the platform had opinions about.
JPEG = b"\xff\xd8\xff\xe0" + b"percepta" + b"\xff\xd9"


@pytest.fixture()
def watcher_client(db: Session):
    """A TestClient whose session is watch staff in the PLATFORM org.

    Written out here rather than imported from `test_odin_phase5`, following
    the note that suite leaves on its own copy: a fixture shared between suites
    by import is a fixture whose failure blames the wrong file.
    """
    from fastapi import Depends
    from fastapi.testclient import TestClient

    from backend.auth.dependencies import get_identity
    from backend.auth.password import hash_password
    from backend.auth.platform import PLATFORM_ORGANIZATION_ID
    from backend.database.dependencies import get_db
    from backend.database.models import OrganizationMembership, User
    from backend.database.session import set_request_org_context
    from backend.main import app

    if db.get(Organization, PLATFORM_ORGANIZATION_ID) is None:
        db.add(Organization(id=PLATFORM_ORGANIZATION_ID, name="Platform"))
        db.commit()

    user = User(
        id=uuid.uuid4(),
        email="watcher@example.test",
        display_name="Watcher",
        first_name="A",
        last_name="Watcher",
        password_hash=hash_password("not-used-by-these-tests"),
    )
    db.add(user)
    db.flush()
    db.add(OrganizationMembership(
        id=uuid.uuid4(),
        user_id=user.id,
        organization_id=PLATFORM_ORGANIZATION_ID,
        roles=[UserRole.WATCH.value],
    ))
    db.commit()

    identity = Identity(
        user_id=user.id,
        organization_id=PLATFORM_ORGANIZATION_ID,
        session_id=uuid.uuid4(),
        roles=(UserRole.WATCH.value,),
        is_platform_admin=True,
    )

    def _identity(session: Session = Depends(get_db)) -> Identity:
        # Bypass, because this endpoint reads another tenant's station by
        # design — the same context the real watch path runs in.
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


def credential(
    db: Session,
    station: GroundStation,
    org: Organization,
    *,
    secret: str = SECRET,
    revoked: bool = False,
) -> StationCredential:
    record = StationCredential(
        id=uuid.uuid4(),
        organization_id=org.id,
        ground_station_id=station.id,
        secret_hash=lookup_hash(secret),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    db.add(record)
    db.commit()
    return record


# ------------------------------------------------------------- resolving ---


class TestResolve:
    def test_a_valid_secret_finds_its_own_station(
        self, db: Session, station: GroundStation, org: Organization
    ) -> None:
        credential(db, station, org)
        found = enrolment.resolve(db, secret=SECRET)
        assert found is not None and found.id == station.id

    def test_resolving_does_not_stamp_the_credential(
        self, db: Session, station: GroundStation, org: Organization
    ) -> None:
        """The reason this function exists at all rather than reusing
        `authenticate`.

        A poster arrives every sixty seconds from every watched station. If
        resolving one wrote `last_used_at`, that would be an UPDATE and a COMMIT
        on one hot row per station, for ever, to record something no more true
        than the last one — and nothing would look broken.
        """
        record = credential(db, station, org)
        assert record.last_used_at is None

        enrolment.resolve(db, secret=SECRET)
        db.refresh(record)
        assert record.last_used_at is None

    def test_a_revoked_credential_resolves_to_nothing(
        self, db: Session, station: GroundStation, org: Organization
    ) -> None:
        credential(db, station, org, revoked=True)
        assert enrolment.resolve(db, secret=SECRET) is None

    def test_a_deactivated_station_resolves_to_nothing(
        self, db: Session, station: GroundStation, org: Organization
    ) -> None:
        # Deactivating a station is how a tenant stops it being reachable, and
        # that has to hold on every path, not just the ones written first.
        credential(db, station, org)
        station.is_active = False
        db.commit()
        assert enrolment.resolve(db, secret=SECRET) is None

    def test_an_unknown_secret_resolves_to_nothing(self, db: Session) -> None:
        assert enrolment.resolve(db, secret="not-a-secret-anybody-issued") is None


# --------------------------------------------------------------- posting ---


class TestPost:
    def test_no_credential_is_refused(self, client) -> None:
        response = client.post("/media/poster", content=JPEG)
        assert response.status_code == 401

    def test_an_unknown_credential_is_refused(self, client) -> None:
        response = client.post(
            "/media/poster",
            content=JPEG,
            headers={"Authorization": "Bearer nobody-issued-this"},
        )
        assert response.status_code == 401

    def test_a_revoked_credential_is_refused_the_same_way(
        self, client, db: Session, station: GroundStation, org: Organization
    ) -> None:
        # The SAME answer as an unknown secret, deliberately. Telling them apart
        # would let somebody probe which of their old secrets had been revoked.
        credential(db, station, org, revoked=True)
        response = client.post(
            "/media/poster",
            content=JPEG,
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert response.status_code == 401

    def test_an_empty_body_is_refused(
        self, client, db: Session, station: GroundStation, org: Organization
    ) -> None:
        credential(db, station, org)
        response = client.post(
            "/media/poster",
            content=b"",
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert response.status_code == 400

    def test_an_oversized_frame_is_refused(
        self, client, db: Session, station: GroundStation, org: Organization
    ) -> None:
        """Bounded on the platform as well as on the station.

        The station refuses first so a misconfigured camera costs nothing on a
        metered link. This half is for the station that has not been updated, or
        is not ours — without it, whoever holds a credential decides how much
        memory a worker uses.
        """
        from backend.api.posters import MAX_POSTER_BYTES

        credential(db, station, org)
        response = client.post(
            "/media/poster",
            content=b"x" * (MAX_POSTER_BYTES + 1),
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert response.status_code == 413

    def test_the_station_is_derived_and_never_stated(
        self, client, db: Session, station: GroundStation, org: Organization,
        monkeypatch,
    ) -> None:
        """A station cannot post a picture as somebody else.

        It holds a valid credential and names a different station every way the
        request allows — a query parameter and a header. Neither is read: the
        endpoint takes no station id at all, which is the only version of this
        that cannot be got wrong later.

        Asserted on WHERE THE BYTES WENT rather than on a status code, because
        the endpoint answers 204 either way — a spoof that worked would look
        exactly like a spoof that was ignored, from the outside.
        """
        import backend.api.posters as posters
        from backend.realtime.bus import poster_key

        other = GroundStation(
            id=uuid.uuid4(),
            organization_id=org.id,
            name="Somebody Else",
            timezone="Pacific/Auckland",
        )
        db.add(other)
        db.commit()
        credential(db, station, org)

        written: list[str] = []
        monkeypatch.setattr(
            posters, "write_poster_sync",
            lambda key, value=None, **kw: written.append(key) or True,
        )

        response = client.post(
            f"/media/poster?ground_station_id={other.id}",
            content=JPEG,
            headers={
                "Authorization": f"Bearer {SECRET}",
                "X-Ground-Station-Id": str(other.id),
            },
        )

        assert response.status_code == 204
        assert poster_key(station.id) in written
        assert poster_key(other.id) not in written

    def test_the_platform_stamps_the_time_not_the_station(
        self, client, db: Session, station: GroundStation, org: Organization,
        monkeypatch,
    ) -> None:
        """A station with a wrong clock must not decide how fresh its own
        picture looks.

        This fleet boots offline with no RTC, so a station can genuinely believe
        it is 1970 or next year. The stamp drives the tile's `?v=` and its
        staleness, and both want a monotone clock the platform controls — a
        stamp from the future would make every later frame look older, and one
        from 1970 would never change.
        """
        import backend.api.posters as posters
        from backend.realtime.bus import poster_stamp_key

        credential(db, station, org)
        written: dict[str, bytes] = {}
        monkeypatch.setattr(
            posters, "write_poster_sync",
            lambda key, value=None, **kw: written.__setitem__(key, value) or True,
        )

        client.post(
            "/media/poster",
            content=JPEG,
            headers={
                "Authorization": f"Bearer {SECRET}",
                "X-Captured-At": "1970-01-01T00:00:00+00:00",
            },
        )

        stamp = written[poster_stamp_key(station.id)].decode()
        assert not stamp.startswith("1970")
        assert stamp.startswith(str(datetime.now(UTC).year))


# --------------------------------------------------------------- reading ---


class TestGet:
    def test_a_tenant_admin_may_not_read_the_wall_endpoint(self, client) -> None:
        # `client` is an ordinary org admin. This path is cross-tenant by
        # construction, so it is behind the platform watch ceiling and an
        # ordinary admin has no business on it whatever their own org.
        response = client.get(f"/api/odin/stations/{uuid.uuid4()}/poster")
        assert response.status_code in (401, 403, 404)

    def test_a_station_with_no_picture_is_a_404(
        self, watcher_client, station: GroundStation
    ) -> None:
        # 404 rather than an empty 200: a tile must be able to tell "no picture"
        # from "a picture of nothing".
        response = watcher_client.get(f"/api/odin/stations/{station.id}/poster")
        assert response.status_code == 404

    def test_a_deactivated_station_is_a_404_even_with_a_picture(
        self, watcher_client, db: Session, station: GroundStation, monkeypatch
    ) -> None:
        """Deactivating a station is how a tenant stops being watched.

        Checked per request rather than trusted from the wall's roster, which is
        up to thirty seconds old — half a minute of serving a customer's camera
        after they switched it off is half a minute too long.
        """
        import backend.api.posters as posters

        monkeypatch.setattr(posters, "read_latest_sync", lambda keys: [JPEG])
        station.is_active = False
        db.commit()

        response = watcher_client.get(f"/api/odin/stations/{station.id}/poster")
        assert response.status_code == 404

    def test_a_picture_comes_back_as_a_jpeg_no_cache_may_keep(
        self, watcher_client, station: GroundStation, monkeypatch
    ) -> None:
        """`no-cache` would NOT do here, and that is the whole point of the pair.

        It permits a shared cache to store the body and only requires
        revalidation before reuse. `private` keeps a customer's camera frame out
        of intermediaries; `no-store` keeps it off the viewer's disk as well.
        Nothing is lost by refusing to cache — the tile busts its own URL with
        `?v=` from the digest.
        """
        import backend.api.posters as posters

        monkeypatch.setattr(posters, "read_latest_sync", lambda keys: [JPEG])

        response = watcher_client.get(f"/api/odin/stations/{station.id}/poster")
        assert response.status_code == 200
        assert response.content == JPEG
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "private, no-store"

    def test_redis_being_down_is_a_404_and_not_a_500(
        self, watcher_client, station: GroundStation, monkeypatch
    ) -> None:
        # `read_latest_sync` fails soft and returns an EMPTY LIST, not a list of
        # Nones. Indexing it blind would turn one Redis outage into a 500 on
        # every tile on the wall at once.
        import backend.api.posters as posters

        monkeypatch.setattr(posters, "read_latest_sync", lambda keys: [])

        response = watcher_client.get(f"/api/odin/stations/{station.id}/poster")
        assert response.status_code == 404

    def test_the_path_carries_no_jpg_suffix(self) -> None:
        """A cross-tenant edge-cache leak, avoided by not naming the file.

        This deployment sits behind a tunnel where `.jpg` is exactly the suffix
        an intermediary special-cases by extension. `api/tiles.py` already
        reasons about this for basemap tiles, which are a far weaker secret than
        a customer's camera frame.
        """
        from backend.api import posters

        paths = [route.path for route in posters.router.routes]
        assert not any(path.endswith(".jpg") for path in paths)
        assert "/api/odin/stations/{station_id}/poster" in paths
