import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.authorization import capabilities_for, visible_station_ids
from backend.auth.dependencies import get_identity
from backend.auth.identity import Identity
from backend.database.dependencies import get_db
from backend.database.models.device import Device
from backend.database.models.ground_station import GroundStation

router = APIRouter(prefix="/api/stations", tags=["stations"])

#: Windows the console offers, in hours. Bounded by the recorder's retention -
#: asking for more than is kept would draw a flat line rather than an error.
POWER_WINDOWS = {12: 12, 24: 24, 168: 168}


class DeviceSummary(BaseModel):
    id: str
    kind: str
    slug: str
    name: str


class StationSummary(BaseModel):
    id: str
    name: str
    timezone: str
    latitude: float | None
    longitude: float | None
    last_seen_at: str | None
    online: bool


class PowerPoint(BaseModel):
    t: str
    soc: float


class StationDetail(StationSummary):
    capabilities: list[str]
    devices: list[DeviceSummary]


# A station is considered offline once it has missed this much contact. The
# onboard computer reports far more often than this; the window is generous
# because a Starlink obstruction dropout is normal and should not flap the
# status of every station on the map.
OFFLINE_AFTER_SECONDS = 120


def _online(station: GroundStation) -> bool:
    if station.last_seen_at is None:
        return False
    from datetime import UTC, datetime

    return (
        datetime.now(UTC) - station.last_seen_at
    ).total_seconds() < OFFLINE_AFTER_SECONDS


def _summary(station: GroundStation) -> StationSummary:
    return StationSummary(
        id=str(station.id),
        name=station.name,
        timezone=station.timezone,
        latitude=station.latitude,
        longitude=station.longitude,
        last_seen_at=station.last_seen_at.isoformat()
        if station.last_seen_at
        else None,
        online=_online(station),
    )


@router.get("", response_model=list[StationSummary])
def list_stations(
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[StationSummary]:
    """The station switcher's contents: exactly what this user may see.

    Backed by the same visible_station_ids the realtime layer uses for the org
    status channel, so the switcher and the alert feed can never disagree about
    which stations exist for this user.
    """
    allowed = visible_station_ids(
        db, user_id=identity.user_id, organization_id=identity.organization_id
    )
    if not allowed:
        return []
    rows = db.execute(
        select(GroundStation)
        .where(GroundStation.id.in_(allowed))
        .order_by(GroundStation.name)
    ).scalars()
    return [_summary(s) for s in rows]


@router.get("/{station_id}/power/history", response_model=list[PowerPoint])
def power_history(
    station_id: uuid.UUID,
    hours: int = 12,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[PowerPoint]:
    """State of charge over a window, for the battery chart.

    Behind telemetry.view rather than being public to the org: it is the same
    data as the live power panel, just older, and the same people should see it.

    Downsampled at write time to one point per minute (see
    services/power_history.py), and thinned again here so a 7-day window returns
    a few hundred points rather than ten thousand - the chart is a few hundred
    pixels wide, so anything finer is invisible and only costs transfer.
    """
    from datetime import UTC, datetime, timedelta

    from backend.auth.authorization import capabilities_for
    from backend.auth.capabilities import Capability
    from backend.database.models.power_sample import PowerSample

    granted = capabilities_for(
        db,
        user_id=identity.user_id,
        organization_id=identity.organization_id,
        ground_station_id=station_id,
    )
    if Capability.TELEMETRY_VIEW not in granted:
        raise HTTPException(status_code=404, detail="Station not available")

    window = POWER_WINDOWS.get(hours)
    if window is None:
        raise HTTPException(status_code=422, detail="Unsupported window")

    since = datetime.now(UTC) - timedelta(hours=window)
    rows = db.execute(
        select(PowerSample.at, PowerSample.soc_pct)
        .where(
            PowerSample.ground_station_id == station_id,
            PowerSample.at >= since,
        )
        .order_by(PowerSample.at)
    ).all()

    # Thin to at most this many points, keeping the first and last so the
    # window's endpoints - and therefore the trend figure - stay honest.
    limit = 400
    if len(rows) > limit:
        step = len(rows) / limit
        picked = [rows[min(len(rows) - 1, int(i * step))] for i in range(limit)]
        picked[-1] = rows[-1]
        rows = picked

    return [PowerPoint(t=at.isoformat(), soc=soc) for at, soc in rows]


@router.get("/{station_id}", response_model=StationDetail)
def get_station(
    station_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> StationDetail:
    """Station detail plus this user's capabilities on it.

    The capabilities are returned so the console can render only the controls
    the user actually holds. That is presentation, not enforcement - every
    action is re-checked server-side, and the UI hiding a button is a courtesy
    rather than a security boundary.
    """
    granted = capabilities_for(
        db,
        user_id=identity.user_id,
        organization_id=identity.organization_id,
        ground_station_id=station_id,
    )
    if not granted:
        # 404 rather than 403, for the same reason as everywhere else: the
        # difference between "no access" and "does not exist" would leak the
        # existence of another tenant's hardware.
        raise HTTPException(status_code=404, detail="Station not available")

    station = db.execute(
        select(GroundStation).where(GroundStation.id == station_id)
    ).scalar_one_or_none()
    if station is None:
        raise HTTPException(status_code=404, detail="Station not available")

    devices = db.execute(
        select(Device)
        .where(Device.ground_station_id == station_id, Device.is_active.is_(True))
        .order_by(Device.kind, Device.slug)
    ).scalars()

    base = _summary(station)
    return StationDetail(
        **base.model_dump(),
        capabilities=sorted(c.value for c in granted),
        devices=[
            DeviceSummary(id=str(d.id), kind=d.kind, slug=d.slug, name=d.name)
            for d in devices
        ],
    )
