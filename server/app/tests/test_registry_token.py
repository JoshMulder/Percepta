"""The registry token endpoint — the pull side of remote update.

A station authenticates with the bearer credential it already holds and gets a
short-lived, pull-only JWT scoped to the one repository; the release robot gets
push; a JWT it issues verifies against the platform's public key and carries the
`kid` the registry matches against its bundle.
"""

import base64
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.api import registry
from backend.core.config import settings
from backend.core.crypto import lookup_hash
from backend.database.models.station_credential import StationCredential


@pytest.fixture()
def public_key(tmp_path, monkeypatch):
    """A throwaway signing key on disk, wired into settings, its public half
    returned so a test can verify what the endpoint signed."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_file = tmp_path / "registry-token.key"
    key_file.write_bytes(pem)
    monkeypatch.setattr(settings, "registry_token_key_file", str(key_file))
    registry._signing_key.cache_clear()
    yield key.public_key()
    registry._signing_key.cache_clear()


def _basic(user: str, password: str) -> dict:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _give_credential(db, station, org, secret: str) -> None:
    db.add(StationCredential(
        id=uuid.uuid4(),
        organization_id=org.id,
        ground_station_id=station.id,
        secret_hash=lookup_hash(secret),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    ))
    db.commit()


def _decode(token: str, public_key):
    return jwt.decode(
        token, public_key, algorithms=["RS256"],
        audience=settings.registry_token_service,
    )


def test_a_station_gets_a_pull_only_token(client, db, station, org, public_key):
    _give_credential(db, station, org, "pull-me")
    resp = client.get("/v2/token", params={
        "service": settings.registry_token_service,
        "scope": f"repository:{settings.registry_repository}:pull",
    }, headers=_basic(str(station.id), "pull-me"))
    assert resp.status_code == 200, resp.text

    claims = _decode(resp.json()["token"], public_key)
    assert claims["iss"] == settings.registry_token_issuer
    assert claims["sub"] == str(station.id)
    assert claims["access"] == [{
        "type": "repository",
        "name": settings.registry_repository,
        "actions": ["pull"],
    }]
    header = jwt.get_unverified_header(resp.json()["token"])
    assert header["alg"] == "RS256"
    assert header["kid"]  # the registry matches this against its bundle


def test_station_auth_runs_on_a_privileged_session(client, db, station, org, public_key, monkeypatch):
    """Station auth must use a privileged (RLS-bypassing) session, like the broker.
    The station is *derived* from the secret, so there is no org context to scope a
    query to; a request-scoped session under row-level security sees zero credential
    rows and 401s every real station. The unit database bypasses RLS, so a plain
    functional test cannot catch a reversion to `get_db` — this spies on the session
    factory instead, and would fail the moment station auth stopped using it."""
    _give_credential(db, station, org, "priv-me")
    import backend.api.registry as reg
    calls: list[int] = []
    real = reg.PrivilegedSessionLocal
    monkeypatch.setattr(reg, "PrivilegedSessionLocal",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    resp = client.get("/v2/token", params={
        "service": settings.registry_token_service,
        "scope": f"repository:{settings.registry_repository}:pull",
    }, headers=_basic(str(station.id), "priv-me"))
    assert resp.status_code == 200, resp.text
    assert calls, "issue_token must authenticate the station on a PrivilegedSessionLocal"


def test_the_response_envelope_is_what_the_docker_client_parses(client, db, station, org, public_key):
    """`issued_at` must be an RFC3339 *string*, not the JWT's numeric `iat`. The
    Docker client unmarshals it into a Go time.Time and rejects the entire
    response ("input is not a JSON string") if it is a bare number — a real
    `docker login` fails though every JWT claim is correct."""
    _give_credential(db, station, org, "pull-me-env")
    body = client.get("/v2/token", params={
        "service": settings.registry_token_service,
        "scope": f"repository:{settings.registry_repository}:pull",
    }, headers=_basic(str(station.id), "pull-me-env")).json()
    assert isinstance(body["expires_in"], int)
    assert isinstance(body["issued_at"], str)
    # Parses as RFC3339 (the trailing Z is UTC) — exactly what the client needs.
    datetime.strptime(body["issued_at"], "%Y-%m-%dT%H:%M:%SZ")


def test_a_station_asking_for_push_gets_only_pull(client, db, station, org, public_key):
    _give_credential(db, station, org, "pull-me-2")
    resp = client.get("/v2/token", params={
        "service": settings.registry_token_service,
        "scope": f"repository:{settings.registry_repository}:push,pull",
    }, headers=_basic(str(station.id), "pull-me-2"))
    claims = _decode(resp.json()["token"], public_key)
    assert claims["access"][0]["actions"] == ["pull"]


def test_an_unknown_credential_is_refused(client, public_key):
    resp = client.get("/v2/token", params={
        "service": settings.registry_token_service,
        "scope": f"repository:{settings.registry_repository}:pull",
    }, headers=_basic("someone", "not-a-real-secret"))
    assert resp.status_code == 401


def test_no_credential_is_refused(client, public_key):
    resp = client.get("/v2/token", params={"service": settings.registry_token_service})
    assert resp.status_code == 401


def test_the_release_robot_may_push(client, monkeypatch, public_key):
    monkeypatch.setattr(settings, "registry_robot_user", "release-bot")
    monkeypatch.setattr(settings, "registry_robot_token", "robot-secret")
    resp = client.get("/v2/token", params={
        "service": settings.registry_token_service,
        "scope": f"repository:{settings.registry_repository}:push,pull",
    }, headers=_basic("release-bot", "robot-secret"))
    claims = _decode(resp.json()["token"], public_key)
    assert claims["access"][0]["actions"] == ["pull", "push"]


def test_another_repository_grants_nothing(client, db, station, org, public_key):
    _give_credential(db, station, org, "pull-me-3")
    resp = client.get("/v2/token", params={
        "service": settings.registry_token_service,
        "scope": "repository:some-other-image:pull",
    }, headers=_basic(str(station.id), "pull-me-3"))
    claims = _decode(resp.json()["token"], public_key)
    assert claims["access"] == []
