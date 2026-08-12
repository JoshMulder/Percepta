"""The console proxy endpoint: who may reach a station's on-box console.

The whole feature is a reach across tenants — an admin opening a customer's box —
so the browser-facing side is `require_platform_admin` on every request, and the
station is resolved in code (the guard takes no station id, and a platform-admin
session bypasses RLS). These check the two ends of that: an ordinary org admin is
refused, and a platform admin is forwarded through to the station's socket.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.platform import PLATFORM_ORGANIZATION_ID
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.session import set_request_org_context
from backend.realtime.console import ConsoleResponse, relay


def test_an_ordinary_org_admin_is_refused(client: TestClient, station: GroundStation) -> None:
    # The default `client` is an ADMIN of its own org but not a platform admin.
    response = client.get(f"/api/platform/stations/{station.id}/console/")
    assert response.status_code == 403


class _FakeLink:
    """A station socket that answers a canned page, so the endpoint can be
    driven past the guard without a real box dialling back."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    async def request(self, method, path, headers, body):  # noqa: ANN001
        self.seen.append((method, path))
        return ConsoleResponse(
            status=200,
            headers={"Content-Type": "text/html"},
            body=b'<a href="/connection">devices</a>',
        )


def _platform_admin_client(station: GroundStation) -> TestClient:
    from backend.main import app

    identity = Identity(
        user_id=uuid.uuid4(),
        organization_id=PLATFORM_ORGANIZATION_ID,
        session_id=uuid.uuid4(),
        roles=(UserRole.ADMIN.value,),
        is_platform_admin=True,
    )

    app.dependency_overrides[get_identity] = lambda: identity
    return TestClient(app)


def test_a_platform_admin_is_forwarded_and_the_reply_is_adapted(
    station: GroundStation,
) -> None:
    from backend.main import app

    link = _FakeLink()
    # Pre-register the station's socket so the request forwards immediately
    # rather than publishing `console.open` and waiting on a box that is not here.
    relay._links[station.id] = link  # type: ignore[assignment]
    admin = _platform_admin_client(station)
    try:
        with admin as test_client:
            response = test_client.get(
                f"/api/platform/stations/{station.id}/console/devices?slot=radio"
            )
    finally:
        relay._links.pop(station.id, None)
        app.dependency_overrides.pop(get_identity, None)

    assert response.status_code == 200
    # The request reached the station with the leading slash and the query.
    assert link.seen == [("GET", "/devices?slot=radio")]
    # And the reply came back adapted for the frame: the console's root-relative
    # link now carries the console base.
    base = f"/api/platform/stations/{station.id}/console"
    assert f'href="{base}/connection"'.encode() in response.content


def test_an_unknown_station_is_a_404_for_a_platform_admin() -> None:
    from backend.main import app

    missing = uuid.uuid4()
    admin = _platform_admin_client_no_station()
    try:
        with admin as test_client:
            response = test_client.get(
                f"/api/platform/stations/{missing}/console/"
            )
    finally:
        app.dependency_overrides.pop(get_identity, None)
    assert response.status_code == 404


def _platform_admin_client_no_station() -> TestClient:
    from backend.main import app

    identity = Identity(
        user_id=uuid.uuid4(),
        organization_id=PLATFORM_ORGANIZATION_ID,
        session_id=uuid.uuid4(),
        roles=(UserRole.ADMIN.value,),
        is_platform_admin=True,
    )
    app.dependency_overrides[get_identity] = lambda: identity
    return TestClient(app)
