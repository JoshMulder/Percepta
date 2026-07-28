"""Cross-organisation administration. Platform admins only.

This is the only part of the API that reads and writes across tenants, and it is
reachable only while the caller's active organisation *is* the platform
organisation - see `auth/platform.py`. Row-level security is bypassed for such a
session, so every query here must be scoped in code deliberately rather than
relying on the database to do it. That is the trade: god mode buys cross-org
reach and gives up the safety net, so this file is short on purpose.

Creating an organisation and putting people in it is the whole surface. Anything
that can be done *inside* one organisation belongs in `api/organization.py`,
where RLS still applies.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.auth.password import PasswordError, hash_password
from backend.auth.platform import PLATFORM_ORGANIZATION_ID, require_platform_admin
from backend.database.dependencies import get_db
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.station_grant import StationGrant
from backend.database.models.user import User
from backend.realtime.revocation import organization_changed, revoke_user
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/platform", tags=["platform"])

_ROLES = {r.value for r in UserRole}


class OrgMembershipOut(BaseModel):
    organization_id: str
    organization_name: str
    roles: list[str]


class PlatformUserOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    is_active: bool
    is_platform_admin: bool
    memberships: list[OrgMembershipOut]


class PlatformOrgOut(BaseModel):
    id: str
    name: str
    is_platform: bool
    member_count: int
    station_count: int


class PlatformOverview(BaseModel):
    organizations: list[PlatformOrgOut]
    users: list[PlatformUserOut]
    roles: list[str]


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=255)
    # Optional. Without one the account exists but cannot be signed in to, which
    # is the right default for an invite flow that does not exist yet - an inert
    # account beats a guessable one.
    password: str | None = None


class MembershipSet(BaseModel):
    organization_id: str
    roles: list[str]


def _overview(db: Session) -> PlatformOverview:
    orgs = db.execute(select(Organization).order_by(Organization.name)).scalars().all()

    member_counts = dict(
        db.execute(
            select(
                OrganizationMembership.organization_id,
                func.count(OrganizationMembership.id),
            ).group_by(OrganizationMembership.organization_id)
        ).all()
    )
    station_counts = dict(
        db.execute(
            select(GroundStation.organization_id, func.count(GroundStation.id))
            .where(GroundStation.is_active.is_(True))
            .group_by(GroundStation.organization_id)
        ).all()
    )

    org_names = {o.id: o.name for o in orgs}

    users = db.execute(select(User).order_by(User.display_name)).scalars().all()
    memberships: dict[uuid.UUID, list[OrganizationMembership]] = {}
    for m in db.execute(select(OrganizationMembership)).scalars().all():
        memberships.setdefault(m.user_id, []).append(m)

    return PlatformOverview(
        organizations=[
            PlatformOrgOut(
                id=str(o.id),
                name=o.name,
                is_platform=o.id == PLATFORM_ORGANIZATION_ID,
                member_count=member_counts.get(o.id, 0),
                station_count=station_counts.get(o.id, 0),
            )
            for o in orgs
        ],
        users=[
            PlatformUserOut(
                user_id=str(u.id),
                email=u.email,
                display_name=u.display_name,
                is_active=u.is_active,
                is_platform_admin=any(
                    m.organization_id == PLATFORM_ORGANIZATION_ID
                    for m in memberships.get(u.id, [])
                ),
                memberships=[
                    OrgMembershipOut(
                        organization_id=str(m.organization_id),
                        organization_name=org_names.get(m.organization_id, "—"),
                        roles=list(m.roles or []),
                    )
                    for m in sorted(
                        memberships.get(u.id, []),
                        key=lambda m: org_names.get(m.organization_id, ""),
                    )
                ],
            )
            for u in users
        ],
        roles=sorted(_ROLES),
    )


@router.get("", response_model=PlatformOverview)
def overview(
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformOverview:
    return _overview(db)


@router.post("/organizations", response_model=PlatformOrgOut, status_code=201)
def create_organization(
    body: OrgCreate,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformOrgOut:
    name = body.name.strip()
    existing = db.execute(
        select(Organization).where(func.lower(Organization.name) == name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"An organisation called {name!r} already exists"
        )

    org = Organization(name=name)
    db.add(org)
    db.flush()
    out = PlatformOrgOut(
        id=str(org.id), name=org.name, is_platform=False, member_count=0,
        station_count=0,
    )
    org_id = org.id
    db.commit()

    record(
        action="platform.organization.created",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="organization",
        target_id=str(org_id),
        ip_address=request.client.host if request.client else None,
        detail={"name": name},
    )
    return out


@router.post("/users", response_model=PlatformUserOut, status_code=201)
def create_user(
    body: UserCreate,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformUserOut:
    """Create an account. It belongs to no organisation until one is added.

    Deliberately two steps. A user and their access are different decisions, and
    creating an account that silently lands in an organisation is how someone
    ends up in a tenant nobody meant to put them in.
    """
    email = body.email.strip().lower()
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="That email is already in use")

    password_hash = None
    if body.password:
        try:
            password_hash = hash_password(body.password)
        except PasswordError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    user = User(
        email=email,
        display_name=body.display_name.strip(),
        password_hash=password_hash,
        is_active=True,
    )
    db.add(user)
    db.flush()
    out = PlatformUserOut(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        is_active=True,
        is_platform_admin=False,
        memberships=[],
    )
    user_id = user.id
    db.commit()

    record(
        action="platform.user.created",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="user",
        target_id=str(user_id),
        ip_address=request.client.host if request.client else None,
        detail={"email": email, "password_set": password_hash is not None},
    )
    return out


@router.put("/users/{user_id}/memberships", status_code=200)
def set_membership(
    user_id: uuid.UUID,
    body: MembershipSet,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Add a user to an organisation, or change their roles in it."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")

    try:
        org_id = uuid.UUID(body.organization_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid organisation id") from exc
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="No such organisation")

    roles = sorted(set(body.roles))
    unknown = [r for r in roles if r not in _ROLES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown roles: {unknown}")
    if not roles:
        raise HTTPException(status_code=422, detail="A member needs at least one role")

    membership = db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.organization_id == org_id,
        )
    ).scalar_one_or_none()
    previous = list(membership.roles or []) if membership else []
    if membership is None:
        db.add(
            OrganizationMembership(
                user_id=user_id, organization_id=org_id, roles=roles
            )
        )
    else:
        membership.roles = roles
    db.commit()

    revoke_user(user_id)
    organization_changed(org_id)

    record(
        action="platform.membership.updated",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="user",
        target_id=str(user_id),
        ip_address=request.client.host if request.client else None,
        detail={"organization_id": str(org_id), "from": previous, "to": roles},
    )
    return {"user_id": str(user_id), "organization_id": str(org_id), "roles": roles}


@router.delete("/users/{user_id}/memberships/{organization_id}", status_code=200)
def remove_membership(
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Remove a user from an organisation.

    Their station grants in that organisation go with it. A grant naming a
    station in an org you are no longer a member of grants nothing anyway, and
    leaving the rows behind would make an access review read as though they
    still had access.

    Removing the last platform administrator is refused. There is no console
    path back from a platform with none - recovering it needs database access.
    """
    membership = db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.organization_id == organization_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="Not a member of that organisation")

    if organization_id == PLATFORM_ORGANIZATION_ID:
        remaining = db.execute(
            select(func.count(OrganizationMembership.id)).where(
                OrganizationMembership.organization_id == PLATFORM_ORGANIZATION_ID
            )
        ).scalar_one()
        if remaining <= 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This is the only platform administrator. Add another before "
                    "removing this one."
                ),
            )

    grants = db.execute(
        select(StationGrant).where(
            StationGrant.user_id == user_id,
            StationGrant.organization_id == organization_id,
        )
    ).scalars().all()
    grant_count = len(grants)
    for grant in grants:
        db.delete(grant)
    db.delete(membership)
    db.commit()

    revoke_user(user_id)
    organization_changed(organization_id)

    record(
        action="platform.membership.removed",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="user",
        target_id=str(user_id),
        ip_address=request.client.host if request.client else None,
        detail={
            "organization_id": str(organization_id),
            "station_grants_removed": grant_count,
        },
    )
    return {"removed": True, "station_grants_removed": grant_count}
