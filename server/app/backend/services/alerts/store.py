"""Opening, acknowledging and closing an alert. All of it idempotent.

Every function here can be called twice with the same arguments and leave the
same result, because every one of them will be. The station's event queue is
at-least-once by design, the ingest can be restarted mid-frame, and two
operators will click the same button at the same moment on two screens.

ACK IS A CONDITIONAL UPDATE, and that is the whole concurrency design. Two
operators at two desks must not both work the same fault, and the way to
guarantee that is not to check-then-write in the application — that has a race
in it wide enough to drive a shift through. It is

    UPDATE ... SET state='acked', acked_by=:me WHERE id=:id AND state='open'

and if that returns no row, somebody else got there first. The caller then
answers 409 naming the current owner, and the second operator's screen tells
them who has it rather than silently doing nothing. "Nothing is handled twice"
becomes a property of the database rather than a convention in the UI.

ACK IS ALSO ASSIGNMENT. There is no separate assignee and no 'assigned' state:
two concepts where one will do is how a queue stops being read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from backend.database.models.platform_alert import PlatformAlert, StationMaintenance


class AlreadyHeld(Exception):
    """Somebody else acknowledged this first."""

    def __init__(self, holder_id: uuid.UUID | None) -> None:
        super().__init__("This alert is already acknowledged.")
        self.holder_id = holder_id


def under_maintenance(
    db: Session, *, station_id: uuid.UUID, now: datetime | None = None
) -> bool:
    """Whether this station is in a declared maintenance window.

    Checked at RAISE time rather than at display time, deliberately. Suppressing
    on the way in means a silenced site produces no rows at all, so it cannot
    fill the rail, cannot chime, and cannot be counted as a fault — and when the
    window ends, the next occurrence opens a fresh alert with an honest
    first_seen_at rather than one dated to the middle of the maintenance.
    """
    moment = now or datetime.now(UTC)
    found = db.execute(
        select(StationMaintenance.id).where(
            StationMaintenance.ground_station_id == station_id,
            StationMaintenance.from_at <= moment,
            StationMaintenance.until_at > moment,
        ).limit(1)
    ).first()
    return found is not None


def open_or_touch(
    db: Session,
    *,
    organization_id: uuid.UUID,
    station_id: uuid.UUID,
    source: str,
    dedupe_key: str,
    type: str,
    severity: str,
    title: str,
    message: str | None = None,
    now: datetime | None = None,
) -> PlatformAlert | None:
    """Open an alert, or record another occurrence of one already open.

    Returns None when the station is under maintenance — the caller has nothing
    to publish and nothing to chime about.

    The unique partial index does the deduplication, so this cannot produce two
    rows for one fact even under concurrent raises from two processes. The
    SELECT below is the fast path; the index is the guarantee.
    """
    moment = now or datetime.now(UTC)
    if under_maintenance(db, station_id=station_id, now=moment):
        return None

    existing = db.execute(
        select(PlatformAlert).where(
            PlatformAlert.ground_station_id == station_id,
            PlatformAlert.dedupe_key == dedupe_key,
            PlatformAlert.state != "closed",
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.last_seen_at = moment
        existing.occurrences += 1
        # A fault that gets worse re-raises the alert's severity but never
        # lowers it: an alert that has been critical is not made routine by a
        # subsequent warning, and an operator who saw red should not find amber
        # in its place with no explanation.
        if severity == "critical" and existing.severity != "critical":
            existing.severity = "critical"
        db.flush()
        return existing

    alert = PlatformAlert(
        id=uuid.uuid4(),
        organization_id=organization_id,
        ground_station_id=station_id,
        source=source,
        dedupe_key=dedupe_key,
        type=type,
        severity=severity,
        title=title,
        message=message,
        first_seen_at=moment,
        last_seen_at=moment,
        occurrences=1,
        state="open",
    )
    db.add(alert)
    db.flush()
    return alert


def touch_only(
    db: Session, *, station_id: uuid.UUID, dedupe_key: str, now: datetime | None = None
) -> PlatformAlert | None:
    """Record an occurrence against an alert that already exists, opening nothing.

    This is what a CONDITION_OWNED event does: the paired event is evidence that
    the fault is still happening, and it belongs on the alert's timeline, but the
    condition is the only thing allowed to open or close it.
    """
    existing = db.execute(
        select(PlatformAlert).where(
            PlatformAlert.ground_station_id == station_id,
            PlatformAlert.dedupe_key == dedupe_key,
            PlatformAlert.state != "closed",
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    existing.last_seen_at = now or datetime.now(UTC)
    existing.occurrences += 1
    db.flush()
    return existing


def ack(
    db: Session, *, alert_id: uuid.UUID, user_id: uuid.UUID, note: str | None = None
) -> PlatformAlert:
    """Take ownership. Raises AlreadyHeld if somebody else has it."""
    now = datetime.now(UTC)
    result = db.execute(
        update(PlatformAlert)
        .where(PlatformAlert.id == alert_id, PlatformAlert.state == "open")
        .values(state="acked", acked_by_user_id=user_id, acked_at=now, ack_note=note)
        .returning(PlatformAlert.id)
    ).first()
    if result is None:
        current = db.get(PlatformAlert, alert_id)
        if current is None:
            raise LookupError("No such alert")
        # Re-acking your own is a no-op rather than an error: a double click on
        # a slow link should not read as a conflict.
        if current.state == "acked" and current.acked_by_user_id == user_id:
            return current
        raise AlreadyHeld(current.acked_by_user_id)
    db.flush()
    return db.get(PlatformAlert, alert_id)


def close(
    db: Session,
    *,
    alert_id: uuid.UUID,
    reason: str,
    now: datetime | None = None,
) -> PlatformAlert | None:
    """Close an alert. Idempotent: closing a closed alert changes nothing."""
    alert = db.get(PlatformAlert, alert_id)
    if alert is None or alert.state == "closed":
        return alert
    alert.state = "closed"
    alert.closed_at = now or datetime.now(UTC)
    alert.closed_reason = reason
    db.flush()
    return alert


def auto_close(
    db: Session,
    *,
    station_id: uuid.UUID,
    dedupe_key: str,
    now: datetime | None = None,
) -> PlatformAlert | None:
    """Close because the underlying fact stopped being true.

    Distinguished from a manual close by `closed_reason`, which is worth keeping:
    a fault that never self-resolves is a different maintenance question from one
    that clears every night.
    """
    alert = db.execute(
        select(PlatformAlert).where(
            PlatformAlert.ground_station_id == station_id,
            PlatformAlert.dedupe_key == dedupe_key,
            PlatformAlert.state != "closed",
        )
    ).scalar_one_or_none()
    if alert is None:
        return None
    return close(db, alert_id=alert.id, reason="resolved", now=now)


def snooze(
    db: Session, *, alert_id: uuid.UUID, until: datetime
) -> PlatformAlert | None:
    """Silence one alert until a moment. The alert stays open and stays counted;
    it simply stops demanding attention, which is the difference between snoozing
    a fault and pretending it is fixed."""
    alert = db.get(PlatformAlert, alert_id)
    if alert is None:
        return None
    alert.snooze_until = until
    db.flush()
    return alert


def open_alerts(
    db: Session,
    *,
    severities: tuple[str, ...] | None = None,
    station_id: uuid.UUID | None = None,
    limit: int = 200,
) -> list[PlatformAlert]:
    """The rail's read: still-open alerts, worst first, then oldest first.

    Oldest-first within a severity because a queue ages UPWARD. Newest-first is a
    social feed, and it buries the thing that has been waiting longest — which on
    a wall is precisely the thing most likely to have been forgotten.
    """
    query = select(PlatformAlert).where(PlatformAlert.state != "closed")
    if severities:
        query = query.where(PlatformAlert.severity.in_(severities))
    if station_id is not None:
        query = query.where(PlatformAlert.ground_station_id == station_id)
    # critical before warning before info, then oldest first.
    query = query.order_by(
        text("CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END"),
        PlatformAlert.first_seen_at.asc(),
    ).limit(limit)
    return list(db.execute(query).scalars().all())
