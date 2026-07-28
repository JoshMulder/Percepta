"""The single place where "may this person do this, here" is answered.

Every path that grants access to a ground station goes through
`capabilities_for` - the WebSocket subscribe handler, the media stream-ticket
endpoint, and any future REST route. One function, one answer, one place to audit.

That matters more than tidiness. The isolation design (docs/03-realtime-isolation.md)
gets its fail-closed property from authorising once, at subscribe time, and then
treating fan-out group membership *as* the permission. If two paths computed
authorisation differently, the group would stop meaning what it claims to mean.

Relationship to RLS: this runs on the request's session, so every query it makes
is already org-scoped by row-level security. A station or grant belonging to
another org is not merely filtered out here - it is invisible at the database.
This function is the second layer, not the only one.
"""

import uuid

from sqlalchemy.orm import Session

from backend.auth.capabilities import (
    ACTUATOR_CAPABILITIES,
    GRANTABLE_CAPABILITIES,
    READ_CAPABILITIES,
    Capability,
)
from backend.database.models.enums import UserRole
from backend.repositories.organization_membership_repository import (
    OrganizationMembershipRepository,
)
from backend.repositories.station_grant_repository import StationGrantRepository

_NONE: frozenset[Capability] = frozenset()


def capabilities_for(
    db: Session,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    ground_station_id: uuid.UUID,
) -> frozenset[Capability]:
    """What this user may do at this station, right now.

    Returns an empty set for every "no" - a user with no membership, no grant,
    an expired grant, a deactivated station, or a station in another org are all
    indistinguishable from here, deliberately. Callers should not be able to
    tell "you have no access" from "that station does not exist", because the
    difference leaks the existence of another tenant's hardware.
    """
    membership_repo = OrganizationMembershipRepository(db)
    grant_repo = StationGrantRepository(db)

    roles = set(membership_repo.roles(user_id=user_id, organization_id=organization_id))
    if not roles:
        # Not a member of this org at all.
        return _NONE

    # The station must exist, be active, and be visible in this org context.
    # RLS enforces the org part; a deactivated station grants nothing to anyone,
    # including admins - taking a station out of service should stop control of
    # it, not just hide it from the list.
    if grant_repo.active_station(ground_station_id=ground_station_id) is None:
        return _NONE

    if UserRole.ADMIN.value in roles:
        # Org admins implicitly hold everything grantable on every station in
        # their own org - which is what DroneOps' require_admin already means.
        # Note "grantable", not "every": radio.transmit stays out until the
        # hardware and the operator licensing exist to justify it, and an admin
        # is not an exception to that.
        granted = frozenset(GRANTABLE_CAPABILITIES)
    else:
        grant = grant_repo.get_live(
            user_id=user_id, ground_station_id=ground_station_id
        )
        if grant is None:
            return _NONE
        granted = frozenset(
            Capability(c) for c in (grant.capabilities or []) if c in _VALID_VALUES
        )

    # The viewer ceiling applies last and applies to everyone. If a user somehow
    # holds both admin and viewer, the more restrictive wins - a security
    # decision, not an oversight. A ceiling that could be escaped by holding one
    # more role would not be a ceiling.
    if UserRole.VIEWER.value in roles:
        granted &= READ_CAPABILITIES

    return granted


_VALID_VALUES = {c.value for c in Capability}


def has_capability(
    db: Session,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    ground_station_id: uuid.UUID,
    capability: Capability,
) -> bool:
    return capability in capabilities_for(
        db,
        user_id=user_id,
        organization_id=organization_id,
        ground_station_id=ground_station_id,
    )


def visible_station_ids(
    db: Session, *, user_id: uuid.UUID, organization_id: uuid.UUID
) -> set[uuid.UUID]:
    """Stations this user may see at all - the station switcher's contents, and
    the subscription set for the org status channel.

    For an admin this is every active station in the org; for everyone else it
    is exactly their live grants that include station.view. Both are already
    org-scoped by RLS.
    """
    from sqlalchemy import select

    from backend.database.models.ground_station import GroundStation

    roles = set(
        OrganizationMembershipRepository(db).roles(
            user_id=user_id, organization_id=organization_id
        )
    )
    if not roles:
        return set()

    if UserRole.ADMIN.value in roles:
        rows = db.execute(
            select(GroundStation.id).where(GroundStation.is_active.is_(True))
        ).scalars()
        return set(rows)

    grants = StationGrantRepository(db).list_for_user(user_id=user_id)
    candidates = {
        g.ground_station_id
        for g in grants
        if Capability.STATION_VIEW.value in (g.capabilities or [])
    }
    if not candidates:
        return set()

    # A grant naming a deactivated station shows nothing.
    rows = db.execute(
        select(GroundStation.id).where(
            GroundStation.id.in_(candidates), GroundStation.is_active.is_(True)
        )
    ).scalars()
    return set(rows)


def assert_grantable(capabilities: list[str]) -> None:
    """Guard for the grant-writing path.

    Rejects anything not currently grantable - today that is radio.transmit,
    which exists so the permission model is built and tested before a certified
    transceiver is wired in, and which must not be handed out before then.
    """
    unknown = [c for c in capabilities if c not in _VALID_VALUES]
    if unknown:
        raise ValueError(f"Unknown capabilities: {sorted(unknown)}")

    grantable = {c.value for c in GRANTABLE_CAPABILITIES}
    refused = [c for c in capabilities if c not in grantable]
    if refused:
        raise ValueError(
            f"Capabilities not currently grantable: {sorted(refused)}. "
            "radio.transmit is reserved until certified transmit hardware and "
            "operator licensing are in place."
        )


def actuator_capabilities(granted: frozenset[Capability]) -> frozenset[Capability]:
    """The subset with a physical effect at the station. Every use of one of
    these is audited."""
    return granted & ACTUATOR_CAPABILITIES
