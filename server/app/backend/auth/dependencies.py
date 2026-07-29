"""FastAPI dependencies for authenticated HTTP routes.

Thin wrappers over auth/identity.py, which the WebSocket handshake also uses -
so both transports resolve identity through exactly the same code. A socket that
authenticated slightly differently from the REST API is how a revoked session
ends up still streaming video.
"""

import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.auth.authorization import capabilities_for
from backend.auth.capabilities import Capability
from backend.auth.cookies import ACCESS_COOKIE_NAME
from backend.auth.identity import Identity, resolve_identity
from backend.database.dependencies import get_db
from backend.database.models.enums import UserRole
from backend.repositories.organization_membership_repository import (
    OrganizationMembershipRepository,
)


def _extract_token(request: Request) -> str | None:
    """Prefer the HttpOnly session cookie; fall back to a Bearer header so
    non-browser API clients still work."""
    cookie_token = request.cookies.get(ACCESS_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def get_identity(
    request: Request, db: Session = Depends(get_db)
) -> Identity:
    identity = resolve_identity(db, _extract_token(request))
    if identity is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return identity


def require_admin(
    identity: Identity = Depends(get_identity), db: Session = Depends(get_db)
) -> Identity:
    """Route guard for org-wide administration.

    Distinct from `require_capability` on purpose. Capabilities answer "may you
    do this at that station"; this answers "may you change who can do anything
    here at all", which is a different and larger question - an admin can grant
    themselves every capability on every station, so this is the boundary that
    actually matters for the tenancy model.

    403 rather than 404, unlike the station guard. There is nothing to conceal:
    the caller is a member of this org and already knows it exists, so the
    enumeration risk that justifies the 404 elsewhere does not apply, and a
    truthful error is more useful.

    A platform admin working inside a customer's organisation passes this, and
    that is not a hole. `is_guest` is set on exactly one path - a member of the
    platform organisation who has switched into an org they hold no membership
    in - so it already means "platform administrator, acting here". Refusing
    them buys nothing: from the Platform pane they can grant themselves a
    membership in any organisation, or create a user who has one, and then come
    back and pass this check honestly. All the refusal did was make customer
    member roles and station grants unreachable, which is most of what platform
    administration is for.

    Note this does not widen what they can *see*. RLS still binds every query to
    the organisation they switched into; god mode remains a property of the
    active org and is set in one place, `resolve_identity`.
    """
    if identity.is_guest:
        return identity

    roles = set(
        OrganizationMembershipRepository(db).roles(
            user_id=identity.user_id, organization_id=identity.organization_id
        )
    )
    if UserRole.ADMIN not in roles:
        raise HTTPException(
            status_code=403, detail="This requires an organisation administrator"
        )
    return identity


def require_capability(capability: Capability):
    """Route guard for a capability at a station named in the path.

    Routes using this must have a `station_id` path parameter. The refusal is
    always 404, never 403: telling a caller "that exists but you may not touch
    it" leaks the existence of another tenant's hardware, and the WebSocket path
    is careful about the same thing.
    """

    def dependency(
        station_id: uuid.UUID,
        identity: Identity = Depends(get_identity),
        db: Session = Depends(get_db),
    ) -> Identity:
        granted = capabilities_for(
            db,
            user_id=identity.user_id,
            organization_id=identity.organization_id,
            ground_station_id=station_id,
        )
        if capability not in granted:
            raise HTTPException(status_code=404, detail="Station not available")
        return identity

    return dependency
