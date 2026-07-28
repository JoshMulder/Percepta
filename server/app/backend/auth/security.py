"""Access token minting and verification.

Token shape matches DroneOps so the two are reasoned about the same way:

    sub  user id
    org  the *active* organisation - switching orgs mints a new token
    jti  the auth_sessions row backing it, which is what makes revocation real

The `org` claim is why a connection can be pinned to an org at connect and never
have to trust anything a client sends afterwards.
"""

import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from backend.core.config import settings

ALGORITHM = "HS256"


def create_access_token(
    *, user_id: uuid.UUID, organization_id: uuid.UUID, session_id: uuid.UUID
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "org": str(organization_id),
        "jti": str(session_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Raises jose.JWTError on anything malformed, expired or wrongly signed."""
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])


def token_identity(token: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None:
    """(user_id, organization_id, session_id), or None if the token is unusable.

    Returns None rather than raising so callers that must fail closed - the
    WebSocket handshake in particular - cannot accidentally treat a decode error
    as anything other than "no identity".
    """
    try:
        payload = decode_access_token(token)
        return (
            uuid.UUID(payload["sub"]),
            uuid.UUID(payload["org"]),
            uuid.UUID(payload["jti"]),
        )
    except (JWTError, KeyError, ValueError):
        return None
