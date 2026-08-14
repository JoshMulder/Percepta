"""Reading and working alerts from the wall.

THIS SESSION CROSSES TENANT BOUNDARIES — the same rule as api/platform.py and
api/odin.py. Every query here scopes itself in code, and every mutation writes an
audit row against the STATION's organisation with the operator as the actor,
because that is what is actually happening: somebody from the vendor is changing
a record that belongs to a customer, and the customer's own audit trail is where
that belongs.

Kept separate from api/odin.py, which is the websocket. A socket and a REST
surface have different failure modes, different auth mechanics (a dependency
cannot run on a websocket route) and different reasons to change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.auth.platform import require_odin_watch
from backend.database.dependencies import get_db
from backend.database.models.ground_station import GroundStation
from backend.database.models.platform_alert import StationMaintenance
from backend.services.alerts import store
from backend.services.audit import record

router = APIRouter(prefix="/api/odin", tags=["odin"])


class AlertOut(BaseModel):
    id: str
    organization_id: str
    ground_station_id: str
    source: str
    type: str
    severity: str
    title: str
    message: str | None
    first_seen_at: str
    last_seen_at: str
    occurrences: int
    state: str
    acked_by_user_id: str | None
    acked_at: str | None
    snooze_until: str | None


def _out(alert) -> AlertOut:
    return AlertOut(
        id=str(alert.id),
        organization_id=str(alert.organization_id),
        ground_station_id=str(alert.ground_station_id),
        source=alert.source,
        type=alert.type,
        severity=alert.severity,
        title=alert.title,
        message=alert.message,
        first_seen_at=alert.first_seen_at.isoformat(),
        last_seen_at=alert.last_seen_at.isoformat(),
        occurrences=alert.occurrences,
        state=alert.state,
        acked_by_user_id=(
            str(alert.acked_by_user_id) if alert.acked_by_user_id else None
        ),
        acked_at=alert.acked_at.isoformat() if alert.acked_at else None,
        snooze_until=(
            alert.snooze_until.isoformat() if alert.snooze_until else None
        ),
    )


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    severity: str | None = None,
    station_id: uuid.UUID | None = None,
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> list[AlertOut]:
    """Still-open alerts, worst first and then oldest first."""
    severities = tuple(s for s in (severity or "").split(",") if s) or None
    return [
        _out(a)
        for a in store.open_alerts(db, severities=severities, station_id=station_id)
    ]


class AckBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def ack_alert(
    alert_id: uuid.UUID,
    body: AckBody,
    request: Request,
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> AlertOut:
    """Take ownership of an alert.

    409 when somebody else already holds it, naming them. The client must NOT
    retry that: it is not a transient failure, it is the answer. The right
    response is to re-render with the current owner shown, so the second
    operator learns who is on it rather than watching a button do nothing.
    """
    try:
        alert = store.ack(
            db, alert_id=alert_id, user_id=identity.user_id, note=body.note
        )
    except store.AlreadyHeld as held:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_acknowledged",
                "acked_by_user_id": str(held.holder_id) if held.holder_id else None,
            },
        ) from held
    except LookupError as missing:
        raise HTTPException(status_code=404, detail="No such alert") from missing

    record(
        action="odin.alert.ack",
        organization_id=alert.organization_id,
        actor_user_id=identity.user_id,
        target_type="platform_alert",
        target_id=str(alert.id),
        ground_station_id=alert.ground_station_id,
        ip_address=request.client.host if request.client else None,
        detail={"type": alert.type, "severity": alert.severity},
    )
    db.commit()
    return _out(alert)


class CloseBody(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


@router.post("/alerts/{alert_id}/close", response_model=AlertOut)
def close_alert(
    alert_id: uuid.UUID,
    body: CloseBody,
    request: Request,
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> AlertOut:
    """Close an alert by hand.

    Deliberately not the same action as ack, and conflating the two is how a
    command centre loses a fault. Ack means "I have seen this and I am on it";
    close means "it stopped being true". A station keeps its attention colour on
    the wall until CLOSED, so acknowledging something never hides a site that is
    still broken.
    """
    alert = store.close(db, alert_id=alert_id, reason="manual")
    if alert is None:
        raise HTTPException(status_code=404, detail="No such alert")
    record(
        action="odin.alert.close",
        organization_id=alert.organization_id,
        actor_user_id=identity.user_id,
        target_type="platform_alert",
        target_id=str(alert.id),
        ground_station_id=alert.ground_station_id,
        ip_address=request.client.host if request.client else None,
        detail={"type": alert.type, "note": body.reason},
    )
    db.commit()
    return _out(alert)


class SnoozeBody(BaseModel):
    minutes: int = Field(ge=5, le=60 * 24)


@router.post("/alerts/{alert_id}/snooze", response_model=AlertOut)
def snooze_alert(
    alert_id: uuid.UUID,
    body: SnoozeBody,
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> AlertOut:
    """Stop this one alert demanding attention, without pretending it is fixed.

    It stays open and stays counted. That is the difference between snoozing a
    fault and closing one, and keeping them separate is what stops a busy night
    quietly becoming a clean board.
    """
    until = datetime.now(UTC) + timedelta(minutes=body.minutes)
    alert = store.snooze(db, alert_id=alert_id, until=until)
    if alert is None:
        raise HTTPException(status_code=404, detail="No such alert")
    db.commit()
    return _out(alert)


class MaintenanceBody(BaseModel):
    minutes: int = Field(ge=5, le=60 * 24 * 14)
    #: Required. A silenced station with no stated reason is indistinguishable
    #: from a forgotten one, and the next shift has no way to judge whether the
    #: silence is still deliberate.
    reason: str = Field(min_length=1, max_length=500)


@router.post("/stations/{station_id}/maintenance", status_code=201)
def declare_maintenance(
    station_id: uuid.UUID,
    body: MaintenanceBody,
    request: Request,
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> dict:
    """Expect this station to misbehave for a while, and stop asking about it.

    Suppression at RAISE time, not display time. A silenced site produces no
    rows at all, so it cannot fill the rail, cannot chime and cannot be counted
    as a fault — and when the window ends, the next occurrence opens a fresh
    alert with an honest first_seen_at rather than one dated to the middle of
    the maintenance.

    This ships with the engine rather than after it. The stated biggest risk of
    the whole feature is alert fatigue, and a week of a known-bad site shouting
    every ninety seconds is long enough to teach an operator to stop reading the
    rail — a habit that does not come back when the suppression does.
    """
    station = db.execute(
        select(GroundStation).where(GroundStation.id == station_id)
    ).scalar_one_or_none()
    if station is None:
        raise HTTPException(status_code=404, detail="No such station")

    now = datetime.now(UTC)
    window = StationMaintenance(
        id=uuid.uuid4(),
        organization_id=station.organization_id,
        ground_station_id=station.id,
        from_at=now,
        until_at=now + timedelta(minutes=body.minutes),
        reason=body.reason,
        created_by_user_id=identity.user_id,
    )
    db.add(window)
    record(
        action="odin.station.maintenance",
        organization_id=station.organization_id,
        actor_user_id=identity.user_id,
        target_type="ground_station",
        target_id=str(station.id),
        ground_station_id=station.id,
        ip_address=request.client.host if request.client else None,
        detail={"minutes": body.minutes, "reason": body.reason},
    )
    db.commit()
    return {"until": window.until_at.isoformat(), "reason": window.reason}
