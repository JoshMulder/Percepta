"""The WebSocket endpoint.

Protocol
--------
client -> server (JSON text frames):
    {"type": "select_station", "ground_station_id": "<uuid>"}
    {"type": "subscribe",      "stream": "status|telemetry|video|audio"}
    {"type": "unsubscribe",    "stream": "..."}
    {"type": "watch_set",      "stations": ["<uuid>", ...]}   (Odin watch only)
    {"type": "poster_set",     "stations": ["<uuid>", ...]}   (Odin watch only)
    {"type": "attach_station", "ground_station_id": "<uuid>"|null}  (Odin watch)
    {"type": "ping"}

server -> client:
    {"type": "hello",            "user_id", "organization_id", "stations": [...]}
    {"type": "station_selected", "ground_station_id", "capabilities": [...]}
    {"type": "subscribed",       "stream"}
    {"type": "unsubscribed",     "stream"}
    {"type": "event",            "stream", "station_id", "payload"}
    {"type": "status",           "station_id", "payload"}
    {"type": "station_revoked",  "reason"}
    {"type": "watching",         "stations": [...]}
    {"type": "posters",          "stations": [...]}
    {"type": "attached",         "ground_station_id": <uuid>|null}
    {"type": "watch_revoked",    "stations": [...]}
    {"type": "revoked",          "reason"}
    {"type": "error",            "code", "message"}
    {"type": "pong"}

Note what the client never sends: a station id on `subscribe`. The station is
pinned on the connection, so a socket cannot be talked into serving one it was
not authorised into.
"""

import asyncio
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from backend.auth.cookies import ACCESS_COOKIE_NAME
from backend.auth.identity import resolve_identity
from backend.database.session import SessionLocal
from backend.realtime.connection import Connection
from backend.realtime.hub import AuthorizationError, hub

log = logging.getLogger(__name__)

# Close codes. 4401 is our "authenticate and try again"; 1008 is a policy
# violation for a client that is authenticated but misbehaving.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_REVOKED = 4403


def _extract_token(ws: WebSocket) -> str | None:
    """Cookie first, then a Bearer header for non-browser clients.

    Deliberately no support for a token in the query string: URLs end up in
    proxy logs, browser history and Referer headers, and a stream credential
    does not belong in any of them.
    """
    cookie_token = ws.cookies.get(ACCESS_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    header = ws.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _authenticate(token: str | None):
    """Runs on a worker thread: SQLAlchemy's Session is sync."""
    with SessionLocal() as db:
        return resolve_identity(db, token)


async def _sender(conn: Connection) -> None:
    """Drains the connection's queue. One task per connection, so a slow socket
    backs up only its own queue - which is bounded and drops oldest."""
    try:
        while True:
            message = await conn.send_queue.get()
            if conn.ws.client_state is not WebSocketState.CONNECTED:
                return
            await conn.ws.send_json(message)
            if message.get("type") in ("revoked",):
                return
    except (WebSocketDisconnect, RuntimeError):
        return
    except Exception:
        log.exception("Sender task failed; closing connection.")


async def _handle_message(conn: Connection, message: dict) -> None:
    kind = message.get("type")

    if kind == "ping":
        conn.enqueue({"type": "pong"})
        return

    if kind == "select_station":
        raw = message.get("ground_station_id")
        try:
            station_id = uuid.UUID(str(raw))
        except (TypeError, ValueError):
            conn.enqueue(
                {"type": "error", "code": "bad_request",
                 "message": "ground_station_id must be a uuid"}
            )
            return
        try:
            capabilities = await hub.select_station(conn, station_id)
        except AuthorizationError:
            # Deliberately identical whether the station is in another org, does
            # not exist, is deactivated, or is simply not granted. Telling them
            # apart would leak the existence of another tenant's hardware.
            conn.enqueue(
                {"type": "error", "code": "not_available",
                 "message": "station not available"}
            )
            return
        conn.enqueue(
            {
                "type": "station_selected",
                "ground_station_id": str(station_id),
                "capabilities": sorted(c.value for c in capabilities),
            }
        )
        return

    if kind in ("subscribe", "unsubscribe"):
        stream = str(message.get("stream", ""))
        if kind == "unsubscribe":
            # `await`, because hub.unsubscribe is async (hub.py:213). Without it
            # the coroutine was created and dropped: the connection never left
            # the group, and the line below cheerfully told the client it had.
            # Latent so far only because the console never unsubscribes — it
            # becomes load-bearing the moment a client guards and releases
            # channels, which is exactly what an Odin listening watch does.
            await hub.unsubscribe(conn, stream)
            conn.enqueue({"type": "unsubscribed", "stream": stream})
            return
        try:
            await hub.subscribe(conn, stream)
        except AuthorizationError as exc:
            conn.enqueue(
                {"type": "error", "code": "not_permitted", "message": str(exc)}
            )
            return
        conn.enqueue({"type": "subscribed", "stream": stream})
        return

    if kind == "watch_set":
        raw = message.get("stations")
        if not isinstance(raw, list):
            conn.enqueue(
                {"type": "error", "code": "bad_request",
                 "message": "stations must be a list of uuids"}
            )
            return
        try:
            station_ids = [uuid.UUID(str(r)) for r in raw]
        except (TypeError, ValueError):
            conn.enqueue(
                {"type": "error", "code": "bad_request",
                 "message": "stations must be a list of uuids"}
            )
            return
        guarded = await hub.watch_set(conn, station_ids)
        # Always the SERVER'S set, never an echo of what was asked for. A station
        # that was refused simply is not in it, and the client reconciles its
        # strip from this — so a channel the operator may not have cannot appear
        # to be guarded because the request was accepted in bulk.
        conn.enqueue(
            {"type": "watching", "stations": sorted(str(s) for s in guarded)}
        )
        return

    if kind == "poster_set":
        raw = message.get("stations")
        if not isinstance(raw, list):
            conn.enqueue(
                {"type": "error", "code": "bad_request",
                 "message": "stations must be a list of uuids"}
            )
            return
        try:
            station_ids = [uuid.UUID(str(r)) for r in raw]
        except (TypeError, ValueError):
            conn.enqueue(
                {"type": "error", "code": "bad_request",
                 "message": "stations must be a list of uuids"}
            )
            return
        showing = await hub.poster_set(conn, station_ids)
        # The SERVER'S set again, for the same reason `watching` is: a station
        # that was refused is simply absent, and a tile whose station is not in
        # this list knows to keep its placeholder rather than waiting for a
        # picture that is never coming.
        conn.enqueue(
            {"type": "posters", "stations": sorted(str(s) for s in showing)}
        )
        return

    if kind == "attach_station":
        raw = message.get("ground_station_id")
        station_id = None
        if raw is not None:
            try:
                station_id = uuid.UUID(str(raw))
            except (TypeError, ValueError):
                conn.enqueue(
                    {"type": "error", "code": "bad_request",
                     "message": "ground_station_id must be a uuid or null"}
                )
                return
        try:
            attached = await hub.attach_station(conn, station_id)
        except AuthorizationError:
            # Deliberately the same answer as an unknown station, for the same
            # reason select_station gives: telling them apart would leak the
            # existence of another tenant's hardware.
            conn.enqueue(
                {"type": "error", "code": "not_available",
                 "message": "station not available"}
            )
            return
        conn.enqueue(
            {
                "type": "attached",
                "ground_station_id": str(attached) if attached else None,
            }
        )
        return

    conn.enqueue(
        {"type": "error", "code": "bad_request", "message": "unknown message type"}
    )


async def serve(ws: WebSocket, identity) -> None:
    """Run an already-accepted, already-authenticated socket.

    Split out of `websocket_endpoint` so the Odin watch can be a route of its own
    — refusing non-watch staff at the handshake, where a refusal is legible to
    the client — without a second copy of the register/sender/receive/unregister
    lifecycle. Two copies of that is two places for a connection to be left in
    the registry after its socket has gone, and the registry is what the fan-out
    walks.
    """
    conn = Connection(ws=ws, identity=identity)
    await hub.register(conn)

    sender = asyncio.create_task(_sender(conn))
    conn.enqueue(
        {
            "type": "hello",
            "user_id": str(conn.user_id),
            "organization_id": str(conn.organization_id),
            "stations": sorted(str(s) for s in conn.visible_stations),
        }
    )

    try:
        while True:
            raw = await ws.receive_json()
            if conn.closed:
                break
            if not isinstance(raw, dict):
                conn.enqueue(
                    {"type": "error", "code": "bad_request",
                     "message": "expected a JSON object"}
                )
                continue
            await _handle_message(conn, raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket receive loop failed.")
    finally:
        await hub.unregister(conn)
        sender.cancel()
        try:
            await sender
        except asyncio.CancelledError:
            pass
        if ws.client_state is WebSocketState.CONNECTED:
            await ws.close(code=CLOSE_REVOKED if conn.closed else 1000)


async def websocket_endpoint(ws: WebSocket) -> None:
    token = _extract_token(ws)
    identity = await asyncio.to_thread(_authenticate, token)

    if identity is None:
        # Accept then close, rather than rejecting the handshake: browsers give
        # scripts no useful detail about a failed WebSocket handshake, so a
        # close code is the only way the client can tell "log in again" from
        # "the server is down".
        await ws.accept()
        await ws.close(code=CLOSE_UNAUTHENTICATED, reason="authentication required")
        return

    await ws.accept()
    await serve(ws, identity)
