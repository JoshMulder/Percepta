"""The Odin wall socket.

THIS SESSION CROSSES TENANT BOUNDARIES. Its active organisation is the platform
org, so row-level security is bypassed for the life of the connection, and every
query behind it must scope itself in code — the same rule as api/platform.py and
for the same reason. Nothing here queries anything: the socket only relays a
frame computed elsewhere, which is deliberate. A read that crosses tenants is
worth keeping to as few places as possible, and this is not one of them.

Authorised ONCE, at connect. That is not a shortcut: the frame is identical for
every viewer, so there is no per-viewer authorisation question to re-ask — an
operator either sees the whole fleet or is not on this socket at all. What still
has to be handled is the operator's access being taken away mid-shift, which the
close on revocation below covers.

Cookie authentication, never a token in the query string: URLs end up in proxy
logs, browser history and Referer headers, and a stream credential does not
belong in any of them. This mirrors realtime/endpoint.py rather than inventing a
second convention.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from backend.database.models.enums import UserRole
from backend.realtime.odin import wall

log = logging.getLogger(__name__)

router = APIRouter(tags=["odin"])

#: Matches realtime/endpoint.py. 4401 is "you are not authenticated", 4403 is
#: "you were, and are not any more" — the client tells them apart to decide
#: between prompting for a login and simply falling back to polling.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403

#: Long enough that a healthy wall never trips it (the digest is every 3s), short
#: enough that a dead uplink is noticed while the operator is still in the room.
IDLE_TIMEOUT_SECONDS = 30.0


@router.websocket("/api/odin/ws")
async def odin_wall(websocket: WebSocket) -> None:
    """One command-centre screen, fed the fleet digest."""
    # Imported here rather than at module scope: realtime/endpoint.py owns the
    # cookie extraction and the threaded authentication, and importing it at
    # import time would make this module part of that one's import cycle.
    from backend.realtime.endpoint import _authenticate, _extract_token

    await websocket.accept()

    token = _extract_token(websocket)
    identity = None
    if token:
        identity = await asyncio.to_thread(_authenticate, token)
    if identity is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return

    # The same test as require_odin_watch, applied by hand because a dependency
    # cannot run on a websocket route. Admin is a superset of watch: a platform
    # administrator keeps every view an operator has.
    watch_roles = {UserRole.ADMIN.value, UserRole.WATCH.value}
    if not identity.is_platform_admin or not watch_roles.intersection(identity.roles):
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    viewer = wall.register(identity.user_id)
    try:
        while True:
            try:
                frame = await asyncio.wait_for(
                    viewer.queue.get(), timeout=IDLE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                # Nothing to send for a whole interval means the digest has
                # stopped, not that the fleet is quiet — it publishes whether or
                # not anything changed. Ping so the client learns the socket is
                # alive but starved, and can say so rather than showing a frozen
                # wall as a current one.
                if websocket.client_state is not WebSocketState.CONNECTED:
                    break
                await websocket.send_json({"type": "odin.idle"})
                continue
            await websocket.send_text(frame)
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError: the socket closed underneath a send. Normal on a wall
        # display that was simply switched off.
        pass
    finally:
        wall.unregister(viewer)
        if websocket.client_state is WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass


@router.websocket("/api/odin/watch")
async def odin_watch(websocket: WebSocket) -> None:
    """The listening watch: a hub connection that may guard other tenants' audio.

    A SECOND socket, not a second protocol on the wall socket, and the split is
    the point. The wall is one-way and identical for every viewer — it relays a
    frame computed elsewhere and queries nothing. This one takes messages, joins
    groups and reaches across tenant boundaries. Keeping the surface that can be
    talked into something separate from the surface that cannot is worth one
    extra connection.

    It runs the ORDINARY console socket lifecycle (realtime/endpoint.serve), so
    registration, revocation push, the revalidation sweep and the bounded send
    queue all apply unchanged. What is different is only this door, and
    hub.watch_join behind it.

    Refused HERE as well as in the hub. The hub's check is the one that is
    load-bearing — it is re-read per join, from the membership row — but a socket
    that can never do anything should be told so at the handshake rather than
    left open, accepting messages and silently refusing every one.
    """
    from backend.realtime.endpoint import _authenticate, _extract_token, serve

    await websocket.accept()

    token = _extract_token(websocket)
    identity = None
    if token:
        identity = await asyncio.to_thread(_authenticate, token)
    if identity is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return

    watch_roles = {UserRole.ADMIN.value, UserRole.WATCH.value}
    if not identity.is_platform_admin or not watch_roles.intersection(identity.roles):
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    await serve(websocket, identity)
