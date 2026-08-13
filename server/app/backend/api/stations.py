import json
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
from backend.realtime.bus import (
    health_snapshot_key,
    publish_roster_sync,
    read_latest_sync,
)
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
    # The version this station last reported running, from the cached health
    # frame — for the console's update-available pill. Null when it has not
    # reported, or when the cache is unavailable.
    running_version: str | None = None
    # The version it is updating TO, while an update is in flight. The station
    # reports this itself, so "Updating…" survives a page reload — the console
    # cannot know it from local state alone, and a refresh mid-update otherwise
    # re-offers an update that is already running.
    desired_version: str | None = None


class PowerPoint(BaseModel):
    t: str
    soc: float
    # The flows the history popout charts beneath the state of charge. Null,
    # not 0, when the source is not fitted — a site with no grid or no generator
    # leaves them out and the chart leaves them off the legend. Short keys: a
    # 7-day window is a few hundred of these and the names repeat on every one.
    pv: float | None = None
    load: float | None = None
    mains: float | None = None
    gen: float | None = None


class TranscriptPoint(BaseModel):
    #: The station's own time for the transmission. `clock` says whether to
    #: trust it — a box with no battery-backed clock still logs, it just cannot
    #: place the entry on a timeline with confidence.
    t: str
    clock: str
    message: str


class WeatherPoint(BaseModel):
    t: str
    # Each nullable, and null is "no such sensor", not zero — a station may have
    # no humidity module or no barometer, and the chart leaves that trace out
    # rather than drawing it flat along the bottom.
    temp: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    wind: float | None = None


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


def _summary(
    station: GroundStation,
    versions: tuple[str | None, str | None] = (None, None),
) -> StationSummary:
    running, desired = versions
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
        running_version=running,
        desired_version=desired,
    )


def _reported_versions(
    station_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[str | None, str | None]]:
    """Map each station to (running, desired) as it last reported them, read from
    the ingest's cached health snapshots in one MGET.

    `desired` is set only while an update is in flight, and is what lets the
    console show "Updating…" across a page reload — local state cannot survive
    one, and without this a refresh mid-update re-offers an update already
    running. Best-effort: a station with no cached frame is simply absent, and
    the pill stays off for that row."""
    if not station_ids:
        return {}
    out: dict[uuid.UUID, tuple[str | None, str | None]] = {}
    keys = [health_snapshot_key(sid) for sid in station_ids]
    for sid, blob in zip(station_ids, read_latest_sync(keys)):
        if not blob:
            continue
        try:
            frame = json.loads(blob)
        except (ValueError, TypeError):
            continue
        software = frame.get("software") or {}
        running = software.get("running_version") or frame.get("agent_version")
        desired = software.get("desired_version")
        out[sid] = (
            running if isinstance(running, str) else None,
            desired if isinstance(desired, str) else None,
        )
    return out


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
    ).scalars().all()
    versions = _reported_versions([s.id for s in rows])
    return [_summary(s, versions.get(s.id, (None, None))) for s in rows]


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
    # Every console in the org learns of the new station at once, rather than on
    # its next slow list reconcile — a fresh enrolment shows up live.
    publish_roster_sync(identity.organization_id)
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
        select(
            PowerSample.at,
            PowerSample.soc_pct,
            PowerSample.pv_w,
            PowerSample.load_w,
            PowerSample.mains_w,
            PowerSample.generator_w,
        )
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

    return [
        PowerPoint(
            t=r.at.isoformat(),
            soc=r.soc_pct,
            pv=r.pv_w,
            load=r.load_w,
            mains=r.mains_w,
            gen=r.generator_w,
        )
        for r in rows
    ]


class StationHealth(BaseModel):
    """The selected station's latest self-reported host stats, for the settings
    overview. Null fields mean the station has not reported within the snapshot's
    lifetime — the panel shows 'no recent telemetry' rather than stale numbers."""

    online: bool
    #: ok | degraded | failing, when it has reported recently.
    status: str | None = None
    #: Host device stats — cpu_percent, load_1m, temperature_c, uptime_s (host),
    #: memory{total_mb,used_mb,used_percent}. Best-effort on the station, so any
    #: field may be absent; the whole object is null until a frame is cached.
    system: dict | None = None
    #: The station's own account of its software: running_version,
    #: desired_version while an update is in flight, and how the last one went
    #: (update_last_result / update_last_version / update_at). This is the only
    #: place a *failed* update surfaces — "rolled_back", "signature_rejected" —
    #: so the console can say why rather than just showing no change.
    software: dict | None = None


@router.get("/{station_id}/health", response_model=StationHealth)
def station_health(
    station_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> StationHealth:
    """The selected station's latest host system stats, for the settings overview.

    Health lives only on the live fan-out; the ingest caches each station's last
    frame in Redis (`health_snapshot_key`), TTL'd, and this reads that one key.
    Behind telemetry.view — it is the same self-description the live health panel
    shows, and the same people should see it. A station that has not reported
    within the snapshot's lifetime returns nulls, which the panel renders as 'no
    recent telemetry' rather than stale numbers.
    """
    from backend.auth.capabilities import Capability

    granted = capabilities_for(
        db,
        user_id=identity.user_id,
        organization_id=identity.organization_id,
        ground_station_id=station_id,
    )
    if Capability.TELEMETRY_VIEW not in granted:
        raise HTTPException(status_code=404, detail="Station not available")

    station = db.get(GroundStation, station_id)
    online = _online(station) if station is not None else False

    blob = next(iter(read_latest_sync([health_snapshot_key(station_id)])), None)
    if not blob:
        return StationHealth(online=online)
    try:
        frame = json.loads(blob)
    except (ValueError, TypeError):
        return StationHealth(online=online)
    system = frame.get("system")
    status = frame.get("status")
    software = frame.get("software")
    return StationHealth(
        online=online,
        status=status if isinstance(status, str) else None,
        system=system if isinstance(system, dict) and system else None,
        software=software if isinstance(software, dict) and software else None,
    )


@router.get("/{station_id}/weather/history", response_model=list[WeatherPoint])
def weather_history(
    station_id: uuid.UUID,
    hours: int = 12,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[WeatherPoint]:
    """Weather over a window, for the trend charts. The twin of `power_history`
    — same capability, same windows, same thinning."""
    from datetime import UTC, datetime, timedelta

    from backend.auth.authorization import capabilities_for
    from backend.auth.capabilities import Capability
    from backend.database.models.weather_sample import WeatherSample

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
        select(
            WeatherSample.at,
            WeatherSample.temperature_c,
            WeatherSample.humidity_pct,
            WeatherSample.pressure_hpa,
            WeatherSample.wind_kt,
        )
        .where(
            WeatherSample.ground_station_id == station_id,
            WeatherSample.at >= since,
        )
        .order_by(WeatherSample.at)
    ).all()

    limit = 400
    if len(rows) > limit:
        step = len(rows) / limit
        picked = [rows[min(len(rows) - 1, int(i * step))] for i in range(limit)]
        picked[-1] = rows[-1]
        rows = picked

    return [
        WeatherPoint(
            t=r.at.isoformat(),
            temp=r.temperature_c,
            humidity=r.humidity_pct,
            pressure=r.pressure_hpa,
            wind=r.wind_kt,
        )
        for r in rows
    ]


@router.get(
    "/{station_id}/radio/transcripts", response_model=list[TranscriptPoint]
)
def radio_transcripts(
    station_id: uuid.UUID,
    limit: int = 100,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[TranscriptPoint]:
    """The station's airband transcriptions, newest first.

    These are `radio.transmission` events — the ledger the station writes when
    on-box transcription is enabled — read straight from the event table rather
    than a downsample, because a transmission has no newer version to thin to.
    Behind telemetry.view, the same as the rest of the radio.
    """
    from backend.auth.authorization import capabilities_for
    from backend.auth.capabilities import Capability
    from backend.database.models.station_event import StationEvent

    granted = capabilities_for(
        db,
        user_id=identity.user_id,
        organization_id=identity.organization_id,
        ground_station_id=station_id,
    )
    if Capability.TELEMETRY_VIEW not in granted:
        raise HTTPException(status_code=404, detail="Station not available")

    capped = max(1, min(500, limit))
    rows = db.execute(
        select(StationEvent.at, StationEvent.clock, StationEvent.message)
        .where(
            StationEvent.ground_station_id == station_id,
            StationEvent.type == "radio.transmission",
        )
        .order_by(StationEvent.received_at.desc())
        .limit(capped)
    ).all()

    return [
        TranscriptPoint(t=r.at.isoformat(), clock=r.clock, message=r.message or "")
        for r in rows
    ]


#: Slots the console renders. A stored list longer than this is truncated rather
#: than refused — the console has changed this number once already, and an
#: operator should not get an error because an older build saved five.
PRESET_SLOTS = 4
#: Airband, matching the receiver's own limits (api/commands.py).
PRESET_MIN_HZ = 108_000_000
PRESET_MAX_HZ = 137_000_000


class RadioPreset(BaseModel):
    hz: int = Field(ge=PRESET_MIN_HZ, le=PRESET_MAX_HZ)
    #: Short by design — the console's preset buttons ellipsise anything longer.
    name: str = Field(default="", max_length=12)


@router.get("/{station_id}/radio/presets", response_model=list[RadioPreset | None])
def get_radio_presets(
    station_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[RadioPreset | None]:
    """This station's airband presets, shared by the whole organisation.

    Behind radio.listen: presets describe what is worth listening to at this
    site, so anyone who may listen may see them. A slot nobody has set is null,
    and the list is always PRESET_SLOTS long so the console can index it
    directly.
    """
    from backend.auth.capabilities import Capability

    granted = capabilities_for(
        db,
        user_id=identity.user_id,
        organization_id=identity.organization_id,
        ground_station_id=station_id,
    )
    if Capability.RADIO_LISTEN not in granted:
        raise HTTPException(status_code=404, detail="Station not available")

    station = db.get(GroundStation, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not available")
    return _presets_out(station.radio_presets)


@router.put("/{station_id}/radio/presets", response_model=list[RadioPreset | None])
def set_radio_presets(
    station_id: uuid.UUID,
    body: list[RadioPreset | None],
    request: Request,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[RadioPreset | None]:
    """Replace this station's presets. Behind radio.control.

    The whole list is sent, not one slot: it is four entries the console already
    holds, and a read-modify-write of the lot avoids two operators racing on
    different slots and one of them losing an edit to a stale index.

    radio.control rather than config.write — saving where you just tuned is the
    same act as tuning, and it is already an actuator capability a viewer can
    never hold. That these are org-wide is exactly the point: an operator naming
    the tower frequency once names it for everybody.
    """
    from backend.auth.capabilities import Capability

    granted = capabilities_for(
        db,
        user_id=identity.user_id,
        organization_id=identity.organization_id,
        ground_station_id=station_id,
    )
    if Capability.RADIO_LISTEN not in granted:
        raise HTTPException(status_code=404, detail="Station not available")
    if Capability.RADIO_CONTROL not in granted:
        raise HTTPException(status_code=403, detail="Not allowed to change presets")

    station = db.get(GroundStation, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not available")

    trimmed = list(body)[:PRESET_SLOTS]
    station.radio_presets = [
        None if p is None else {"hz": p.hz, "name": p.name} for p in trimmed
    ]
    # Read back before the commit: the org context is SET LOCAL and lasts one
    # transaction, so touching the row afterwards reads with no org set.
    result = _presets_out(station.radio_presets)
    db.commit()

    record(
        action="station.radio_presets.updated",
        organization_id=identity.organization_id,
        actor_user_id=identity.user_id,
        target_type="ground_station",
        target_id=str(station_id),
        ground_station_id=station_id,
        ip_address=request.client.host if request.client else None,
        detail={"set": sum(1 for p in result if p is not None)},
    )
    return result


def _presets_out(stored: list | None) -> list[RadioPreset | None]:
    """Stored JSON to exactly PRESET_SLOTS entries.

    Tolerant on the way out: anything unparseable becomes an empty slot rather
    than a 500. This column is written only by the endpoint above, but it is
    JSON in a database and the console indexes the result directly.
    """
    out: list[RadioPreset | None] = []
    values = stored if isinstance(stored, list) else []
    for i in range(PRESET_SLOTS):
        value = values[i] if i < len(values) else None
        if isinstance(value, dict) and isinstance(value.get("hz"), int):
            hz = value["hz"]
            if PRESET_MIN_HZ <= hz <= PRESET_MAX_HZ:
                name = value.get("name")
                out.append(
                    RadioPreset(hz=hz, name=name[:12] if isinstance(name, str) else "")
                )
                continue
        out.append(None)
    return out


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
