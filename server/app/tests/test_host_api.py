"""The host-shell ticket endpoint: who may ask for a terminal on a host.

A host shell is host RCE via the platform, so getting the ticket that authorises
the terminal socket is `require_platform_admin`, and the station is resolved in
code (the guard takes no station id, a platform-admin session bypasses RLS).
These check the guard and the resolution; the socket bridging is exercised in
`test_host_relay.py`.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.platform import PLATFORM_ORGANIZATION_ID
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation


def test_an_ordinary_org_admin_cannot_ask_for_a_host_terminal(
    client: TestClient, station: GroundStation
) -> None:
    response = client.post(f"/api/platform/stations/{station.id}/host-shell-ticket")
    assert response.status_code == 403


def _platform_admin(app) -> Identity:
    identity = Identity(
        user_id=uuid.uuid4(),
        organization_id=PLATFORM_ORGANIZATION_ID,
        session_id=uuid.uuid4(),
        roles=(UserRole.ADMIN.value,),
        is_platform_admin=True,
    )
    app.dependency_overrides[get_identity] = lambda: identity
    return identity


def test_a_platform_admin_gets_a_single_use_station_bound_ticket(
    station: GroundStation,
) -> None:
    from backend.main import app

    _platform_admin(app)
    try:
        with TestClient(app) as test_client:
            response = test_client.post(
                f"/api/platform/stations/{station.id}/host-shell-ticket"
            )
    finally:
        app.dependency_overrides.pop(get_identity, None)

    assert response.status_code == 200
    body = response.json()
    assert body["ticket"] and body["url"].endswith(f"ticket={body['ticket']}")
    assert body["expires_in"] == 60


def test_an_unknown_station_is_a_404_for_a_platform_admin() -> None:
    from backend.main import app

    _platform_admin(app)
    try:
        with TestClient(app) as test_client:
            response = test_client.post(
                f"/api/platform/stations/{uuid.uuid4()}/host-shell-ticket"
            )
    finally:
        app.dependency_overrides.pop(get_identity, None)
    assert response.status_code == 404
