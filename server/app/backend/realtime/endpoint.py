"""The WebSocket endpoint.

Protocol
--------
client -> server (JSON text frames):
    {"type": "select_station", "ground_station_id": "<uuid>"}
    {"type": "subscribe",      "stream": "status|telemetry|video|audio"}
    {"type": "unsubscribe",    "stream": "..."}
    {"type": "ping"}

server -> client:
    {"type": "hello",            "user_id", "organization_id", "stations": [...]}
    {"type": "station_selected", "ground_station_id", "capabilities": [...]}
    {"type": "subscribed",       "stream"}
    {"type": "unsubscribed",     "stream"}
    {"type": "event",            "stream", "station_id", "payload"}
    {"type": "status",           "station_id", "payload"}
    {"type": "station_revoked",  "reason"}
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
            hub.unsubscribe(conn, stream)
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

    conn.enqueue(
        {"type": "error", "code": "bad_request", "message": "unknown message type"}
    )


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
