"""Turning what stations say into alerts somebody can work.

Three inputs, and they are deliberately different shapes:

  CONDITIONS are a state. The station publishes the set of things currently
  wrong with it in every health frame, and the interesting thing is the DIFF
  between one frame and the last: a condition appearing is a fault starting, a
  condition disappearing is a fault ending. This is the largest coverage gain in
  the whole feature — nineteen named conditions that until now existed only
  inside a live telemetry frame and reached the vendor nowhere at all.
  `credential.renewal_failing` could be true for six hours and leave no
  queryable trace anywhere.

  EVENTS are occurrences. They arrive in batches, at-least-once, and each one
  says something happened rather than something is.

  SILENCE is inferred. Nobody reports that a station has gone dark; the platform
  notices the absence.

THE DIFF IS TAKEN AGAINST THE PREVIOUS CACHED FRAME, not against the database.
At two hundred stations, health frames land around seven a second, and a SELECT
per frame to ask "was this condition already open" would put a database round
trip on the single-leader ingest loop for every one of them. The previous frame
is already in Redis, about to be overwritten, and reading it costs nothing
because the write was going to happen anyway.

Everything here is synchronous and takes a Session. It is called from inside
work that is already on a worker thread with a transaction open, so it joins
that transaction rather than opening its own — which also means an alert and the
event that caused it commit together or not at all.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from backend.services.alerts import rules, store

log = logging.getLogger(__name__)

#: A condition's severity as the station states it, mapped onto the contract's
#: three levels. The station uses the same vocabulary, so this is mostly
#: identity — it exists to make an unexpected word land somewhere sensible
#: rather than propagate into a column with a check on it.
_SEVERITY = {
    "critical": "critical",
    "failing": "critical",
    "warning": "warning",
    "degraded": "warning",
    "info": "info",
}


def _conditions(frame: dict | None) -> dict[str, dict]:
    """The conditions in a health frame, by id."""
    if not isinstance(frame, dict):
        return {}
    raw = frame.get("conditions")
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict] = {}
    for c in raw:
        if isinstance(c, dict) and isinstance(c.get("id"), str):
            out[c["id"]] = c
    return out


def on_health(
    db: Session,
    *,
    organization_id: uuid.UUID,
    station_id: uuid.UUID,
    previous: dict | None,
    current: dict,
    now: datetime | None = None,
) -> None:
    """Raise on the appearing edge, close on the disappearing edge.

    Mirrors the console's own `{station}:{condition.id}` raise/forget semantics,
    which until now lived only in one browser tab, and makes them durable.
    """
    moment = now or datetime.now(UTC)
    before = _conditions(previous)
    after = _conditions(current)

    for cid, condition in after.items():
        if cid in before:
            # Still true. The health frame arrives every thirty seconds and
            # bumping an occurrence count on each one would turn "how many times
            # has this happened" into "how long has this been true", which the
            # timestamps already answer.
            continue
        severity = _SEVERITY.get(str(condition.get("severity", "")), "warning")
        detail = condition.get("detail") or None
        store.open_or_touch(
            db,
            organization_id=organization_id,
            station_id=station_id,
            source="condition",
            dedupe_key=cid,
            type=cid,
            severity=severity,
            title=cid.replace(".", " ").replace("_", " "),
            message=detail if isinstance(detail, str) else None,
            now=moment,
        )

    for cid in before.keys() - after.keys():
        # The station stopped reporting it, so it stopped being true. This is
        # the ONLY way a condition-owned alert closes itself, which is why the
        # paired events are never allowed to raise one.
        store.auto_close(db, station_id=station_id, dedupe_key=cid, now=moment)


def on_events(
    db: Session,
    *,
    organization_id: uuid.UUID,
    station_id: uuid.UUID,
    events: list[dict],
    now: datetime | None = None,
) -> None:
    """Apply the disposition rules to a batch of station events."""
    moment = now or datetime.now(UTC)
    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if not isinstance(etype, str):
            continue
        severity = str(event.get("severity") or "info")
        message = event.get("message")

        # A recovery closes its fault and does nothing else. These are all
        # severity 'info', so nothing below would ever have looked at them.
        recovered = rules.closes(etype)
        if recovered:
            store.auto_close(
                db, station_id=station_id, dedupe_key=recovered, now=moment
            )
            continue

        how = rules.disposition(etype, severity)
        if how is rules.Disposition.NEVER:
            continue
        if how is rules.Disposition.CONDITION_OWNED:
            # Evidence only. The condition owns whether this is open.
            store.touch_only(
                db, station_id=station_id, dedupe_key=etype, now=moment
            )
            continue

        store.open_or_touch(
            db,
            organization_id=organization_id,
            station_id=station_id,
            source="event",
            dedupe_key=etype,
            type=etype,
            severity=severity if severity in ("warning", "critical") else "warning",
            title=etype.replace(".", " ").replace("_", " "),
            message=message if isinstance(message, str) else None,
            now=moment,
        )


def on_dark(
    db: Session,
    *,
    organization_id: uuid.UUID,
    station_id: uuid.UUID,
    station_name: str,
    minutes: int,
    now: datetime | None = None,
) -> None:
    """A station has stopped talking for long enough to matter.

    Replaces an in-process `alerted` set in services/station_watch.py, which had
    a confirmed bug: the set lived in memory, so every deploy re-announced every
    currently-dark station as though it had just gone dark. On a platform that is
    redeployed several times in an afternoon, that is the alarm that taught
    everyone to ignore alarms.
    """
    store.open_or_touch(
        db,
        organization_id=organization_id,
        station_id=station_id,
        source="dark",
        dedupe_key="platform.station.dark",
        type="platform.station.dark",
        severity="critical",
        title="Station dark",
        message=f"{station_name} has not been heard from for {minutes} minutes.",
        now=now,
    )


def on_heard_again(
    db: Session, *, station_id: uuid.UUID, now: datetime | None = None
) -> None:
    """It came back. Closes the dark alert and nothing else."""
    store.auto_close(
        db, station_id=station_id, dedupe_key="platform.station.dark", now=now
    )
