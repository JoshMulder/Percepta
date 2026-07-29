import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.cookies import clear_access_cookie, set_access_cookie
from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.auth import mfa
from backend.auth.password import PasswordError, verify_password
from backend.auth.platform import PLATFORM_ORGANIZATION_ID
from backend.auth.security import create_access_token
from backend.core.config import settings
from backend.database.dependencies import get_db
from backend.database.models.audit_log import AuditLog
from backend.database.session import PrivilegedSessionLocal
from backend.database.models.enums import UserRole
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import (
    OrganizationMembership,
)
from backend.database.models.user import User
from backend.realtime.revocation import revoke_session
from backend.repositories.auth_session_repository import AuthSessionRepository
from backend.repositories.organization_membership_repository import (
    OrganizationMembershipRepository,
)
from backend.repositories.user_repository import UserRepository
from backend.services import password_reset

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
    # Absent on the first attempt. The console sends it after the server has
    # asked for it, which is also the only time the user has been told to look.
    mfa_code: str | None = None


class LoginChallenge(BaseModel):
    """Returned instead of a session when a second factor is outstanding.

    200, not 401. The password was correct - that is exactly what distinguishes
    this from a failed login, and the console has to be able to tell, because it
    must ask for a code rather than say the password was wrong.

    The enrolment fields carry a freshly minted secret and are populated only on
    `mfa_enrollment_required`, after the password has already been verified.
    """

    status: str
    secret: str | None = None
    otpauth_uri: str | None = None
    qr_svg: str | None = None


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
    # In someone else's organisation via platform access.
    is_guest: bool = False


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


def _mfa_required_for(db: Session, user: User) -> bool:
    """Whether any organisation this user belongs to insists on a second factor.

    Any, not all. A person in two organisations where one requires MFA is
    protected by it everywhere - the alternative is that whether they need a
    code depends on which organisation their session happens to land in, and a
    second factor that comes and goes is one people learn to work around.
    """
    return db.execute(
        select(Organization.id)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(
            OrganizationMembership.user_id == user.id,
            Organization.is_active.is_(True),
            Organization.mfa_required.is_(True),
        )
        .limit(1)
    ).scalar_one_or_none() is not None


@router.post("/login", response_model=None)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> MeResponse | LoginChallenge:
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

    # --- second factor ----------------------------------------------------
    #
    # Checked after the password and after membership, so a wrong password never
    # reveals whether an account exists or whether it carries MFA. From here on
    # the caller has already proved they know the password.
    if _mfa_required_for(db, user):
        code = (body.mfa_code or "").strip()

        if user.mfa_enabled:
            if not code:
                return LoginChallenge(status="mfa_required")
            if not mfa.verify_code(secret=user.mfa_secret, code=code):
                _audit("login_failed", request=request, actor_email=body.email,
                       actor_user_id=user.id, detail={"reason": "bad_mfa_code"})
                raise HTTPException(status_code=401, detail="Invalid code")
        else:
            # Enrolment. The secret is minted once and kept across attempts:
            # regenerating it on every request would invalidate the QR the user
            # is in the middle of scanning.
            if user.mfa_secret is None:
                user.mfa_secret = mfa.generate_secret()
                db.commit()
            if not code:
                uri = mfa.provisioning_uri(
                    secret=user.mfa_secret, account_name=user.email
                )
                return LoginChallenge(
                    status="mfa_enrollment_required",
                    secret=user.mfa_secret,
                    otpauth_uri=uri,
                    qr_svg=mfa.qr_svg_data_uri(uri),
                )
            if not mfa.verify_code(secret=user.mfa_secret, code=code):
                _audit("login_failed", request=request, actor_email=body.email,
                       actor_user_id=user.id,
                       detail={"reason": "bad_mfa_enrollment_code"})
                raise HTTPException(status_code=401, detail="Invalid code")
            # Only now, having produced a working code, is it actually on. A
            # user who scanned nothing but reached this screen is not locked out
            # of an account they can still get into with the password alone.
            user.mfa_enabled = True
            db.commit()
            _audit("mfa_enrolled", request=request, actor_email=user.email,
                   actor_user_id=user.id)

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
        is_guest=False,
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
        is_guest=not roles,
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
        is_guest=identity.is_guest,
    )


class PasswordResetRedeem(BaseModel):
    token: str
    new_password: str


@router.post("/password-reset/redeem", status_code=200)
def redeem_password_reset(
    body: PasswordResetRedeem,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Set a new password from an emailed link. No session required.

    Unauthenticated by necessity - the point of a reset is that the person
    cannot sign in. The token is the whole of the authorisation, which is why it
    is single use, short lived, and stored only as a hash.

    Runs on a privileged session. Row-level security binds queries to an
    organisation taken from the caller's identity, and there is no caller and no
    identity here; a normal session would evaluate every policy against an unset
    org and find nothing, so every valid link would report itself invalid.
    """
    with PrivilegedSessionLocal() as privileged:
        try:
            user = password_reset.redeem(
                privileged, token_value=body.token, new_password=body.new_password
            )
        except PasswordError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except password_reset.ResetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Read before the commit. Org context here is unset, so anything read
        # afterwards comes back through policies that match nothing.
        user_id, email = user.id, user.email
        # Only so the audit entry lands somewhere an admin will find it. A user
        # can belong to several organisations and a password belongs to none of
        # them, so any one of theirs is as correct as another.
        organization_id = privileged.execute(
            select(OrganizationMembership.organization_id)
            .where(OrganizationMembership.user_id == user_id)
            .limit(1)
        ).scalar_one_or_none()
        privileged.commit()

    _audit(
        "user.password_reset.redeemed",
        request=request,
        actor_email=email,
        actor_user_id=user_id,
        organization_id=organization_id,
    )
    # Deliberately does not sign them in. Redemption revoked every session this
    # user had, and handing one straight back to whoever held the link would
    # undo exactly what that is for.
    return {"reset": True}
