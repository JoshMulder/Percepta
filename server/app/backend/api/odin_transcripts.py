"""The watch's transcript companion: what was said, across guarded channels.

THIS SESSION CROSSES TENANT BOUNDARIES — the same rule as api/platform.py,
api/odin.py and api/odin_alerts.py. The query below scopes itself in code, to a
station list the caller names, and that is the only thing keeping it honest.

Why polled REST rather than a stream on the watch socket. A transcript is a
station-side artefact: the agent transcribes an over AFTER it ends, writes a
`radio.transmission` event, and that event travels the ordinary telemetry path
on its own schedule — seconds behind the audio, sometimes much more on a busy
box. It is not synchronous with the sound, so pretending it is by pushing it
down the audio socket would build a timing relationship that does not exist and
cannot be relied on. Polling states the truth plainly: this is the record of
what was said, and it arrives when it arrives.

It is also the surface an operator uses to catch up on a channel they were NOT
listening to, which is not a live concern at all.

READ-ONLY. There is no Odin write path to a transcript, and there should not be:
an operator who could edit a customer's record of what went over the air would
be able to alter evidence about an incident they were involved in.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.auth.platform import require_odin_watch
from backend.database.dependencies import get_db
from backend.database.models.ground_station import GroundStation
from backend.database.models.station_event import StationEvent

router = APIRouter(prefix="/api/odin", tags=["odin"])

#: Rows returned at most, whatever is asked for. A watch strip shows a scrolling
#: recent history, not an archive: the station's own
#: `/stations/{id}/radio/transcripts` is where a long look belongs, and it is
#: behind that tenant's own authorisation where it should be.
MAX_ROWS = 300

#: Stations one request may span. Matches realtime/hub.WATCH_MAX — the feed
#: exists to accompany a guard set, and a request naming more stations than an
#: operator can guard is not a transcript feed, it is a fleet-wide export.
MAX_STATIONS = 8


class OdinTranscript(BaseModel):
    ground_station_id: str
    #: When the transmission happened, by the station's clock.
    t: str
    #: The station's own wall clock rendering, kept because an operator reading a
    #: transcript alongside a recording compares against what the station said,
    #: not against what the server inferred.
    clock: str | None
    message: str


@router.get("/transcripts", response_model=list[OdinTranscript])
def odin_transcripts(
    stations: str = Query(
        ..., description="Comma-separated ground station uuids, at most 8."
    ),
    limit: int = 100,
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> list[OdinTranscript]:
    """Recent transmissions across the guarded stations, newest first.

    ONE query across the whole set, not one per station. A per-station loop would
    also have to merge and re-sort in Python to get a single time-ordered feed,
    which is the database's job and is where an off-by-one in the merge would
    silently drop somebody's over.

    ACTIVE STATIONS ONLY. Deactivating a station is how a tenant stops being
    watched, and it has to stop the record of what was said just as it stops the
    sound — otherwise the loudest half of the watch would keep working after the
    lever was pulled.
    """
    try:
        wanted = [uuid.UUID(s) for s in stations.split(",") if s.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="stations must be uuids")
    if not wanted:
        return []
    if len(wanted) > MAX_STATIONS:
        raise HTTPException(
            status_code=400, detail=f"at most {MAX_STATIONS} stations"
        )

    live = set(
        db.execute(
            select(GroundStation.id).where(
                GroundStation.id.in_(wanted),
                GroundStation.is_active.is_(True),
            )
        ).scalars()
    )
    if not live:
        # Deliberately an empty list, not a 404. Which of the named stations
        # exists is not this endpoint's to disclose, and an operator whose guard
        # set has just been revoked should see the feed go quiet rather than see
        # an error they cannot act on.
        return []

    capped = max(1, min(MAX_ROWS, limit))
    rows = db.execute(
        select(
            StationEvent.ground_station_id,
            StationEvent.at,
            StationEvent.clock,
            StationEvent.message,
        )
        .where(
            StationEvent.ground_station_id.in_(live),
            StationEvent.type == "radio.transmission",
        )
        .order_by(StationEvent.received_at.desc())
        .limit(capped)
    ).all()

    return [
        OdinTranscript(
            ground_station_id=str(r.ground_station_id),
            t=r.at.isoformat(),
            clock=r.clock,
            message=r.message or "",
        )
        for r in rows
    ]
