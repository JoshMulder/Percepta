"""Host shell: a platform admin gets a terminal on a station's host.

Three routes, the media path's exact shape (`api/media.py`) — a ticket, a
station-in socket, a browser-out socket:

    POST /api/platform/stations/{id}/host-shell-ticket   authorise, briefly
    WS   /host/ingest                                     station helper → platform
    WS   /host/view?ticket=…                              browser terminal → platform

**This is the biggest trust escalation in the platform: a root-capable shell on
a station's host, reached over the internet.** It is therefore off by default and
gated in depth. The station side needs a privileged helper container that only
exists when the box opts into the `hostshell` compose profile, AND the agent flag
`GSU_HOST_SHELL` before the agent will even write the helper its instructions.
The browser side is `require_platform_admin` to get a ticket, and the ticket is
single-use, station-bound, 60-seconds-lived and re-checked at redemption — the
same reasoning as a stream ticket, because a WebSocket cannot carry a session
header and a token in a URL must be worthless the moment it is spent. Opening and
closing a session are both audited, with the admin's identity.

The relay (`realtime/host.py`) is a byte bridge, not a request/response tunnel —
a PTY is a stream — so these endpoints pump frames verbatim between the two
sockets and interpret nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel

from backend.auth.identity import Identity
from backend.auth.platform import PLATFORM_ORGANIZATION_ID, require_platform_admin
from backend.database.models.ground_station import GroundStation
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.bus import command_channel, publish_sync
from backend.realtime.host import relay
from backend.repositories.auth_session_repository import AuthSessionRepository
from backend.repositories.organization_membership_repository import (
    OrganizationMembershipRepository,
)
from backend.services import enrolment
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(tags=["host"])

#: Ticket lifetime — long enough to open a socket, far too short to keep or find
#: useful in a log later. The media path's number, for the media path's reason.
TICKET_SECONDS = 60

#: How long the station is asked to keep the host session up per `open`, and how
#: often the platform renews it while a terminal is attached. Silence is the stop
#: signal, as with video: if the platform stops renewing — the admin closed the
#: tab, or the worker died — the helper's window lapses and it closes the PTY on
#: its own rather than leaving a root shell open to nobody.
LEASE_SECONDS = 300
RENEW_SECONDS = 120

#: A frame over this closes the socket (1009). A PTY is read in small chunks on
#: the station, so real frames are tiny; this is generous headroom for a burst,
#: and sits under the app-wide `--ws-max-size` (scripts/start_app.py) so the
#: endpoint refuses an oversized frame before uvicorn closes a socket it cannot
#: name.
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: Issued tickets, by id. In-process, like the media path's: the socket a ticket
#: authorises lands on the worker holding the relay anyway.
_tickets: dict[str, dict] = {}


class HostShellTicket(BaseModel):
    ticket: str
    expires_in: int
    url: str


def _prune() -> None:
    now = datetime.now(UTC)
    for key in [k for k, v in _tickets.items() if v["expires_at"] <= now]:
        _tickets.pop(key, None)


def _resolve_station(station_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """(id, organization_id) for a station, read privileged for a platform admin.

    The guard takes no station id and a platform-admin session bypasses RLS, so
    this is the one place the station — and the tenant whose host is about to be
    reached — is resolved. Missing or decommissioned is a 404.
    """
    with PrivilegedSessionLocal() as db:
        station = db.get(GroundStation, station_id)
        if station is None or not station.is_active:
            raise HTTPException(status_code=404, detail="No such station")
        return station.id, station.organization_id


@router.post(
    "/api/platform/stations/{station_id}/host-shell-ticket",
    response_model=HostShellTicket,
)
def host_shell_ticket(
    station_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
) -> HostShellTicket:
    """Authorise a terminal on this station's host, for the next minute.

    Asks the station to open its host session (`host.open` on the command
    channel — the agent then instructs its privileged helper, if the box has
    opted in), audits that we did, and returns a single-use ticket the browser
    redeems at `/host/view`.
    """
    _prune()
    _, organization_id = _resolve_station(station_id)

    _audit(request, identity, station_id, organization_id, "host_shell_open")
    if not publish_sync(
        command_channel(station_id),
        {"kind": "host.open", "lease_seconds": LEASE_SECONDS},
    ):
        raise HTTPException(
            status_code=503, detail="Could not reach the station right now"
        )

    ticket = uuid.uuid4().hex
    _tickets[ticket] = {
        "user_id": identity.user_id,
        # Kept so the terminal socket can re-check the session behind it is still
        # live and still a platform admin — signing out or losing platform access
        # must end the shell, and neither changes the ticket.
        "session_id": identity.session_id,
        "station_id": station_id,
        "organization_id": organization_id,
        "expires_at": datetime.now(UTC) + timedelta(seconds=TICKET_SECONDS),
    }
    return HostShellTicket(
        ticket=ticket,
        expires_in=TICKET_SECONDS,
        url=f"/host/view?ticket={ticket}",
    )


# --- the station side ----------------------------------------------------


@router.websocket("/host/ingest")
async def host_ingest(websocket: WebSocket) -> None:
    """A station's privileged helper opening its host PTY outward.

    Authenticated with the station credential, like `/media/ingest` and
    `/console/ingest` — the helper reads the station's own bearer secret from the
    agent's handoff and presents it here; the station id is derived from it, never
    sent. The socket carries PTY output up and keystrokes/resize down.
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
    link = relay.register_ingest(station_id, organization_id, websocket)
    log.info("Host ingest open for station %s.", station_id)
    revoked = asyncio.create_task(
        enrolment.close_when_revoked(
            credential_id, station_id, lambda: websocket.close(code=4401)
        )
    )
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            text = message.get("text")
            size = len(data) if data is not None else len(text or "")
            if size > MAX_FRAME_BYTES:
                log.warning("Host frame from station %s is %d bytes; closing.",
                            station_id, size)
                await websocket.close(code=1009)
                return
            # Forward PTY output to whoever is watching; drop it if nobody is
            # (the PTY keeps running — a shell prompt with no terminal is fine).
            await _forward(link.viewer, data, text)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Host ingest failed for station %s.", station_id)
    finally:
        revoked.cancel()
        relay.ingest_gone(station_id, link)
        log.info("Host ingest closed for station %s.", station_id)


# --- the browser side ----------------------------------------------------


@router.websocket("/host/view")
async def host_view(websocket: WebSocket, ticket: str = Query(...)) -> None:
    """A platform admin's browser terminal. Redeems a ticket, then bridges."""
    _prune()
    claim = _tickets.pop(ticket, None)
    if claim is None:
        await websocket.close(code=4403)
        return

    station_id: uuid.UUID = claim["station_id"]
    organization_id: uuid.UUID = claim["organization_id"]

    # Re-checked at attach, never trusted from issue: platform access can be
    # withdrawn, or the session signed out, in the seconds between asking for a
    # ticket and using it, and this is the last point that can be caught.
    if not _still_platform_admin(claim):
        await websocket.close(code=4403)
        return

    await websocket.accept()

    # The helper may not have dialled back yet — broker command → agent → handoff
    # → the privileged helper is a few hops. Wait for it, then take the session.
    link = await relay.wait_for(station_id)
    if link is None:
        # No host session opened: the box is offline, has not opted in, or the
        # helper is not running. Tell the terminal in words rather than hanging.
        try:
            await websocket.send_text(
                '\r\n\x1b[31mThe station did not open a host session. It may be '
                "offline, or host shell access may not be enabled on it.\x1b[0m\r\n"
            )
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        _publish_close(station_id)
        return

    # Re-resolve through attach_viewer rather than trusting the link `wait_for`
    # returned: the helper could have reconnected (superseding it) in the gap, and
    # the pump below must send to the socket that is live now, not a closed one.
    link = relay.attach_viewer(station_id, websocket)
    if link is None:
        _publish_close(station_id)
        with contextlib.suppress(Exception):
            await websocket.close()
        return
    log.info("Host terminal attached to station %s.", station_id)
    # Keep the station's session alive while this terminal is open; when it
    # closes we stop, and the helper's window lapses. Silence is the stop signal.
    renew = asyncio.create_task(_renew_lease(station_id))
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            text = message.get("text")
            if not await _forward(link.ingest, data, text):
                break  # the helper's socket went; the session is over
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("Host terminal of %s ended.", station_id, exc_info=True)
    finally:
        renew.cancel()
        relay.detach_viewer(station_id, websocket)
        # Close the session promptly rather than waiting out the lease: a root
        # shell should not outlive the terminal on it by five minutes.
        _publish_close(station_id)
        _audit_close(claim)
        with contextlib.suppress(Exception):
            await websocket.close()


async def _renew_lease(station_id: uuid.UUID) -> None:
    while True:
        await asyncio.sleep(RENEW_SECONDS)
        publish_sync(command_channel(station_id),
                     {"kind": "host.open", "lease_seconds": LEASE_SECONDS})


async def _forward(target: object | None, data: bytes | None, text: str | None) -> bool:
    """Send one frame to the paired socket, preserving binary vs text.

    Returns False if the target is gone or the send failed, so the caller can
    end the session rather than pump into a closed socket.
    """
    if target is None:
        return True  # nobody on the other end yet; not an error
    try:
        if data is not None:
            await target.send_bytes(data)  # type: ignore[attr-defined]
        elif text is not None:
            await target.send_text(text)  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        return False


def _still_platform_admin(claim: dict) -> bool:
    """The session is still live and the user is still a platform-org member —
    which is what platform-admin *is* (`auth/platform.py`)."""
    with PrivilegedSessionLocal() as db:
        session = AuthSessionRepository(db).get_active(session_id=claim["session_id"])
        if session is None or session.user_id != claim["user_id"]:
            return False
        membership = OrganizationMembershipRepository(db).get(
            user_id=claim["user_id"], organization_id=PLATFORM_ORGANIZATION_ID
        )
        return membership is not None


def _publish_close(station_id: uuid.UUID) -> None:
    publish_sync(command_channel(station_id), {"kind": "host.close"})


def _audit(
    request: Request,
    identity: Identity,
    station_id: uuid.UUID,
    organization_id: uuid.UUID,
    action: str,
) -> None:
    record(
        action=action,
        organization_id=organization_id,
        actor_user_id=identity.user_id,
        target_type="ground_station",
        target_id=str(station_id),
        ground_station_id=station_id,
        ip_address=request.client.host if request.client else None,
        detail={"via": "platform", "platform_org": str(PLATFORM_ORGANIZATION_ID)},
    )


def _audit_close(claim: dict) -> None:
    record(
        action="host_shell_close",
        organization_id=claim["organization_id"],
        actor_user_id=claim["user_id"],
        target_type="ground_station",
        target_id=str(claim["station_id"]),
        ground_station_id=claim["station_id"],
        detail={"via": "platform"},
    )
