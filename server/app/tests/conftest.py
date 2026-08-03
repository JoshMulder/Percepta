"""Fixtures for the platform's API tests.

**Against a real Postgres, always.** Nothing here would survive SQLite: the
schema uses UUID and JSONB columns, row-level security policies, a trigger that
keeps `devices.organization_id` matching its station, and foreign keys whose
`ON DELETE` behaviour is the subject of at least one test in this suite. A
fake database would answer questions about the fake.

The database is a throwaway. `docker-compose.yaml` gives the `tests` service
its own `postgres-test` with no volume behind it, so a run starts from nothing
and leaves nothing — a suite that shares a database with a dev stack is a suite
that either corrupts it or is afraid to write.

**RLS is not what these exercise.** With no `APP_DB_PASSWORD` set, the app
engine is the schema owner, which bypasses row-level security. That is
deliberate: tenant isolation has its own verifier in
`backend/scripts/verify_rls.py`, which checks the policies directly and is the
right tool for it. Mixing the two here would mean every test carrying setup for
a property it is not testing.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.password import hash_password
from backend.database.dependencies import get_db
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.user import User
from backend.database.session import (
    PrivilegedSessionLocal,
    privileged_engine,
    set_request_org_context,
)

APP_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def schema() -> None:
    """The real migrations, once, against the throwaway database.

    Alembic rather than `Base.metadata.create_all`: the migrations carry things
    the models do not — the RLS policies, the org-matching trigger, and the
    `ON DELETE` clauses this suite asserts on. A schema built from the models
    would pass tests the deployed one fails, which is the worst possible
    outcome for a suite whose whole purpose is to catch that.
    """
    done = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=APP_DIR, capture_output=True, text=True,
    )
    if done.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{done.stdout}\n{done.stderr}")


@pytest.fixture()
def db(schema) -> Session:
    """A privileged session, for arranging and for looking afterwards.

    Privileged on purpose: a test that sets up its own fixtures through the
    same guards it is testing cannot tell a broken guard from a broken setup.
    """
    session = PrivilegedSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def clean(schema) -> None:
    """Empty every table between tests, in one statement.

    TRUNCATE ... CASCADE rather than deleting per table in dependency order:
    the order is exactly the thing under test in places, and a fixture that
    encodes it would break whenever the schema legitimately changed.
    """
    yield
    with privileged_engine.begin() as connection:
        tables = connection.execute(text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
        )).scalars().all()
        if tables:
            names = ", ".join(f'"{t}"' for t in tables)
            connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def org(db: Session) -> Organization:
    organization = Organization(id=uuid.uuid4(), name="Test Organisation")
    db.add(organization)
    db.commit()
    return organization


@pytest.fixture()
def admin(db: Session, org: Organization) -> User:
    """A member with the ADMIN role, which is what `config.write` comes from."""
    user = User(
        id=uuid.uuid4(),
        email="admin@example.test",
        display_name="Test Admin",
        first_name="Test",
        last_name="Admin",
        password_hash=hash_password("not-used-by-these-tests"),
    )
    db.add(user)
    db.flush()
    db.add(OrganizationMembership(
        id=uuid.uuid4(),
        user_id=user.id,
        organization_id=org.id,
        roles=[UserRole.ADMIN.value],
    ))
    db.commit()
    return user


@pytest.fixture()
def client(admin: User, org: Organization) -> TestClient:
    """The real app, with authentication replaced and nothing else.

    Only `get_identity` is overridden. Everything downstream of it — the
    capability lookup, the org context, the endpoints — runs as it does in
    production, so a test that passes here says something about the deployed
    system rather than about the mocks.

    The override stamps the RLS org context itself, because that normally
    happens inside the auth path being replaced. Without it every query in the
    request would run with no org set, and the policies fail closed: the
    endpoints would return empty results and the tests would read as
    mysterious 404s.
    """
    from backend.main import app

    identity = Identity(
        user_id=admin.id,
        organization_id=org.id,
        session_id=uuid.uuid4(),
        roles=(UserRole.ADMIN.value,),
        is_platform_admin=False,
    )

    def _identity(session: Session = Depends(get_db)) -> Identity:
        set_request_org_context(
            session, organization_id=org.id, bypass=False
        )
        return identity

    app.dependency_overrides[get_identity] = _identity
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_identity, None)


@pytest.fixture()
def station(db: Session, org: Organization) -> GroundStation:
    record = GroundStation(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Bench Station",
        timezone="Pacific/Auckland",
    )
    db.add(record)
    db.commit()
    return record
