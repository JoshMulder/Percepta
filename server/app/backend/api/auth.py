import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth.cookies import clear_access_cookie, set_access_cookie
from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.password import verify_password
from backend.auth.security import create_access_token
from backend.core.config import settings
from backend.database.dependencies import get_db
from backend.database.models.audit_log import AuditLog
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.revocation import revoke_session
from backend.repositories.auth_session_repository import AuthSessionRepository
from backend.repositories.organization_membership_repository import (
    OrganizationMembershipRepository,
)
from backend.repositories.user_repository import UserRepository

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # Deliberately a plain string, not EmailStr. Validating the format here buys
    # nothing - the address is about to be looked up, and an address that does
    # not exist fails the same way whatever shape it is - while costing two
    # things. It rejects legitimate but unusual addresses outright, and it
    # answers a malformed address with 422 where a merely wrong one gets 401,
    # which is a small account-enumeration signal for free.
    email: str
    password: str


class OrganizationSummary(BaseModel):
    id: str
    name: str


class MeResponse(BaseModel):
    user_id: str
    email: str
    display_name: str
    organization_id: str
    roles: list[str]
    # Session bootstrap carries it rather than a separate endpoint: the console
    # needs it before it renders anything, and this is already the first call.
    demo_mode: bool = False


def _audit(
    action: str,
    *,
    request: Request,
    actor_email: str | None = None,
    actor_user_id=None,
    organization_id=None,
    detail: dict | None = None,
) -> None:
    """Audit writes use the privileged session deliberately.

    audit_logs sits outside RLS because it is written during authentication,
    before an org context exists - a failed login for an unknown email has no
    resolved org at all, and an INSERT policy would reject exactly the rows most
    worth keeping. See migration 0002.
    """
    try:
        with PrivilegedSessionLocal() as db:
            db.add(
                AuditLog(
                    organization_id=organization_id,
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    action=action,
                    ip_address=request.client.host if request.client else None,
                    detail=detail,
                )
            )
            db.commit()
    except Exception:
        # Never let auditing break the request it is describing.
        log.exception("Failed to write audit record for %s.", action)


@router.post("/login", response_model=MeResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> MeResponse:
    user = UserRepository(db).get_by_email(body.email)

    # One failure message and one code path for every reason: unknown email,
    # wrong password, disabled account, no membership. Distinguishing them tells
    # an attacker which addresses are real.
    if user is None or not user.is_active:
        verify_password(body.password, None)
        _audit("login_failed", request=request, actor_email=body.email,
               detail={"reason": "unknown_or_inactive"})
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(body.password, user.password_hash):
        _audit("login_failed", request=request, actor_email=body.email,
               actor_user_id=user.id, detail={"reason": "bad_password"})
        raise HTTPException(status_code=401, detail="Invalid email or password")

    memberships = OrganizationMembershipRepository(db)
    # These reads run before any org context exists. organization_memberships is
    # outside RLS for exactly this reason.
    from sqlalchemy import select

    from backend.database.models.organization import Organization
    from backend.database.models.organization_membership import (
        OrganizationMembership,
    )

    rows = db.execute(
        select(Organization)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(
            OrganizationMembership.user_id == user.id,
            Organization.is_active.is_(True),
        )
        .order_by(Organization.name)
    ).scalars().all()

    if not rows:
        _audit("login_failed", request=request, actor_email=body.email,
               actor_user_id=user.id, detail={"reason": "no_membership"})
        raise HTTPException(status_code=401, detail="Invalid email or password")

    organization = rows[0]
    expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    session = AuthSessionRepository(db).create(
        user_id=user.id, organization_id=organization.id, expires_at=expires_at
    )
    db.commit()

    token = create_access_token(
        user_id=user.id, organization_id=organization.id, session_id=session.id
    )
    set_access_cookie(
        response,
        token,
        max_age=settings.access_token_expire_minutes * 60,
        secure=settings.cookie_secure,
    )
    _audit("login", request=request, actor_email=user.email,
           actor_user_id=user.id, organization_id=organization.id)

    roles = memberships.roles(user_id=user.id, organization_id=organization.id)
    return MeResponse(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        organization_id=str(organization.id),
        roles=roles,
        demo_mode=settings.demo_mode,
    )


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> Response:
    AuthSessionRepository(db).revoke(session_id=identity.session_id)
    db.commit()
    # Revoking the row is not enough on its own: an open WebSocket makes no
    # further HTTP requests, so it would keep streaming until the next
    # revalidation sweep. Push tells every worker now.
    revoke_session(identity.session_id)
    _audit("logout", request=request, actor_user_id=identity.user_id,
           organization_id=identity.organization_id)
    clear_access_cookie(response)
    return Response(status_code=204)


@router.get("/me", response_model=MeResponse)
def me(
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> MeResponse:
    user = UserRepository(db).get_by_id(identity.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return MeResponse(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        organization_id=str(identity.organization_id),
        roles=list(identity.roles),
        demo_mode=settings.demo_mode,
    )
