import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth.cookies import clear_access_cookie, set_access_cookie
from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth.password import verify_password
from backend.auth.platform import PLATFORM_ORGANIZATION_ID
from backend.auth.security import create_access_token
from backend.core.config import settings
from backend.database.dependencies import get_db
from backend.database.models.audit_log import AuditLog
from backend.database.session import PrivilegedSessionLocal
from backend.database.models.enums import UserRole
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
    organization_name: str = ""
    roles: list[str]
    # Session bootstrap carries it rather than a separate endpoint: the console
    # needs it before it renders anything, and this is already the first call.
    demo_mode: bool = False
    # True only while the active org IS the platform org - it is a property of
    # the session, not of the person. See auth/platform.py.
    is_platform_admin: bool = False


def _org_name(db: Session, organization_id) -> str:
    from sqlalchemy import select

    from backend.database.models.organization import Organization

    return (
        db.execute(
            select(Organization.name).where(Organization.id == organization_id)
        ).scalar_one_or_none()
        or ""
    )


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
        organization_name=organization.name,
        roles=roles,
        demo_mode=settings.demo_mode,
        # Derived from the org this session was minted for, matching exactly what
        # resolve_identity will decide on every later request.
        is_platform_admin=organization.id == PLATFORM_ORGANIZATION_ID,
    )


class OrganizationOption(BaseModel):
    id: str
    name: str
    is_platform: bool
    #: False when the caller reaches this org through platform access rather
    #: than through a membership of their own. Worth showing: it is someone
    #: else's tenant and they are working inside it.
    is_member: bool


def _switchable(db: Session, user_id) -> list[OrganizationOption]:
    """Organisations this user may switch into.

    Their own memberships, plus - for a platform administrator - every active
    organisation, because administering tenants you can never look at is not
    administration.
    """
    from sqlalchemy import select

    from backend.database.models.organization import Organization
    from backend.database.models.organization_membership import (
        OrganizationMembership,
    )

    mine = {
        row.organization_id
        for row in db.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user_id
            )
        ).scalars()
    }
    has_platform = PLATFORM_ORGANIZATION_ID in mine

    query = select(Organization).where(Organization.is_active.is_(True))
    if not has_platform:
        if not mine:
            return []
        query = query.where(Organization.id.in_(mine))
    rows = db.execute(query.order_by(Organization.name)).scalars().all()

    return [
        OrganizationOption(
            id=str(o.id),
            name=o.name,
            is_platform=o.id == PLATFORM_ORGANIZATION_ID,
            is_member=o.id in mine,
        )
        for o in rows
    ]


@router.get("/organizations", response_model=list[OrganizationOption])
def organizations(
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[OrganizationOption]:
    return _switchable(db, identity.user_id)


class SwitchRequest(BaseModel):
    organization_id: str


@router.post("/organization", response_model=MeResponse)
def switch_organization(
    body: SwitchRequest,
    request: Request,
    response: Response,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> MeResponse:
    """Move this login to a different organisation.

    A new session is minted and **the current one is revoked**, rather than the
    existing session being repointed. Two reasons, both load-bearing.

    A session carries its organisation into every later decision, including
    row-level security and fan-out group membership; repointing it would mean a
    request already in flight could straddle two tenants. And revocation is
    pushed, so any WebSocket still streaming the previous organisation's data
    is closed immediately - a socket authorised for one tenant must not survive
    a switch to another. The console reconnects under the new session.
    """
    from sqlalchemy import select

    from backend.database.models.organization import Organization

    try:
        target_id = uuid.UUID(body.organization_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid organisation id") from exc

    allowed = {uuid.UUID(o.id) for o in _switchable(db, identity.user_id)}
    if target_id not in allowed:
        # Same shape as every other refusal: no distinction between "no such
        # org" and "not yours".
        raise HTTPException(status_code=404, detail="Organisation not available")

    user = UserRepository(db).get_by_id(identity.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")

    organization = db.execute(
        select(Organization).where(Organization.id == target_id)
    ).scalar_one()

    if target_id == identity.organization_id:
        # Already there. Not an error - a console that re-selects the current
        # organisation should not lose its session over it.
        return MeResponse(
            user_id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            organization_id=str(target_id),
            organization_name=organization.name,
            roles=list(identity.roles),
            demo_mode=settings.demo_mode,
            is_platform_admin=identity.is_platform_admin,
        )

    expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    session = AuthSessionRepository(db).create(
        user_id=user.id, organization_id=target_id, expires_at=expires_at
    )
    previous_session_id = identity.session_id
    AuthSessionRepository(db).revoke(session_id=previous_session_id)
    db.commit()

    # Closes any socket still open on the old session, which is streaming the
    # previous organisation's data.
    revoke_session(previous_session_id)

    token = create_access_token(
        user_id=user.id, organization_id=target_id, session_id=session.id
    )
    set_access_cookie(
        response,
        token,
        max_age=settings.access_token_expire_minutes * 60,
        secure=settings.cookie_secure,
    )
    _audit("organization_switched", request=request, actor_email=user.email,
           actor_user_id=user.id, organization_id=target_id,
           detail={"from": str(identity.organization_id), "to": str(target_id)})

    roles = OrganizationMembershipRepository(db).roles(
        user_id=user.id, organization_id=target_id
    )
    return MeResponse(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        organization_id=str(target_id),
        organization_name=organization.name,
        # A platform admin inside someone else's organisation holds no
        # membership there and presents as its admin - see resolve_identity.
        roles=roles or [UserRole.ADMIN.value],
        demo_mode=settings.demo_mode,
        is_platform_admin=target_id == PLATFORM_ORGANIZATION_ID,
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
        organization_name=_org_name(db, identity.organization_id),
        roles=list(identity.roles),
        demo_mode=settings.demo_mode,
        is_platform_admin=identity.is_platform_admin,
    )
