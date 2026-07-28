"""Resolving a token to a live identity, shared by HTTP and WebSocket.

Both transports must answer "who is this, and in which org" identically. The
WebSocket handshake is not a place to reimplement authentication - a socket that
authenticated slightly differently from the REST API is exactly how a revoked
session ends up still streaming video.

So the actual work lives here, and `auth/dependencies.py` (HTTP) and
`realtime/endpoint.py` (WebSocket) are both thin wrappers over it.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.auth.security import token_identity
from backend.database.session import set_request_org_context
from backend.repositories.auth_session_repository import AuthSessionRepository
from backend.repositories.organization_membership_repository import (
    OrganizationMembershipRepository,
)
from backend.repositories.user_repository import UserRepository


@dataclass(frozen=True)
class Identity:
    """A verified, currently-live identity, pinned to one organisation.

    Frozen on purpose. Once a connection is established this must not drift -
    every later decision is made against these three ids, never against
    anything the client says afterwards.
    """

    user_id: uuid.UUID
    organization_id: uuid.UUID
    session_id: uuid.UUID
    roles: tuple[str, ...]
    is_platform_admin: bool


def resolve_identity(db: Session, token: str | None) -> Identity | None:
    """Verify a token and bind the org context to this session's connection.

    Returns None for every failure - bad token, revoked session, disabled user,
    no membership. Callers get one answer shape and cannot accidentally treat a
    partial failure as success.

    The org context set here is what row-level security keys off for the rest of
    the request (or, for a WebSocket, for this particular database session).
    """
    if not token:
        return None

    identity = token_identity(token)
    if identity is None:
        return None
    user_id, organization_id, session_id = identity

    # The token is only valid while its server-side session is live. A revoked
    # or expired session fails here even if the JWT's own exp hasn't passed.
    session = AuthSessionRepository(db).get_active(session_id=session_id)
    if session is None or session.user_id != user_id:
        return None

    # The session was minted for one org. A token claiming a different one than
    # the session it references is not something that should ever happen, so it
    # is treated as hostile rather than reconciled.
    if session.organization_id != organization_id:
        return None

    user = UserRepository(db).get_by_id(user_id)
    if user is None or not user.is_active:
        return None

    membership_repo = OrganizationMembershipRepository(db)
    membership = membership_repo.get(user_id=user_id, organization_id=organization_id)
    if membership is None:
        return None

    # God mode is a property of the *session's active organisation*, not of the
    # person. A platform admin working inside a customer's org sees exactly what
    # that org's own members see, and RLS binds them to it. Only while their
    # active org is the platform org itself do they read across tenants.
    #
    # This is the one place bypass is ever set from a request.
    # Imported here rather than at module scope: auth.platform imports
    # auth.dependencies, which imports this module.
    from backend.auth.platform import PLATFORM_ORGANIZATION_ID

    is_platform_admin = organization_id == PLATFORM_ORGANIZATION_ID
    set_request_org_context(
        db, organization_id=organization_id, bypass=is_platform_admin
    )

    return Identity(
        user_id=user_id,
        organization_id=organization_id,
        session_id=session_id,
        roles=tuple(membership.roles or []),
        is_platform_admin=is_platform_admin,
    )
