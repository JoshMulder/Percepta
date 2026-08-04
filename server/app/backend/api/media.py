"""Media endpoints: a station pushes in, a viewer pulls out, nothing meets.

Three routes:

    POST /api/stations/{id}/stream-ticket   authorise a viewer, briefly
    WS   /media/ingest                      station → platform, fMP4
    WS   /media/view?ticket=…               platform → browser, fMP4

**Stream tickets exist because the media path makes its own attach decisions.**
The WebSocket that carries video is not the API request that authorised it, and
a browser cannot set headers on a WebSocket - so the authorisation has to travel
in the URL. That is only safe if it is short-lived, single-use and bound to one
station, which is what a ticket is. A session cookie in a query string would be
a session cookie in every proxy log.

`authorize_stream` is the same `capabilities_for` every other path uses, against
the same pinned station. There is no second authorisation model here.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth.authorization import capabilities_for
from backend.auth.capabilities import Capability
from backend.auth.dependencies import require_capability
from backend.auth.identity import Identity
from backend.database.dependencies import get_db
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.bus import command_channel, publish_sync
from backend.core.config import settings
from backend.realtime import media as media_relay
from backend.realtime.media import LEASE_SECONDS, RENEW_SECONDS, relay
from backend.repositories.auth_session_repository import AuthSessionRepository
from backend.services import enrolment

log = logging.getLogger(__name__)
router = APIRouter(tags=["media"])

#: Ticket lifetime. Long enough to open a socket, far too short to keep, share
#: or find useful in a log days later.
TICKET_SECONDS = 60

#: A frame larger than this is dropped and the socket closed (1009). The same
#: rule the relay has always had in `broker.py`, at the size *this* endpoint
#: needs: H.264, where a multi-megabyte fMP4 fragment is ordinary and the
#: largest seen on the bench was a little over three.
#:
#: Uvicorn's `--ws-max-size` is deliberately set *above* this one in
#: `start_app.py`, and the ordering is the entire point. An app-wide limit
#: cannot name the endpoint or the station it closed: when it tripped here the
#: only evidence anywhere was the websockets library's own `PayloadTooBig`
#: text, arriving at the station as a close reason and appearing in no
#: platform log at all. That is how a 512 KiB cap on this socket stayed
#: invisible while no video flowed.
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: Issued tickets, by id. In-process for the same reason the relay is: the
#: socket it authorises must land on this worker anyway.
_tickets: dict[str, dict] = {}


class StreamTicket(BaseModel):
    ticket: str
    expires_in: int
    url: str


def _prune() -> None:
    now = datetime.now(UTC)
    for key in [k for k, v in _tickets.items() if v["expires_at"] <= now]:
        _tickets.pop(key, None)


@router.post(
    "/api/stations/{station_id}/stream-ticket", response_model=StreamTicket
)
def stream_ticket(
    station_id: uuid.UUID,
    identity: Identity = Depends(require_capability(Capability.VIDEO_VIEW)),
    db: Session = Depends(get_db),
) -> StreamTicket:
    """Authorise this user to watch this station, for the next minute.

    Single use: redeemed at attach and discarded, so a ticket that leaks into a
    proxy log or a browser history is already spent.
    """
    _prune()
    ticket = uuid.uuid4().hex
    _tickets[ticket] = {
        "user_id": identity.user_id,
        # Kept so the viewer socket can re-check that the session behind it is
        # still live. Capabilities alone do not cover signing out, a password
        # change, or a session an admin revoked — all of which must stop the
        # picture, and none of which change a grant.
        "session_id": identity.session_id,
        "organization_id": identity.organization_id,
        "station_id": station_id,
        "expires_at": datetime.now(UTC) + timedelta(seconds=TICKET_SECONDS),
    }
    return StreamTicket(
        ticket=ticket,
        expires_in=TICKET_SECONDS,
        url=f"/media/view?ticket={ticket}",
    )


# --- the station side ----------------------------------------------------


@router.websocket("/media/ingest")
async def media_ingest(websocket: WebSocket) -> None:
    """A station pushing its stream in.

    Authenticated with the station credential, not a user session - the same
    secret it uses at the broker, presented as a bearer token. The station id is
    derived from the credential rather than sent: a box holding a valid secret
    still cannot claim to be a different station.
    """
    auth = websocket.headers.get("authorization", "")
    secret = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not secret:
        await websocket.close(code=4401)
        return

    with PrivilegedSessionLocal() as db:
        found = enrolment.authenticate(db, secret=secret)
        if found is None:
            await websocket.close(code=4401)
            return
        station, credential = found
        station_id = station.id
        organization_id = station.organization_id
        credential_id = credential.id
        db.commit()

    await websocket.accept()
    stream = await relay.publisher_connected(station_id, organization_id)
    log.info("Media ingest open for station %s.", station_id)
    # The expensive half of the revocation gap: this socket carries megabits,
    # and authenticating it once at connect meant a decommissioned box kept
    # streaming for as long as the link held. Same watcher as /broker.
    revoked = asyncio.create_task(enrolment.close_when_revoked(
        credential_id, station_id, lambda: websocket.close(code=4401),
    ))

    # Set by a "key" control frame, consumed by the binary fragment it precedes:
    # the station announces a keyframe so the relay can cache from it without
    # reading the media. Cleared after each fragment and on any session reset, so
    # a lost binary cannot leave a stale flag mislabelling a later delta.
    pending_keyframe = False
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            data = message.get("bytes")
            text = message.get("text") or ""

            # One bound, both frame types, before either is used - a control
            # frame is small by nature, so an oversized one is as much a bug
            # upstream as an oversized fragment. Checked after `receive()` has
            # already allocated, exactly as the relay's is: this cannot stop
            # the allocation, only the socket that keeps making them.
            size = len(data) if data is not None else len(text)
            if size > MAX_FRAME_BYTES:
                log.warning(
                    "Station %s sent a %d byte media frame; the cap is %d. "
                    "Closing.", station_id, size, MAX_FRAME_BYTES,
                )
                await websocket.close(code=1009)
                return

            if data is not None:
                # The first binary frame of a session is the initialisation
                # segment. Everything after it is a media fragment, keyframe or
                # not according to the marker that preceded it.
                is_init = stream.init_segment is None
                await relay.publish(
                    station_id, data,
                    is_init=is_init,
                    keyframe=pending_keyframe and not is_init,
                )
                pending_keyframe = False
                continue

            # Text frames are control. Deliberately minimal: the station says
            # when a new encoder session begins, what its encoder produced, and
            # which fragment is a keyframe - the three things the relay cannot
            # work out for itself without parsing the media, which is exactly
            # what it must not do.
            if text == "key":
                pending_keyframe = True
                continue
            if text == "init":
                # Discards the segment and fragments, and deliberately NOT the
                # codec. The documented order is codec, then `init`, then the
                # segment - so clearing it here threw away the string the
                # station had just sent, every time, and every viewer got bytes
                # with nothing to decode them as. A new connection still resets
                # it (publisher_connected), which is the case that matters.
                stream.init_segment = None
                stream.recent.clear()
                stream.recent_bytes = 0
                pending_keyframe = False
                continue
            if text.startswith("{"):
                try:
                    codec = json.loads(text).get("codec")
                except ValueError:
                    codec = None
                if isinstance(codec, str) and codec:
                    await relay.set_codec(station_id, codec)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Media ingest failed for station %s.", station_id)
    finally:
        revoked.cancel()
        await relay.publisher_gone(station_id)


# --- the viewer side -----------------------------------------------------


@router.websocket("/media/view")
async def media_view(websocket: WebSocket, ticket: str = Query(...)) -> None:
    """A browser watching. Redeems a ticket, then receives fMP4."""
    _prune()
    claim = _tickets.pop(ticket, None)
    if claim is None:
        await websocket.close(code=4403)
        return

    station_id: uuid.UUID = claim["station_id"]
    organization_id: uuid.UUID = claim["organization_id"]

    # Re-checked at attach, not trusted from issue. A grant can be withdrawn in
    # the seconds between asking for a ticket and using it, and this is the last
    # point at which that can still be caught.
    with PrivilegedSessionLocal() as db:
        granted = capabilities_for(
            db,
            user_id=claim["user_id"],
            organization_id=organization_id,
            ground_station_id=station_id,
        )
    if Capability.VIDEO_VIEW not in granted:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    stream, queue = await relay.attach(station_id, organization_id)
    log.info(
        "Media viewer attached to %s (%d watching).",
        station_id, len(stream.viewers),
    )

    async def pump() -> None:
        # The codec first, and as text. Media Source Extensions cannot create a
        # buffer without the exact codec string, so a viewer that receives only
        # bytes silently decodes nothing - no error, no picture, which is
        # exactly how this failed the first time.
        if stream.codec:
            await websocket.send_text(json.dumps({"codec": stream.codec}))
        # Then whatever a late joiner needs to decode: the init segment, and the
        # most recent fragment so a picture appears without waiting.
        for chunk in stream.snapshot_for_new_viewer():
            await websocket.send_bytes(chunk)
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            if isinstance(chunk, str):
                await websocket.send_text(chunk)
            else:
                await websocket.send_bytes(chunk)

    def _viewer_still_allowed() -> bool:
        """The same two questions the console's own socket keeps asking."""
        with PrivilegedSessionLocal() as db:
            if AuthSessionRepository(db).get_active(
                    session_id=claim["session_id"]) is None:
                return False
            return Capability.VIDEO_VIEW in capabilities_for(
                db,
                user_id=claim["user_id"],
                organization_id=organization_id,
                ground_station_id=station_id,
            )

    async def revalidate() -> None:
        # Authorising once at attach was the same mistake as authenticating a
        # station once at connect, on the heaviest stream in the platform: a
        # withdrawn grant, a deactivated station or a signed-out session left
        # the camera running in that tab until the station stopped publishing.
        # docs/03-realtime-isolation.md §6 argues at length that this is not
        # acceptable, and it was applied everywhere except here.
        #
        # The backstop, not the whole mechanism. An ended session now also
        # arrives as a push, because a viewer registers itself by session id
        # for `revocation.py` to find (`realtime/media.watch_session`). This
        # timer catches what a push cannot reach: a withdrawn grant, a
        # deactivated station, or a worker that never saw the event.
        # Deliberately the same interval the hub sweeps on.
        while True:
            await asyncio.sleep(settings.stream_revalidate_seconds)
            if await asyncio.to_thread(_viewer_still_allowed):
                continue
            log.info("Viewer of %s is no longer permitted; closing.", station_id)
            return

    async def watch_for_close() -> None:
        # A viewer that closes while nothing is being sent must still be
        # noticed. Waiting only on the fragment queue meant a browser tab shut
        # during a quiet moment was never detached, so the last viewer never
        # left, video.stop was never sent, and the station kept streaming to
        # nobody - the exact bandwidth on-demand exists to save. Found by
        # watching the command channel rather than by reading this code.
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

    # Something for a revocation push to find. The poll below remains the
    # backstop; this is what makes signing out stop the picture now rather
    # than within a minute. A ticket with an unreadable session id still
    # streams and still polls - it simply cannot be pushed to.
    try:
        session_uuid = uuid.UUID(str(claim["session_id"]))
    except (KeyError, TypeError, ValueError):
        session_uuid = None
    revoked = (
        media_relay.watch_session(session_uuid) if session_uuid
        else asyncio.Event()
    )

    async def wait_for_revocation() -> None:
        await revoked.wait()
        log.info("Viewer of %s closed: the session was revoked.", station_id)

    sender = asyncio.create_task(pump())
    closer = asyncio.create_task(watch_for_close())
    checker = asyncio.create_task(revalidate())
    pushed = asyncio.create_task(wait_for_revocation())
    try:
        done, pending = await asyncio.wait(
            {sender, closer, checker, pushed}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                log.debug("Media viewer of %s ended: %r", station_id, exc)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("Media viewer of %s ended.", station_id, exc_info=True)
    finally:
        sender.cancel()
        closer.cancel()
        checker.cancel()
        pushed.cancel()
        if session_uuid:
            media_relay.unwatch_session(session_uuid, revoked)
        await relay.detach(station_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass


# --- on demand -----------------------------------------------------------


async def _demand_changed(stream, wanted: bool) -> None:
    """Ask the station to start or stop, as viewers come and go."""
    command = {"kind": "video.start" if wanted else "video.stop"}
    if wanted:
        command["lease_seconds"] = LEASE_SECONDS
    if not publish_sync(command_channel(stream.station_id), command):
        log.warning(
            "Could not reach station %s to %s video.",
            stream.station_id, "start" if wanted else "stop",
        )


async def renew_leases() -> None:
    """Keep telling watched stations to keep going.

    The station's lease expires on its own, so this is what makes the stream
    stop when the platform goes away rather than when it remembers to say so.
    Silence is the stop signal, which is the only version of this that survives
    the platform crashing.
    """
    while True:
        try:
            for stream in relay.watched_stations():
                publish_sync(
                    command_channel(stream.station_id),
                    {"kind": "video.start", "lease_seconds": LEASE_SECONDS},
                )
        except Exception:
            log.exception("Media lease renewal failed.")
        await asyncio.sleep(RENEW_SECONDS)


relay.on_demand_changed = _demand_changed
# Keep a stream up briefly after its last viewer leaves, so a reload returns to
# it live. Read once here rather than per-detach: the same place the demand
# callback is wired, and the relay stays free of the config import.
relay.linger_seconds = settings.stream_linger_seconds
