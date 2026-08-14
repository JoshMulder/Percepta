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

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.auth.password import PasswordError, hash_password
from backend.auth.platform import (
    PLATFORM_ORGANIZATION_ID,
    require_odin_watch,
    require_platform_admin,
)
from backend.database.dependencies import get_db
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.platform_alert import StationMaintenance
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.station_event import StationEvent
from backend.database.models.station_grant import StationGrant
from backend.database.models.user import User
from backend.realtime.bus import (
    adsb_snapshot_key,
    publish_roster_sync,
    read_latest_sync,
)
from backend.realtime.revocation import organization_changed, revoke_user
from backend.realtime.bus import (
    health_snapshot_key,
    power_snapshot_key,
    read_latest_sync,
)
from backend.services.audit import record
from backend.services.station_vitals import project_health, project_power
from backend.services.station_status import DARK_AFTER, ONLINE_WITHIN, status_for

#: ONLINE_WITHIN and DARK_AFTER are imported above, from
#: services/station_status. They used to be defined here as well, and the local
#: definition would have shadowed the import — which is the same class of bug as
#: the three independent copies this consolidation removed, only quieter.

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
    #: False for a removed organisation. It is still listed here — the platform
    #: view is where a removal is undone — but shown as removed.
    is_active: bool
    member_count: int
    station_count: int


class PlatformOverview(BaseModel):
    organizations: list[PlatformOrgOut]
    users: list[PlatformUserOut]
    roles: list[str]


# --- fleet dashboard -------------------------------------------------------


class FleetStats(BaseModel):
    stations_total: int
    stations_online: int
    stations_offline: int
    stations_dark: int
    stations_never: int
    stations_no_location: int
    stations_simulated: int
    organizations_total: int
    organizations_active: int
    faults_critical_24h: int
    faults_warning_24h: int


#: Event types that are NOT faults, however they are graded.
#:
#: `adsb.proximity` is emitted at warning severity on every close-and-low
#: contact (station/gsu/agent.py:1891). Measured on the live fleet, it was 46 of
#: the 71 warnings in 24 hours - so a KPI labelled "Faults 24h" was
#: two-thirds an aircraft counter, and one busy circuit could fill all 20 rows
#: of the attention feed with aeroplanes doing exactly what aeroplanes do.
#:
#: It is excluded here rather than downgraded at the station, because it IS a
#: warning to the tenant watching their own airspace. It is simply not a fault
#: in the fleet's health, which is the only question this endpoint asks.
NOT_A_FAULT = ("adsb.proximity", "radio.transmission")

class FleetStation(BaseModel):
    id: str
    name: str
    organization_id: str
    organization_name: str
    latitude: float | None
    longitude: float | None
    locality: str | None
    region: str | None
    #: "online" | "offline" | "never" — derived from last_seen_at, not stored.
    status: str
    #: Offline for long enough to count as gone dark, not merely between frames.
    dark: bool
    last_seen_at: str | None
    is_simulated: bool
    model: str | None
    config_version: int

    # --- tile vitals ------------------------------------------------------
    # Read from the ingest's Redis snapshots in two bulk MGETs for the whole
    # fleet, never per station and never from the database. All optional, and
    # null means "not known right now" rather than a value: a station that has
    # gone quiet stops having a state of charge, and a tile saying "unknown" is
    # worth more than one showing a number from twenty minutes ago.
    #
    # Radio squelch and camera stream state are deliberately NOT here, because
    # nothing caches them - only health, power and ADS-B frames are cached
    # (services/station_ingest.py); radio and camera exist solely on the live
    # per-station fan-out. The tile says whether those devices are FITTED and
    # well, which is what a wall can honestly know without subscribing to every
    # station at once.
    #: "ok" | "degraded" | "failing", as the station reports itself.
    health: str | None = None
    #: The worst open condition and how many there are. The station names its
    #: own conditions; the platform does not invent thresholds for them.
    worst_condition: str | None = None
    condition_count: int = 0
    #: The station's own view of its link home. Not the same question as
    #: `status` above - that is whether WE have heard from it, this is whether
    #: IT believes it is connected, and they disagree in the interesting cases.
    uplink_connected: bool | None = None
    uplink_offline_seconds: float | None = None
    #: State of charge: the number that decides whether anything else on the
    #: tile will still be true in six hours, on a solar site nobody visits.
    soc_pct: float | None = None
    #: Running on stored power - no mains and no generator contributing.
    on_battery: bool | None = None
    load_w: float | None = None
    #: What the station reports fitted, by slot, with its own device status.
    slots: dict[str, str] = {}
    #: Slots reporting synthetic data, so a wall never shows demo numbers as
    #: real. The station is authoritative about this; the platform is not.
    simulated_slots: list[str] = []
    running_version: str | None = None
    #: An active maintenance window, if any. Present on BOTH this and the pushed
    #: digest: the client swaps between the two sources, and a field in one and
    #: not the other is not a gap, it is a crash — see the position fields, which
    #: taught that lesson the expensive way.
    maintenance_until: str | None = None
    maintenance_reason: str | None = None


class FleetEvent(BaseModel):
    id: str
    station_id: str
    station_name: str
    organization_name: str
    type: str
    severity: str
    message: str | None
    received_at: str


class FleetView(BaseModel):
    stats: FleetStats
    stations: list[FleetStation]
    recent_events: list[FleetEvent]


class FleetAircraft(BaseModel):
    icao: str
    callsign: str | None = None
    latitude: float
    longitude: float
    altitude_m: float | None = None
    track_deg: float | None = None
    ground_speed_kt: float | None = None
    #: How many stations in the fleet are currently hearing this contact.
    heard_by: int


class FleetAdsb(BaseModel):
    #: Unique aircraft across the fleet (deduplicated by ICAO address).
    aircraft: list[FleetAircraft]
    #: Stations currently contributing a fix, so the map can show coverage.
    contributing_stations: int
    total_contacts: int


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrgRename(BaseModel):
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
    # All orgs, active and removed. A removed one carries `is_active=False` and is
    # shown as removed rather than hidden — this platform view is the one place a
    # removal is undone, so it has to be visible to be reactivated.
    orgs = db.execute(
        select(Organization).order_by(Organization.name)
    ).scalars().all()
    # For hiding a member's access to a removed org: the org is listed, but a
    # membership of it is not current access and is not shown as such.
    active_org_ids = {o.id for o in orgs if o.is_active}

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
                is_active=o.is_active,
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
                        # Only memberships of ACTIVE orgs: a membership to a
                        # removed org is not current access and is not shown so.
                        (m for m in memberships.get(u.id, []) if m.organization_id in active_org_ids),
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
        id=str(org.id), name=org.name, is_platform=False, is_active=True,
        member_count=0, station_count=0,
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


@router.patch("/organizations/{organization_id}", response_model=PlatformOrgOut)
def rename_organization(
    organization_id: uuid.UUID,
    body: OrgRename,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformOrgOut:
    """Rename an organisation.

    The one field of an organisation this cross-tenant surface changes — a name
    is how it is recognised everywhere, and only a platform admin sees enough of
    every tenant to spot a clash. Everything else about an org is managed inside
    it, under RLS.
    """
    org = db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="No such organisation")
    if organization_id == PLATFORM_ORGANIZATION_ID:
        # The platform org's name is a fixed system label — bootstrap sets it and
        # things read it. It is identified by its id, not its name, so a rename
        # would not break anything; it is refused because it should not be one of
        # the customer organisations an admin renames by mistake.
        raise HTTPException(
            status_code=409, detail="The platform organisation cannot be renamed"
        )

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A name is required")

    # Case-insensitive uniqueness, matching create — but excluding this org, so a
    # change of case in its own name is allowed and only a clash with a *different*
    # org is refused. The column's own UNIQUE constraint is the backstop.
    clash = db.execute(
        select(Organization).where(
            func.lower(Organization.name) == name.lower(),
            Organization.id != organization_id,
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status_code=409, detail=f"An organisation called {name!r} already exists"
        )

    member_count = db.execute(
        select(func.count(OrganizationMembership.id)).where(
            OrganizationMembership.organization_id == organization_id
        )
    ).scalar_one()
    station_count = db.execute(
        select(func.count(GroundStation.id)).where(
            GroundStation.organization_id == organization_id,
            GroundStation.is_active.is_(True),
        )
    ).scalar_one()
    out = PlatformOrgOut(
        id=str(org.id),
        name=name,
        is_platform=organization_id == PLATFORM_ORGANIZATION_ID,
        is_active=org.is_active,
        member_count=member_count,
        station_count=station_count,
    )

    previous = org.name
    if name == previous:
        # Nothing to change — return the current shape without a spurious audit row.
        return out

    org.name = name
    db.commit()

    # Nudge the renamed org's own consoles to re-pull, so the new name shows
    # without a reload — the same contentless signal a station rename sends.
    publish_roster_sync(organization_id)

    record(
        action="platform.organization.renamed",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="organization",
        target_id=str(organization_id),
        ip_address=request.client.host if request.client else None,
        detail={"from": previous, "to": name},
    )
    return out


@router.delete("/organizations/{organization_id}", status_code=200)
def remove_organization(
    organization_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Remove an organisation — a soft delete.

    `is_active` goes false rather than the row being deleted. That is the
    schema's own signal: login and the org switcher already exclude an inactive
    org, and the overview above hides it, so it disappears from use. Nothing is
    destroyed — its stations, members and their history stay in the database and
    the removal is reversible — which, for a whole tenant, is the only safe
    default. A member already signed into it keeps that session until it ends;
    they are not offered the org again once it is gone.

    The platform organisation cannot be removed: it is the tenant this
    cross-tenant surface runs inside.
    """
    org = db.get(Organization, organization_id)
    # An already-removed org reads as absent here, exactly as it does everywhere
    # else — so a double removal is a clean 404, not a second audit row.
    if org is None or not org.is_active:
        raise HTTPException(status_code=404, detail="No such organisation")
    if organization_id == PLATFORM_ORGANIZATION_ID:
        raise HTTPException(
            status_code=409, detail="The platform organisation cannot be removed"
        )

    removed_name = org.name
    org.is_active = False
    db.commit()

    record(
        action="platform.organization.removed",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="organization",
        target_id=str(organization_id),
        ip_address=request.client.host if request.client else None,
        detail={"name": removed_name},
    )
    return {"removed": True, "organization_id": str(organization_id)}


@router.post("/organizations/{organization_id}/reactivate", status_code=200)
def reactivate_organization(
    organization_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Undo a removal — the inverse of the soft delete above.

    Flips `is_active` back on, so the org returns to sign-in, the switcher and
    its members' access. Its stations and people were never touched, so it comes
    back exactly as it was.
    """
    org = db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="No such organisation")
    if org.is_active:
        # Already active — nothing to undo, and no audit row for a non-change.
        return {"reactivated": False, "organization_id": str(organization_id)}

    org.is_active = True
    db.commit()

    record(
        action="platform.organization.reactivated",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="organization",
        target_id=str(organization_id),
        ip_address=request.client.host if request.client else None,
        detail={"name": org.name},
    )
    return {"reactivated": True, "organization_id": str(organization_id)}


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
        # Count ADMINS, not members. Watch operators hold a platform membership
        # row too, and counting rows would happily remove the last person who can
        # administer anything while three people who cannot are still on shift -
        # leaving nobody able to add one back.
        remaining = db.execute(
            select(func.count(OrganizationMembership.id)).where(
                OrganizationMembership.organization_id == PLATFORM_ORGANIZATION_ID,
                OrganizationMembership.roles.any(UserRole.ADMIN.value),
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


# --- fleet monitoring ------------------------------------------------------


def _station_status(last_seen: datetime | None, now: datetime) -> tuple[str, bool]:
    """(status, dark) from last contact. Never stored.

    Thin wrapper kept for the call sites; the rule itself lives in
    services/station_status.py, which the per-org list, the dark alarm and Odin
    all read. Three copies of it used to exist, agreeing by luck.
    """
    return status_for(last_seen, now=now)


def _vitals(station_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """Tile vitals for the whole fleet, in two bulk reads.

    Two MGETs in total, not two per station - the same pattern api/stations.py
    uses for reported versions. Fail-soft by construction: read_latest_sync
    returns empty on a Redis failure, every field is optional, and a wall with no
    vitals still shows liveness rather than going blank.
    """
    if not station_ids:
        return {}
    out: dict[uuid.UUID, dict] = {sid: {} for sid in station_ids}

    for sid, blob in zip(
        station_ids, read_latest_sync([health_snapshot_key(s) for s in station_ids])
    ):
        if not blob:
            continue
        try:
            frame = json.loads(blob)
        except (ValueError, TypeError):
            continue
        # Projected by services/station_vitals, NOT here. This was a hand-written
        # copy of the same logic in odin_digest.note, the two had drifted by
        # three fields, and the copy that was missing them is the one the wall
        # prefers — so the drawer went blanker when the live feed came UP.
        out[sid].update(project_health(frame))

    for sid, blob in zip(
        station_ids, read_latest_sync([power_snapshot_key(s) for s in station_ids])
    ):
        if not blob:
            continue
        try:
            frame = json.loads(blob)
        except (ValueError, TypeError):
            continue
        # Shared with the digest — see the note on the health half above. The
        # "on battery" rule (null, not False, when the station reports neither
        # source) lives in station_vitals with the reason it exists.
        out[sid].update(project_power(frame))
    return out


@router.get("/fleet", response_model=FleetView)
def fleet(
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> FleetView:
    """The whole estate at a glance: every active station across every
    organisation, its position and whether it is being heard, rollup counts, and
    the most recent faults. Cross-tenant by design — the platform's own view."""
    now = datetime.now(timezone.utc)

    rows = db.execute(
        select(GroundStation, Organization.name)
        .join(Organization, GroundStation.organization_id == Organization.id)
        .where(GroundStation.is_active.is_(True), Organization.is_active.is_(True))
        .order_by(Organization.name, GroundStation.name)
    ).all()

    vitals = _vitals([station.id for station, _ in rows])

    # Active suppression windows, one query for the fleet.
    windows: dict[uuid.UUID, tuple[str, str]] = {
        sid: (until.isoformat(), reason)
        for sid, until, reason in db.execute(
            select(
                StationMaintenance.ground_station_id,
                StationMaintenance.until_at,
                StationMaintenance.reason,
            ).where(
                StationMaintenance.from_at <= now,
                StationMaintenance.until_at > now,
            )
        ).all()
    }

    stations: list[FleetStation] = []
    for station, org_name in rows:
        status, dark = _station_status(station.last_seen_at, now)
        v = vitals.get(station.id, {})
        model = None
        if isinstance(station.hardware, dict):
            candidate = station.hardware.get("model")
            model = candidate if isinstance(candidate, str) else None
        stations.append(FleetStation(
            id=str(station.id),
            name=station.name,
            organization_id=str(station.organization_id),
            organization_name=org_name,
            latitude=station.latitude,
            longitude=station.longitude,
            locality=station.locality,
            region=station.region,
            status=status,
            dark=dark,
            last_seen_at=station.last_seen_at.isoformat() if station.last_seen_at else None,
            is_simulated=station.is_simulated,
            model=model,
            config_version=station.config_version,
            health=v.get("health"),
            worst_condition=v.get("worst_condition"),
            condition_count=v.get("condition_count", 0),
            uplink_connected=v.get("uplink_connected"),
            uplink_offline_seconds=v.get("uplink_offline_seconds"),
            soc_pct=v.get("soc_pct"),
            on_battery=v.get("on_battery"),
            load_w=v.get("load_w"),
            slots=v.get("slots", {}),
            simulated_slots=v.get("simulated_slots", []),
            running_version=v.get("running_version"),
            maintenance_until=windows.get(station.id, (None, None))[0],
            maintenance_reason=windows.get(station.id, (None, None))[1],
        ))

    org_flags = db.execute(select(Organization.is_active)).scalars().all()
    since = now - timedelta(hours=24)
    severity_counts = dict(db.execute(
        select(StationEvent.severity, func.count(StationEvent.id))
        .where(
            StationEvent.received_at >= since,
            StationEvent.type.notin_(NOT_A_FAULT),
        )
        .group_by(StationEvent.severity)
    ).all())

    stats = FleetStats(
        stations_total=len(stations),
        stations_online=sum(1 for s in stations if s.status == "online"),
        stations_offline=sum(1 for s in stations if s.status == "offline"),
        stations_dark=sum(1 for s in stations if s.dark),
        stations_never=sum(1 for s in stations if s.status == "never"),
        stations_no_location=sum(
            1 for s in stations if s.latitude is None or s.longitude is None
        ),
        stations_simulated=sum(1 for s in stations if s.is_simulated),
        organizations_total=len(org_flags),
        organizations_active=sum(1 for active in org_flags if active),
        faults_critical_24h=severity_counts.get("critical", 0),
        faults_warning_24h=severity_counts.get("warning", 0),
    )

    # The "needs attention" feed: the most recent notable events fleet-wide.
    event_rows = db.execute(
        select(StationEvent, GroundStation.name, Organization.name)
        .join(GroundStation, StationEvent.ground_station_id == GroundStation.id)
        .join(Organization, StationEvent.organization_id == Organization.id)
        .where(
            StationEvent.severity.in_(("warning", "critical")),
            StationEvent.type.notin_(NOT_A_FAULT),
            # Bounded. Without a floor this walks backwards through the whole
            # table whenever the fleet is quiet, and a feed of month-old
            # warnings is not an attention feed.
            StationEvent.received_at >= now - timedelta(days=7),
        )
        .order_by(StationEvent.received_at.desc())
        .limit(20)
    ).all()
    recent_events = [
        FleetEvent(
            id=str(event.id),
            station_id=str(event.ground_station_id),
            station_name=station_name,
            organization_name=org_name,
            type=event.type,
            severity=event.severity,
            message=event.message,
            received_at=event.received_at.isoformat(),
        )
        for event, station_name, org_name in event_rows
    ]

    return FleetView(stats=stats, stations=stations, recent_events=recent_events)


@router.get("/adsb", response_model=FleetAdsb)
def fleet_adsb(
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> FleetAdsb:
    """Conglomerated ADS-B across the whole fleet.

    ADS-B lives only on each station's live telemetry stream — nothing stores it
    — so the ingest writes each station's most recent aircraft list to a short-
    lived Redis key (`realtime/bus.adsb_snapshot_key`). This reads that set in
    one shot and merges it by ICAO address: an aircraft two stations both hear is
    one contact, at one absolute position, tagged with how many stations see it.
    Empty (not an error) when the bus is unavailable — the map just has no
    traffic on it."""
    station_ids = db.execute(
        select(GroundStation.id).where(GroundStation.is_active.is_(True))
    ).scalars().all()
    if not station_ids:
        return FleetAdsb(aircraft=[], contributing_stations=0, total_contacts=0)

    blobs = read_latest_sync([adsb_snapshot_key(sid) for sid in station_ids])

    merged: dict[str, FleetAircraft] = {}
    contributing = 0
    total = 0
    for blob in blobs:
        if not blob:
            continue
        try:
            aircraft = (json.loads(blob) or {}).get("aircraft") or []
        except (ValueError, TypeError):
            continue
        contributed = False
        for contact in aircraft:
            icao = contact.get("icao")
            lat = contact.get("latitude")
            lon = contact.get("longitude")
            if not icao or lat is None or lon is None:
                continue
            contributed = True
            total += 1
            seen = merged.get(icao)
            if seen is None:
                merged[icao] = FleetAircraft(
                    icao=icao,
                    callsign=(contact.get("callsign") or None),
                    latitude=lat,
                    longitude=lon,
                    altitude_m=contact.get("altitude_m"),
                    track_deg=contact.get("track_deg"),
                    ground_speed_kt=contact.get("speed_kt"),
                    heard_by=1,
                )
            else:
                seen.heard_by += 1
        if contributed:
            contributing += 1

    return FleetAdsb(
        aircraft=list(merged.values()),
        contributing_stations=contributing,
        total_contacts=total,
    )
