"""A recent still from each station's camera, for the wall's tiles.

THE WHOLE POINT IS THAT THIS IS NOT VIDEO. A live stream is ~2.6 Mbit/s through
the in-process relay in `api/media.py`, which is what binds this deployment to a
single worker; twenty-four of them is not a feature, it is a different system. A
scaled JPEG once a minute is about 2.75 kbit/s — roughly an eighth of what
squelch-gated Opus costs while somebody is speaking — and it goes in Redis, so
ANY worker can serve it. This is the only picture path in the platform that does
not pin the deployment.

TWO SIDES, TWO KINDS OF CALLER:

  POST /media/poster   the STATION, with its own credential. No user session.
  GET  /api/odin/...   an OPERATOR, behind the cross-tenant read ceiling.

Both are here because they are two halves of one thing and splitting them across
files would mean the cache key's shape lived in two places.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from backend.auth.identity import Identity
from backend.auth.odin import odin_capabilities_for
from backend.auth.platform import require_odin_watch
from backend.database.dependencies import get_db
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.bus import (
    POSTER_TTL,
    poster_key,
    poster_stamp_key,
    read_latest_sync,
    write_poster_sync,
)
from backend.services import enrolment

router = APIRouter(tags=["media"])


def _resolve_station(secret: str):
    """The credential lookup, in a shape a worker thread can run.

    Its own function because it opens and closes a session: handing
    `asyncio.to_thread` a lambda over a session built on the event loop would
    put the checkin on the wrong thread.
    """
    with PrivilegedSessionLocal() as db:
        return enrolment.resolve(db, secret=secret)


def _store(station_id: uuid.UUID, jpeg: bytes) -> bool:
    """Picture and stamp, together, on one worker thread.

    The stamp is written only if the picture was, and never the other way
    round: a stamp with no image behind it is a tile that fetches a 404 once a
    minute and shows nothing, which is worse than a tile that knows it has no
    picture.
    """
    if not write_poster_sync(poster_key(station_id), jpeg):
        return False
    write_poster_sync(
        poster_stamp_key(station_id),
        datetime.now(UTC).isoformat(timespec="seconds").encode(),
        ttl=POSTER_TTL,
    )
    return True

#: Refused above this. The station bounds it too, at the same figure — this is
#: the half that protects the platform from a station that has not been updated
#: or is not ours. A 480x270 JPEG is ~20 kB.
MAX_POSTER_BYTES = 256 * 1024


@router.post("/media/poster", status_code=204)
async def put_poster(request: Request) -> Response:
    """One still from one station.

    AUTHENTICATED WITH THE STATION CREDENTIAL, and the station id is DERIVED
    from it rather than sent — the same rule as the media socket. A box holding
    a valid secret still cannot post a picture as a different station, because
    it never says which station it is.

    Resolved through `enrolment.resolve`, NOT `authenticate`: the latter stamps
    `last_used_at`, which is right once at the start of a long-lived socket and
    wrong on a call that repeats every sixty seconds from every watched station.
    That would be an UPDATE and a COMMIT on one hot row per station, for ever,
    to record something no more true than the last one.
    """
    auth = request.headers.get("authorization", "")
    secret = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not secret:
        raise HTTPException(status_code=401, detail="station credential required")

    # **The declared length is refused before a byte is read.**
    #
    # `request.body()` buffers the WHOLE body into this worker's memory, so
    # checking the size after reading it is checking the lock after the door.
    # An unauthenticated caller — anyone who can reach the endpoint, since the
    # credential has not been verified yet — could name a gigabyte and have us
    # hold it. Content-Length can lie, but a lie in the direction of "small"
    # is caught by the real check below, and a lie in the direction of "large"
    # costs the caller the connection.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_POSTER_BYTES:
                raise HTTPException(status_code=413, detail="poster too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="bad content-length") from None

    # **Authenticated BEFORE the body is read**, so an anonymous caller cannot
    # make this worker buffer anything at all. The DB round trip is one indexed
    # lookup and it is the cheaper of the two things being ordered here.
    #
    # Off the event loop: this is an `async def`, so a synchronous DB call would
    # block the loop that carries the whole fleet's ingest — and this path runs
    # once a minute per watched station, which is exactly often enough to
    # matter.
    station = await asyncio.to_thread(_resolve_station, secret)
    if station is None:
        # 401 whether the secret is unknown, revoked, expired, or belongs to a
        # deactivated station. Telling those apart would let somebody probe
        # which is which.
        raise HTTPException(status_code=401, detail="station credential required")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    if len(body) > MAX_POSTER_BYTES:
        # The real check. Content-Length is the caller's claim; this is the
        # measurement, and a chunked upload never made the claim at all.
        raise HTTPException(status_code=413, detail="poster too large")

    # Both writes on one worker thread. `write_poster_sync` is a blocking
    # round trip to Redis and this is an `async def`; two of them per station
    # per minute on the loop that carries the fleet's ingest is a stall nobody
    # would attribute to a picture.
    #
    # The stamp is the PLATFORM's clock, not the station's header. A station
    # with a wrong clock (no RTC, no NTP yet — this fleet boots offline) would
    # otherwise emit a stamp from 1970 or from next year, and the tile's `?v=`
    # would either never change or never let a later frame look newer. What the
    # stamp is for is cache-busting and staleness, and both want a monotone
    # clock we control. `X-Captured-At` is still logged by the station itself.
    stored = await asyncio.to_thread(_store, station.id, body)
    if not stored:
        # Redis down. Say so rather than 204 — the station's own log is the only
        # place anybody will ever notice, and a silent success would have it
        # cheerfully uploading into nothing for as long as the outage lasts.
        raise HTTPException(status_code=503, detail="poster store unavailable")
    return Response(status_code=204)


@router.get("/api/odin/stations/{station_id}/poster")
def get_poster(
    station_id: uuid.UUID,
    identity: Identity = Depends(require_odin_watch),
    db: Session = Depends(get_db),
) -> Response:
    """The station's most recent still.

    NO `.jpg` ON THE PATH, and `private` on the response. `Cache-Control:
    no-cache` PERMITS a shared cache to store the body — it only requires
    revalidation before reuse — and this deployment sits behind a tunnel where
    `.jpg` is exactly the suffix an edge special-cases by extension. A
    customer's camera frame resident in an intermediary they never agreed to is
    not a thing to discover later. `api/tiles.py` already reasons about this for
    basemap tiles, which are a far weaker secret than this.

    Re-checked against the ODIN ceiling per request, not trusted from the
    roster: deactivating a station is how a tenant stops being watched, and the
    wall's roster is up to thirty seconds old.
    """
    if not odin_capabilities_for(db, station_id=station_id):
        # 404, not 403 — the same rule the rest of the platform follows, so a
        # caller cannot learn that a station exists by being refused it.
        raise HTTPException(status_code=404, detail="Station not available")

    # Empty list, not a list of None, when Redis itself is unreachable — the
    # helper fails soft by design, so indexing it blind would turn an outage
    # into a 500 on every tile at once.
    found = read_latest_sync([poster_key(station_id)])
    jpeg = found[0] if found else None
    if not jpeg:
        raise HTTPException(status_code=404, detail="No recent picture")

    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            # `private` keeps it out of shared caches; `no-store` keeps it out
            # of disk caches on the viewer's machine too. The client busts its
            # own cache with ?v=<poster_at> from the digest, so nothing is lost
            # by refusing to cache here.
            "Cache-Control": "private, no-store",
            "Vary": "Cookie",
        },
    )
