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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth.capabilities import Capability
from backend.auth.dependencies import require_capability
from backend.auth.identity import Identity
from backend.database.dependencies import get_db
from backend.database.models.ground_station import GroundStation
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
    map_min_zoom: int = Field(ge=MIN_ZOOM_FLOOR, le=MAX_ZOOM_CEILING)
    map_max_zoom: int = Field(ge=MIN_ZOOM_FLOOR, le=MAX_ZOOM_CEILING)
    map_radius_km: float = Field(gt=0, le=MAX_RADIUS_KM)


class StationConfigOut(BaseModel):
    id: str
    name: str
    timezone: str
    latitude: float | None
    longitude: float | None
    map_min_zoom: int
    map_max_zoom: int
    map_radius_km: float
    config_version: int


def _out(station: GroundStation) -> StationConfigOut:
    return StationConfigOut(
        id=str(station.id),
        name=station.name,
        timezone=station.timezone,
        latitude=station.latitude,
        longitude=station.longitude,
        map_min_zoom=station.map_min_zoom,
        map_max_zoom=station.map_max_zoom,
        map_radius_km=station.map_radius_km,
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
    return _out(station)


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
    station.map_min_zoom = body.map_min_zoom
    station.map_max_zoom = body.map_max_zoom
    station.map_radius_km = body.map_radius_km

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
    result = _out(station)
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
