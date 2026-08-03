"""Editing a station's own configuration.

Behind `config.write`, the same capability that guards radio calibration and
enrolment. These are settings an operator changes rarely and an admin owns:
where the station is, what it is called, and how much basemap to hold for it.

Note what is *not* here. The station's organisation is absent deliberately -
moving hardware between tenants is not a settings change, and offering it as one
invites it to happen by accident. `is_active` is absent for the same reason:
decommissioning has consequences for grants, history and audit that belong in a
deliberate flow rather than a toggle on a settings pane.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth.capabilities import Capability
from backend.auth.dependencies import require_capability
from backend.auth.identity import Identity
from backend.database.dependencies import get_db
from backend.database.models.ground_station import GroundStation
from backend.database.models.station_credential import StationCredential
from backend.services import enrolment
from backend.services import geocode
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stations/{station_id}/config", tags=["stations"])

#: Zoom bounds. 19 is as deep as any of our basemaps go (Esri imagery and OSM
#: street); OpenTopoMap stops at 17 and the console clamps per basemap. Below 3
#: the whole world is a few tiles and a "minimum" stops meaning anything.
MIN_ZOOM_FLOOR = 3
MAX_ZOOM_CEILING = 19

#: Cache radius. The tile count grows with the square of the radius and by 4x
#: per zoom level, so this and max_zoom together decide whether a prefetch is
#: minutes or days. 200km is already a very large fixed-site basemap.
MAX_RADIUS_KM = 200.0


class StationConfigUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    timezone: str = Field(min_length=1, max_length=64)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # Bounded by the deepest mine and low earth orbit rather than by anything
    # geodetic: the job is to reject a typo, not to police geography.
    elevation_m: float | None = Field(default=None, ge=-500, le=100000)
    map_min_zoom: int = Field(ge=MIN_ZOOM_FLOOR, le=MAX_ZOOM_CEILING)
    map_max_zoom: int = Field(ge=MIN_ZOOM_FLOOR, le=MAX_ZOOM_CEILING)
    map_radius_km: float = Field(gt=0, le=MAX_RADIUS_KM)
    # No `is_simulated`. The station reports per device whether its data is
    # synthetic, and `_reconcile_simulated` writes this row from the health
    # frame whenever it changes — so a value typed here was overwritten by the
    # box within half a minute. A control that silently does nothing is worse
    # than no control, and it is the same "two places to set one fact" that
    # the position below refuses, arriving from the other direction.


class StationConfigOut(BaseModel):
    id: str
    name: str
    timezone: str
    latitude: float | None
    longitude: float | None
    elevation_m: float | None
    map_min_zoom: int
    map_max_zoom: int
    map_radius_km: float
    is_simulated: bool
    #: Whether the position and name are still editable. Sent because the
    #: form has to know: they are settled at enrolment and refused after
    #: it, and a field that looks editable and then 409s is a worse way to
    #: learn that than one that never offered.
    enrolled: bool
    config_version: int


def _out(station: GroundStation, *, enrolled: bool) -> StationConfigOut:
    return StationConfigOut(
        id=str(station.id),
        name=station.name,
        timezone=station.timezone,
        latitude=station.latitude,
        longitude=station.longitude,
        elevation_m=station.elevation_m,
        map_min_zoom=station.map_min_zoom,
        map_max_zoom=station.map_max_zoom,
        map_radius_km=station.map_radius_km,
        is_simulated=station.is_simulated,
        enrolled=enrolled,
        config_version=station.config_version,
    )


@router.get("", response_model=StationConfigOut)
def get_config(
    station_id: uuid.UUID,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> StationConfigOut:
    station = db.get(GroundStation, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not available")
    return _out(station, enrolled=_has_enrolled(db, station_id))


@router.put("", response_model=StationConfigOut)
def update_config(
    station_id: uuid.UUID,
    body: StationConfigUpdate,
    request: Request,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> StationConfigOut:
    """Update a station's identity, position and basemap extent.

    Timezone is validated against the system zone database rather than accepted
    as free text. A station's local time is derived from it, and an unparseable
    zone would surface as a wrong clock on a remote site rather than as an error
    here.
    """
    station = db.get(GroundStation, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not available")

    # Name and position are settled at enrolment and not afterwards.
    #
    # The owner's reasoning, and it is right: a station that needs a different
    # position has physically moved, and a box that has moved needs commissioning
    # at its new site anyway — new coordinates, new basemap cache, quite possibly
    # a new owner. Letting someone type a new latitude into a settings pane makes
    # a station's history silently describe two different places, and every
    # bearing it ever reported becomes unattributable.
    #
    # Editable before enrolment, because until then the record is a plan rather
    # than a station and correcting a typo should not need a site visit.
    if _has_enrolled(db, station_id):
        frozen = {
            "name": (station.name, body.name.strip()),
            "latitude": (station.latitude, body.latitude),
            "longitude": (station.longitude, body.longitude),
            "elevation": (station.elevation_m, body.elevation_m),
        }
        changed_frozen = [k for k, (was, now) in frozen.items() if was != now]
        if changed_frozen:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{', '.join(sorted(changed_frozen))} cannot change after "
                    "enrolment. Re-enrol the station to move or rename it."
                ),
            )

    if body.map_min_zoom > body.map_max_zoom:
        raise HTTPException(
            status_code=422, detail="Minimum zoom cannot exceed maximum zoom"
        )

    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(body.timezone)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timezone {body.timezone!r}. Use an IANA zone, e.g. Pacific/Auckland.",
        ) from exc

    before = {
        "name": station.name,
        "timezone": station.timezone,
        "latitude": station.latitude,
        "longitude": station.longitude,
        "map_min_zoom": station.map_min_zoom,
        "map_max_zoom": station.map_max_zoom,
        "map_radius_km": station.map_radius_km,
    }

    station.name = body.name.strip()
    station.timezone = body.timezone
    station.latitude = body.latitude
    station.longitude = body.longitude
    station.elevation_m = body.elevation_m
    station.map_min_zoom = body.map_min_zoom
    station.map_max_zoom = body.map_max_zoom
    station.map_radius_km = body.map_radius_km

    # Words for the new coordinates, in the same transaction, so the row never
    # carries one position's numbers beside another's town. Only on an actual
    # change — this is somebody else's service, not something to call because a
    # zoom level was edited.
    if (station.latitude, station.longitude) != (before["latitude"], before["longitude"]):
        if station.latitude is None or station.longitude is None:
            station.locality = station.region = station.locality_for = None
        else:
            place = geocode.describe(station.latitude, station.longitude)
            station.locality = place["locality"] if place else None
            station.region = place["region"] if place else None
            station.locality_for = f"{station.latitude:.5f},{station.longitude:.5f}"

    after = {
        "name": station.name,
        "timezone": station.timezone,
        "latitude": station.latitude,
        "longitude": station.longitude,
        "map_min_zoom": station.map_min_zoom,
        "map_max_zoom": station.map_max_zoom,
        "map_radius_km": station.map_radius_km,
    }
    changed = {k: [before[k], after[k]] for k in before if before[k] != after[k]}

    # Build the response *before* committing. The org context is applied with
    # SET LOCAL, so it lasts exactly one transaction: after a commit, the next
    # read runs with no org set, RLS fails closed as designed, and refreshing
    # the instance raises ObjectDeletedError rather than returning the row we
    # just wrote. Read while the transaction that wrote it is still open.
    result = _out(station, enrolled=_has_enrolled(db, station_id))
    db.commit()

    if changed:
        record(
            action="station.config.updated",
            organization_id=identity.organization_id,
            actor_user_id=identity.user_id,
            target_type="ground_station",
            target_id=str(station_id),
            ground_station_id=station_id,
            ip_address=request.client.host if request.client else None,
            detail={"changed": changed},
        )
    return result


def _has_live_credential(db: Session, station_id: uuid.UUID) -> bool:
    """Whether a credential exists that a box could still authenticate with.

    Distinct from `_has_enrolled`, and the distinction is the whole of the
    delete rule. Revocation does not remove the credential row, it stamps
    `revoked_at` — so "a credential row exists" stays true for ever and delete
    was refused permanently, while the message told the operator to revoke and
    try again. The console meanwhile offers delete when `enrolled_at` is null,
    which revocation clears. Three views of "enrolled", disagreeing.

    This is the one that matches what delete is actually protecting: a box on a
    hill that can still publish. Once nothing can authenticate, the row behind
    the station is inert and there is nothing to protect.

    `enrolment.is_valid` rather than a fourth predicate — it is what
    `credential_valid` reports to the console, and the two must not drift.
    """
    credentials = db.execute(
        select(StationCredential)
        .where(StationCredential.ground_station_id == station_id)
    ).scalars().all()
    return any(enrolment.is_valid(c) for c in credentials)


def _has_enrolled(db: Session, station_id: uuid.UUID) -> bool:
    """Whether a credential has ever been issued for this station.

    The test for "is this a real station yet" throughout this module. Not
    `last_seen_at`: a station that enrolled and has not reported since is
    exactly when somebody reaches for delete or for a quick rename, and exactly
    when there is a box out there holding a working credential.
    """
    return db.execute(
        select(StationCredential.id)
        .where(StationCredential.ground_station_id == station_id)
        .limit(1)
    ).first() is not None


@router.delete("", status_code=204)
def delete_station(
    station_id: uuid.UUID,
    identity: Identity = Depends(require_capability(Capability.CONFIG_WRITE)),
    db: Session = Depends(get_db),
) -> Response:
    """Remove a station record that never became a station.

    **Only while nothing can authenticate as it.** A record is created in the console
    before anyone is standing at the hardware, so typos, abandoned plans and
    duplicates accumulate — and until a credential has been issued there is
    nothing behind the row: no telemetry, no history, nothing a person will
    ever want to look up. Deleting one is tidying, not decommissioning.

    Once a station has enrolled it stops being tidying. There is recorded
    history, there may be grants people rely on, and the box itself is out
    there holding a credential that would keep authenticating against a
    station id the platform no longer knows. That needs a decommissioning flow
    that revokes first and preserves the audit trail — deliberately not this,
    and refused here rather than half-done.

    The test is a *live* credential, not `last_seen_at` and not "a credential
    was ever issued". Not `last_seen_at`, because a station that enrolled and
    has gone quiet is exactly when somebody reaches for delete and exactly when
    a box may be sitting on a hill still able to publish. And not "ever
    issued", because revocation stamps `revoked_at` rather than removing the
    row: that test stayed true for ever, so delete was refused permanently
    while this endpoint's own message told the operator to revoke and retry.

    Revoking is therefore the decommissioning step the paragraph above asks
    for. Once it has happened nothing can authenticate, the audit trail is
    intact, and what is left is the tidying this endpoint is for.
    """
    station = db.get(GroundStation, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not available")

    if _has_live_credential(db, station_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "This station has a live credential, so it cannot be deleted "
                "here. "
                "Revoke its credential first."
            ),
        )

    name = station.name
    # Read what audit needs before the row goes: after the commit the org
    # context is gone with the transaction and the instance is unreadable.
    db.delete(station)
    db.commit()

    record(
        action="station.deleted",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="ground_station",
        target_id=str(station_id),
        ground_station_id=station_id,
        detail={"name": name, "enrolled": False},
    )
    log.info("Station %s (%s) deleted before enrolment.", name, station_id)
    return Response(status_code=204)
