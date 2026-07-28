import uuid

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.authorization import capabilities_for, visible_station_ids
from backend.auth.dependencies import get_identity, require_admin
from backend.auth.identity import Identity
from backend.database.dependencies import get_db
from backend.database.models.device import Device
from backend.database.models.ground_station import GroundStation
from backend.services.audit import record

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
    # Synthetic data. Drives the DEMO badge and suppresses fault indication -
    # on a simulated station a fault would only ever mean the simulator stopped.
    is_simulated: bool


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
        is_simulated=station.is_simulated,
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


class StationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


@router.post("", response_model=StationSummary, status_code=201)
def create_station(
    body: StationCreate,
    request: Request,
    identity: Identity = Depends(require_admin),
    db: Session = Depends(get_db),
) -> StationSummary:
    """Create a station record, before any hardware exists.

    Admin only, and the organisation comes from the caller's session rather than
    the request body - creating a record is the moment a site is bound to a
    tenant, and that binding is never something a client gets to name.

    The record is deliberately useful on its own: it can be configured and
    granted to users straight away, and it is what an enrolment code is later
    issued against. Position may be left empty now and filled in once the
    hardware is on site.
    """
    try:
        ZoneInfo(body.timezone)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timezone {body.timezone!r}. Use an IANA zone, e.g. Pacific/Auckland.",
        ) from exc

    station = GroundStation(
        organization_id=identity.organization_id,
        name=body.name.strip(),
        timezone=body.timezone,
        latitude=body.latitude,
        longitude=body.longitude,
    )
    db.add(station)
    db.flush()
    # Built before the commit: the org context is SET LOCAL and lasts one
    # transaction, so reading the row back afterwards finds nothing.
    summary = _summary(station)
    station_id = station.id
    db.commit()

    record(
        action="station.created",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="ground_station",
        target_id=str(station_id),
        ground_station_id=station_id,
        ip_address=request.client.host if request.client else None,
        detail={"name": summary.name, "timezone": summary.timezone},
    )
    return summary


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
