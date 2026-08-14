"""The event browser the ledger has never had.

THIS SESSION CROSSES TENANT BOUNDARIES — the same rule as api/platform.py,
api/odin.py, api/odin_alerts.py and api/odin_transcripts.py. Row-level security
is bypassed for the life of an ODIN session, so every scope below is written by
hand and there is no database backstop under it.

Three read paths already exist over `station_events` and not one of them is
general: the tenant's transcript list (`api/stations.py`) fixes the type, the
fleet attention feed (`api/platform.py`) fixes the severity and takes the newest
twenty, and the watch's transcript companion (`api/odin_transcripts.py`) fixes
the type again. All three are a bounded LIMIT with a hard-coded predicate, which
is right for what each of them is for and useless for the question an operator
actually asks after something goes wrong: *show me everything this station said
in the last 48 hours*.

THE CURSOR IS A TUPLE, AND IT HAS TO BE. `received_at` is stamped once per
arriving BATCH of up to a hundred events (`services/station_events.py`), so a
station that batches produces a hundred rows sharing one timestamp to the
microsecond. A cursor of `received_at < last_seen` then skips every other row in
that tie, and a cursor of `<=` repeats them for ever. `(received_at, id)` is
unique because `id` is, so the pair totally orders the table and neither happens.
This is the sort of defect that never shows up on a bench station sending one
event at a time and appears the moment a real site reconnects with a backlog.

NEVER VALIDATED AGAINST A LIST OF TYPES. The event vocabulary belongs to the
stations, not to this file: a station running a newer agent emits types this
server has never heard of, and rejecting them would make the browser blind to
exactly the events worth browsing. Unknown types simply match nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.platform import NOT_A_FAULT
from backend.auth.identity import Identity
from backend.auth.platform import require_odin_watch
from backend.database.dependencies import get_db
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.station_event import StationEvent

router = APIRouter(prefix="/api/odin", tags=["odin"])

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

#: How far back an unbounded query is allowed to reach.
#:
#: There is always a floor. A filter on a RARE type with no lower bound is a
#: sequential scan of a table dominated by transcripts and video-lifecycle rows —
#: it returns four rows and reads millions. The operator can ask for more by
#: naming a `since`; what they cannot do is ask for all of history by accident.
DEFAULT_WINDOW = timedelta(days=7)


class OdinEvent(BaseModel):
    id: str
    organization_id: str
    organization_name: str
    ground_station_id: str
    station_name: str
    type: str
    severity: str
    message: str | None
    #: When it happened, by the station's clock.
    at: str
    #: The station's own wall-clock quality flag. Carried because an event's
    #: timestamp is only worth as much as the clock that produced it, and a
    #: station that has never had time sync says so here.
    clock: str | None
    #: When the platform received it. This is what the cursor orders on, so it is
    #: returned: a client that sorts on `at` instead would appear to page
    #: backwards through a station that was offline and then flushed a backlog.
    received_at: str


class OdinEventPage(BaseModel):
    events: list[OdinEvent]
    #: Opaque. Pass back as `cursor` for the next page. Null at the end.
    next_cursor: str | None
    has_more: bool


def _encode(received_at: datetime, row_id: uuid.UUID) -> str:
    return f"{received_at.isoformat()}|{row_id}"


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    when, _, row_id = cursor.partition("|")
    return datetime.fromisoformat(when), uuid.UUID(row_id)


@router.get("/events", response_model=OdinEventPage)
def odin_events(
    station_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    type: str | None = Query(
        None, description="Exact event type, e.g. uplink.down. Not a prefix."
    ),
    severity: str | None = Query(None, description="info | warning | critical"),
    since: datetime | None = None,
    until: datetime | None = None,
    exclude_noise: bool = Query(
        False,
        description=(
            "Drop adsb.proximity and radio.transmission — the two high-volume "
            "types that are warnings to a tenant but not faults in the fleet."
        ),
    ),
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> OdinEventPage:
    """Events across the fleet, newest first, filtered and paged.

    ACTIVE STATIONS AND ACTIVE ORGANISATIONS ONLY, joined rather than filtered
    afterwards. Deactivating a station is how a tenant stops being watched, and
    an event browser that still answered for a deactivated station would be a way
    round the stop lever that the rest of ODIN respects — a quieter one than the
    audio, and therefore easier to leave broken.

    `exclude_noise` is OPT-IN, never the default. `adsb.proximity` really is a
    warning to the tenant watching their own airspace; it is simply not a fault
    in fleet health. A browser that hid it by default would answer "show me
    everything" with a filtered subset and say nothing about it.
    """
    capped = max(1, min(MAX_LIMIT, limit))
    floor = since or (datetime.now(UTC) - DEFAULT_WINDOW)

    statement = (
        select(
            StationEvent.id,
            StationEvent.organization_id,
            Organization.name.label("organization_name"),
            StationEvent.ground_station_id,
            GroundStation.name.label("station_name"),
            StationEvent.type,
            StationEvent.severity,
            StationEvent.message,
            StationEvent.at,
            StationEvent.clock,
            StationEvent.received_at,
        )
        .join(GroundStation, GroundStation.id == StationEvent.ground_station_id)
        .join(Organization, Organization.id == StationEvent.organization_id)
        .where(
            GroundStation.is_active.is_(True),
            Organization.is_active.is_(True),
            StationEvent.received_at >= floor,
        )
    )

    if station_id is not None:
        statement = statement.where(StationEvent.ground_station_id == station_id)
    if organization_id is not None:
        statement = statement.where(StationEvent.organization_id == organization_id)
    if type is not None:
        statement = statement.where(StationEvent.type == type)
    if severity is not None:
        statement = statement.where(StationEvent.severity == severity)
    if until is not None:
        statement = statement.where(StationEvent.received_at <= until)
    if exclude_noise:
        statement = statement.where(StationEvent.type.not_in(NOT_A_FAULT))

    if cursor:
        try:
            cursor_at, cursor_id = _decode(cursor)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="bad cursor")
        # The tuple comparison, and the reason this endpoint has a compound
        # cursor at all. SQLAlchemy renders this as a row-value comparison,
        # which Postgres can drive from an index on (received_at, id) and which
        # is exactly "strictly older than the last row I showed".
        statement = statement.where(
            (StationEvent.received_at, StationEvent.id) < (cursor_at, cursor_id)
        )

    # Ordered by the same tuple the cursor compares on. An ORDER BY that did not
    # match the cursor would page through a different sequence than it claimed to.
    statement = statement.order_by(
        StationEvent.received_at.desc(), StationEvent.id.desc()
    ).limit(capped + 1)

    rows = db.execute(statement).all()
    # One extra row asked for, one extra row dropped: that is how `has_more` is
    # answered without a COUNT(*) over a table this size.
    has_more = len(rows) > capped
    rows = rows[:capped]

    events = [
        OdinEvent(
            id=str(r.id),
            organization_id=str(r.organization_id),
            organization_name=r.organization_name,
            ground_station_id=str(r.ground_station_id),
            station_name=r.station_name,
            type=r.type,
            severity=r.severity,
            message=r.message,
            at=r.at.isoformat(),
            clock=r.clock,
            received_at=r.received_at.isoformat(),
        )
        for r in rows
    ]
    next_cursor = (
        _encode(rows[-1].received_at, rows[-1].id) if rows and has_more else None
    )
    return OdinEventPage(
        events=events, next_cursor=next_cursor, has_more=has_more
    )
