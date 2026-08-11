"""Docker registry token-auth endpoint — the pull side of remote update.

See `server/docs/07-remote-update-distribution.md`. The private registry
(`registry:2`, in `docker-compose.yaml`) delegates authentication here via the
Distribution token flow: a client that wants to pull is bounced to
`GET /v2/token`, authenticates, and receives a short-lived JWT the registry then
verifies against the public half of this platform's token key.

The point of routing it through the platform is that a station authenticates
with the **bearer credential it already holds from enrolment** — no
registry-specific secret ever lands on the box — and the platform scopes the
token to `pull` on the one repository. Revoking a station's credential revokes
its pulls, for free, through the machinery that already exists. The release box
gets a separate robot credential that also carries `push`.

The signing key is an RSA keypair the platform holds (`registry_token_key_file`,
with its self-signed cert at the registry's `rootcertbundle`); generate it with:

    openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \\
      -keyout certs/registry-token.key -out certs/registry-token.crt \\
      -subj "/CN=percepta-registry-token"

The key must be readable by the app's runtime uid, which reaches certs through a
shared group (the container runs as uid 10001, added to the cert gid). Give it
the same 640 / cert-group ownership as the other private keys — a 600 key openssl
writes by default is owner-only, and the endpoint then 503s "registry token key
unavailable" for every token. That was the first-run failure on `.49`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
import uuid
from datetime import UTC, datetime
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.database.dependencies import get_db
from backend.services import enrolment

router = APIRouter(tags=["registry"])


def _libtrust_kid(public_key) -> str:
    """The libtrust key ID the registry matches a token's `kid` header against:
    SHA-256 of the DER SubjectPublicKeyInfo, first 240 bits, base32-encoded, in
    twelve colon-separated quads. This is how `registry:2` ties a JWT to a key in
    its `rootcertbundle`, so it must be computed exactly."""
    der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).digest()[:30]
    b32 = base64.b32encode(digest).decode("ascii")
    return ":".join(b32[i:i + 4] for i in range(0, len(b32), 4))


@lru_cache(maxsize=1)
def _signing_key() -> tuple[object, str]:
    """The RSA private key that signs registry tokens, and its libtrust `kid`.
    Loaded once. A missing or unreadable key is a 503 at issue time rather than
    an unsigned token — the registry would reject an unsigned one anyway, but
    failing loudly here says *why*."""
    with open(settings.registry_token_key_file, "rb") as handle:
        private_key = serialization.load_pem_private_key(handle.read(), password=None)
    return private_key, _libtrust_kid(private_key.public_key())


def _basic_auth(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    if header[:6].lower() != "basic ":
        return None
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    user, sep, password = raw.partition(":")
    return (user, password) if sep else None


@router.get("/v2/token")
def issue_token(
    request: Request,
    service: str = "",
    scope: str = "",
    db: Session = Depends(get_db),
) -> dict:
    """Authenticate a pull (station credential) or a push (robot credential) and
    mint the scoped, short-lived JWT the registry accepts."""
    creds = _basic_auth(request)
    if creds is None:
        raise HTTPException(
            status_code=401, detail="registry authentication required",
            headers={"WWW-Authenticate": "Basic"})
    user, password = creds

    # Robot (push) vs station (pull). The robot is one configured credential the
    # release box holds; everyone else is a station proving its enrolment secret,
    # from which — as everywhere — the identity is *derived*, never asserted.
    is_robot = (
        settings.registry_robot_user is not None
        and user == settings.registry_robot_user
    )
    if is_robot:
        if not settings.registry_robot_token or password != settings.registry_robot_token:
            raise HTTPException(status_code=401, detail="bad robot credential")
        subject = user
    else:
        found = enrolment.authenticate(db, secret=password)
        if found is None:
            raise HTTPException(status_code=401, detail="credential not recognised")
        subject = str(found[0].id)

    # A station gets pull; the robot gets whatever it asks. A scope for any other
    # repository grants nothing — the token is still valid for `docker login`,
    # which requests no scope, but carries no access.
    allowed = {"pull", "push"} if is_robot else {"pull"}
    access: list[dict] = []
    parts = scope.split(":")
    if len(parts) == 3 and parts[0] == "repository" and parts[1] == settings.registry_repository:
        actions = sorted(a for a in parts[2].split(",") if a in allowed)
        if actions:
            access = [{"type": "repository", "name": settings.registry_repository,
                       "actions": actions}]

    try:
        private_key, kid = _signing_key()
    except OSError as exc:
        raise HTTPException(status_code=503, detail="registry token key unavailable") from exc

    now = int(time.time())
    ttl = settings.registry_token_ttl_seconds
    claims = {
        "iss": settings.registry_token_issuer,
        "sub": subject,
        "aud": service or settings.registry_token_service,
        "iat": now - 10,   # a little skew allowance either side
        "nbf": now - 10,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "access": access,
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})
    # issued_at MUST be an RFC3339 *string*: the Docker client parses it into a
    # Go time.Time and refuses the whole response ("input is not a JSON string")
    # if it is the bare epoch the JWT's `iat` uses. Found the hard way — the JWT
    # claims are fine, so only a real `docker login` catches it.
    issued_at = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"token": token, "access_token": token, "expires_in": ttl, "issued_at": issued_at}
