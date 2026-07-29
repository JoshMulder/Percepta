"""Who is in this organisation, and what they may reach.

Admin only. This is the file that decides access, so a few rules are enforced
here rather than trusted to the caller:

**radio.transmit can never be granted.** `assert_grantable` refuses it. It exists
so the permission model is built and tested before a certified transceiver is
wired in, and handing it out early is exactly the mistake the ungrantable set
prevents.

**A viewer's ceiling is applied at read time, not write time.** Grants are stored
as asked for; `capabilities_for` intersects them with what the role permits. So
demoting someone to viewer immediately narrows what their existing grants do,
without rewriting rows - and promoting them back restores it. Filtering on write
would silently discard capabilities an admin would then have to re-add.

**An org cannot be left without an admin.** The last one may not demote or remove
themselves. Recovering from that needs database access, which for a
customer-facing platform means a support incident.

Every change here is pushed to live connections. A user whose grant is removed
while watching a stream must lose it now, not at their next page load - see
docs/03-realtime-isolation.md section 6.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.authorization import assert_grantable
from backend.auth.dependencies import require_admin
from backend.auth.identity import Identity
from backend.database.dependencies import get_db
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.station_grant import StationGrant
from backend.database.models.user import User
from backend.realtime.revocation import grants_changed, revoke_user
from backend.core.email import EmailNotConfiguredError
from backend.services import password_reset
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/organization", tags=["organization"])

_ROLES = {r.value for r in UserRole}


class GrantOut(BaseModel):
    ground_station_id: str
    capabilities: list[str]
    expires_at: str | None


class MemberOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    is_active: bool
    roles: list[str]
    grants: list[GrantOut]
    #: Whether they have completed enrolment. Never the secret - an admin has no
    #: business holding somebody else's second factor.
    mfa_enabled: bool = False


class MfaUpdate(BaseModel):
    mfa_required: bool


class OrganizationOut(BaseModel):
    id: str
    name: str
    #: Members of this organisation must present a second factor to sign in.
    mfa_required: bool = False
    members: list[MemberOut]
    stations: list[dict]
    #: What an admin is allowed to tick. radio.transmit is excluded and stays
    #: excluded until certified hardware exists.
    grantable_capabilities: list[str]
    roles: list[str]


class GrantUpdate(BaseModel):
    ground_station_id: str
    capabilities: list[str]


class RolesUpdate(BaseModel):
    roles: list[str]


def _members(db: Session, organization_id: uuid.UUID) -> list[MemberOut]:
    memberships = db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id
        )
    ).scalars().all()

    user_ids = [m.user_id for m in memberships]
    users = {
        u.id: u
        for u in db.execute(select(User).where(User.id.in_(user_ids)))
        .scalars()
        .all()
    } if user_ids else {}

    grants: dict[uuid.UUID, list[StationGrant]] = {}
    if user_ids:
        for grant in db.execute(
            select(StationGrant).where(StationGrant.user_id.in_(user_ids))
        ).scalars().all():
            grants.setdefault(grant.user_id, []).append(grant)

    out: list[MemberOut] = []
    for membership in memberships:
        user = users.get(membership.user_id)
        if user is None:
            continue
        out.append(
            MemberOut(
                user_id=str(user.id),
                email=user.email,
                display_name=user.display_name,
                is_active=user.is_active,
                mfa_enabled=user.mfa_enabled,
                roles=list(membership.roles or []),
                grants=[
                    GrantOut(
                        ground_station_id=str(g.ground_station_id),
                        capabilities=list(g.capabilities or []),
                        expires_at=g.expires_at.isoformat() if g.expires_at else None,
                    )
                    for g in grants.get(user.id, [])
                ],
            )
        )
    out.sort(key=lambda m: m.display_name.lower())
    return out


def _admin_count(db: Session, organization_id: uuid.UUID) -> int:
    rows = db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id
        )
    ).scalars().all()
    return sum(1 for r in rows if UserRole.ADMIN in (r.roles or []))


def _membership(
    db: Session, organization_id: uuid.UUID, user_id: uuid.UUID
) -> OrganizationMembership:
    membership = db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="Not a member of this organisation")
    return membership


@router.get("", response_model=OrganizationOut)
def get_organization(
    identity: Identity = Depends(require_admin),
    db: Session = Depends(get_db),
) -> OrganizationOut:
    from backend.auth.capabilities import GRANTABLE_CAPABILITIES

    org = db.get(Organization, identity.organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    stations = db.execute(
        select(GroundStation)
        .where(GroundStation.organization_id == identity.organization_id)
        .order_by(GroundStation.name)
    ).scalars().all()

    return OrganizationOut(
        id=str(org.id),
        name=org.name,
        mfa_required=org.mfa_required,
        members=_members(db, identity.organization_id),
        stations=[
            {"id": str(s.id), "name": s.name, "is_active": s.is_active}
            for s in stations
        ],
        grantable_capabilities=sorted(c.value for c in GRANTABLE_CAPABILITIES),
        roles=sorted(_ROLES),
    )


@router.put("/members/{user_id}/grants", status_code=200)
def set_grant(
    user_id: uuid.UUID,
    body: GrantUpdate,
    request: Request,
    identity: Identity = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Set what one user may do at one station.

    An empty capability list removes the grant entirely rather than storing a
    row that grants nothing. "Who can reach station 7" should be answerable by
    reading rows, without having to filter out empty ones.
    """
    _membership(db, identity.organization_id, user_id)

    try:
        station_id = uuid.UUID(body.ground_station_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid station id") from exc

    # RLS already confines this to the caller's org; the explicit check turns a
    # cross-org id into a clear 404 rather than a confusing "not found".
    station = db.get(GroundStation, station_id)
    if station is None or station.organization_id != identity.organization_id:
        raise HTTPException(status_code=404, detail="Station not available")

    capabilities = sorted(set(body.capabilities))
    try:
        assert_grantable(capabilities)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = db.execute(
        select(StationGrant).where(
            StationGrant.user_id == user_id,
            StationGrant.ground_station_id == station_id,
        )
    ).scalar_one_or_none()

    previous = list(existing.capabilities or []) if existing else []

    if not capabilities:
        if existing is not None:
            db.delete(existing)
    elif existing is not None:
        existing.capabilities = capabilities
        existing.granted_by = identity.user_id
    else:
        db.add(
            StationGrant(
                organization_id=identity.organization_id,
                user_id=user_id,
                ground_station_id=station_id,
                capabilities=capabilities,
                granted_by=identity.user_id,
            )
        )
    db.commit()

    # Reaches sockets that are already open under the old grant.
    grants_changed(user_id)

    record(
        action="organization.grant.updated",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="user",
        target_id=str(user_id),
        ground_station_id=station_id,
        ip_address=request.client.host if request.client else None,
        detail={"from": previous, "to": capabilities},
    )
    return {"ground_station_id": str(station_id), "capabilities": capabilities}


@router.put("/members/{user_id}/roles", status_code=200)
def set_roles(
    user_id: uuid.UUID,
    body: RolesUpdate,
    request: Request,
    identity: Identity = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Change a member's org-wide roles.

    Roles decide what someone *is* here; grants decide what they may touch.
    Promoting to admin therefore hands over every capability on every station in
    the org implicitly, which is why this needs admin and is audited.
    """
    membership = _membership(db, identity.organization_id, user_id)

    roles = sorted(set(body.roles))
    unknown = [r for r in roles if r not in _ROLES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown roles: {unknown}")
    if not roles:
        raise HTTPException(status_code=422, detail="A member needs at least one role")

    previous = list(membership.roles or [])
    losing_admin = UserRole.ADMIN in previous and UserRole.ADMIN not in roles
    if losing_admin and _admin_count(db, identity.organization_id) <= 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "This is the organisation's only administrator. Promote someone "
                "else before changing this."
            ),
        )

    membership.roles = roles
    db.commit()

    # Roles feed the viewer ceiling and the admin implicit-everything path, so
    # a change here alters what live connections may do.
    revoke_user(user_id)

    record(
        action="organization.roles.updated",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="user",
        target_id=str(user_id),
        ip_address=request.client.host if request.client else None,
        detail={"from": previous, "to": roles},
    )
    return {"user_id": str(user_id), "roles": roles}


@router.post("/members/{user_id}/password-reset", status_code=200)
def send_password_reset(
    user_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Email this member a link to set a new password.

    The admin never learns or chooses the password. A password an administrator
    picked is known to two people from the moment it exists, usually travels
    over chat, and tends to be the one nobody changes.

    Membership is checked first, so this cannot be used to probe for or reset
    accounts outside the organisation being administered.
    """
    _membership(db, identity.organization_id, user_id)

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="Not a member of this organisation")

    _, plaintext = password_reset.issue(
        db, user=user, requested_by=identity.user_id
    )

    # Sent before the commit. If the mail server refuses, nothing is written and
    # the admin gets an error instead of a live link nobody received - which
    # they would otherwise have no way to distinguish from a full inbox.
    try:
        password_reset.send(user=user, plaintext=plaintext, by_admin=True)
    except EmailNotConfiguredError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        db.rollback()
        log.exception("Password reset mail failed")
        raise HTTPException(
            status_code=502,
            detail=f"The email could not be sent: {exc}",
        ) from exc

    email = user.email
    db.commit()

    record(
        action="user.password_reset.sent",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="user",
        target_id=str(user_id),
        ip_address=request.client.host if request.client else None,
        detail={"target_email": email},
    )
    return {"user_id": str(user_id), "sent_to": email}


@router.put("/mfa", status_code=200)
def set_mfa_required(
    body: MfaUpdate,
    request: Request,
    identity: Identity = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Require a second factor from everyone in this organisation.

    Turning it on does not lock anybody out. Members who have not enrolled are
    walked through it at their next sign-in, after their password has already
    been accepted, so the worst case is one extra screen rather than a support
    call from someone who cannot get in.

    Turning it off leaves existing enrolments alone. Someone who set up an
    authenticator keeps using it - the setting governs who is compelled, not who
    is permitted, and silently weakening an account because an admin changed an
    organisation-wide switch is not a thing to do quietly.
    """
    org = db.get(Organization, identity.organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    previous = org.mfa_required
    org.mfa_required = body.mfa_required
    db.commit()

    record(
        action="organization.mfa_required.updated",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="organization",
        target_id=str(identity.organization_id),
        ip_address=request.client.host if request.client else None,
        detail={"from": previous, "to": body.mfa_required},
    )
    return {"mfa_required": body.mfa_required}


class MemberInvite(BaseModel):
    email: str
    display_name: str
    roles: list[str] = ["viewer"]


@router.post("/members", status_code=201)
def invite_member(
    body: MemberInvite,
    request: Request,
    identity: Identity = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Add someone to this organisation and email them a link to set a password.

    The admin supplies an address and a name, never a password. An invitation
    carrying a password an administrator chose is a password two people know
    before it has been used once - the same reason resets are emailed rather
    than typed in on somebody's behalf.

    An address that already has an account is added to this organisation rather
    than rejected. People legitimately belong to more than one, and refusing
    would also turn this into a way to ask whether an address is registered.
    """
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="That is not an email address")

    roles = sorted(set(body.roles))
    unknown = [r for r in roles if r not in _ROLES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown roles: {unknown}")
    if not roles:
        raise HTTPException(status_code=422, detail="A member needs at least one role")

    org = db.get(Organization, identity.organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    # users sits outside RLS - it has to, since an account exists before and
    # independently of any organisation.
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    created = user is None
    if user is None:
        user = User(
            email=email,
            display_name=body.display_name.strip() or email,
            # No password. The account cannot be signed into until the person
            # sets one, so an invitation that goes astray grants nothing.
            password_hash=None,
            is_active=True,
        )
        db.add(user)
        db.flush()

    existing = db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.organization_id == identity.organization_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="They are already in this organisation"
        )

    db.add(
        OrganizationMembership(
            user_id=user.id,
            organization_id=identity.organization_id,
            roles=roles,
        )
    )

    # Only someone who cannot yet sign in needs a link. An existing account has
    # a password already, and sending them a set-password link would be handing
    # an org admin a way to take over an account in someone else's tenancy.
    invited = created or user.password_hash is None
    if invited:
        _, plaintext = password_reset.issue(
            db, user=user, requested_by=identity.user_id
        )
        actor = db.get(User, identity.user_id)
        try:
            password_reset.send_invitation(
                user=user,
                plaintext=plaintext,
                organization_name=org.name,
                inviter=actor.display_name if actor else "An administrator",
            )
        except EmailNotConfiguredError as exc:
            db.rollback()
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except OSError as exc:
            db.rollback()
            log.exception("Invitation mail failed")
            raise HTTPException(
                status_code=502, detail=f"The invitation could not be sent: {exc}"
            ) from exc

    user_id, user_email = user.id, user.email
    db.commit()

    record(
        action="organization.member.invited",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="user",
        target_id=str(user_id),
        ip_address=request.client.host if request.client else None,
        detail={"email": user_email, "roles": roles, "new_account": created},
    )
    return {
        "user_id": str(user_id),
        "email": user_email,
        "roles": roles,
        "invitation_sent": invited,
    }
