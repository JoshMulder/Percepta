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
