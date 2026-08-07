"""The station transport, reached over 443.

    WS   /broker      station <-> platform, `{stream, payload}` JSON frames

**This is the only transport contract 2.0 defines.** Redis on 6380 works on a
LAN and nowhere else: behind a reverse proxy — which is what a public
deployment is — that port is shut, and 443 is the one open at every site, on
every corporate network, over Starlink.

A RELAY, NOT A PROXY
--------------------
Nothing here speaks RESP to the station. Tunnelling Redis would let a box
subscribe to any channel on this platform, which is every other station's
telemetry and every other organisation's commands. What crosses this socket is
one JSON object of exactly two keys: a one-letter stream code and a payload.

**The station's identity comes from the credential, and there is nowhere in the
frame to put one.** Under the draft this superseded, a station named its own
topic and the platform compared it against a derived set — which worked, and
depended on the comparison being right. There is now no name to compare: a
station sends `t` and the platform decides what `t` means for whoever this
credential belongs to. Confinement is structural rather than checked, which is
the difference between a rule that holds and a rule that holds until somebody
edits the wrong branch.

Downstream sees nothing new: accepted payloads are published into the same
internal channels the ingest already listens on, through
`station_topics.channel_for_stream`, which is the one place the wire and the
fan-out meet.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from backend.core.config import settings
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.bus import command_channel
from backend.realtime.hub import hub
from backend.services import enrolment, station_topics

log = logging.getLogger(__name__)

router = APIRouter(tags=["broker"])

#: A frame larger than this is dropped and the connection closed (1009).
#: Matches the station's own cap; anything bigger is a bug upstream, and a
#: socket that stalls under it is a worse way to find out.
MAX_FRAME_BYTES = 512 * 1024


def _supersede_channel(station_id: uuid.UUID) -> str:
    """Where a newly-arrived socket announces itself to any older one.

    Contract 2.0: a second socket on the same credential supersedes the first
    and the platform closes the older one. This is not hypothetical — the ping
    rule has a station reconnect the moment its link goes quiet, and the
    platform cannot distinguish the socket that died from one having a quiet
    minute, so for a while it holds both.

    Commands and `events.ack` must go to exactly one of them. Send them to the
    older and the station on the newer never sees its acknowledgement, re-sends
    the same batch for ever, and the platform stores it again on every round.

    Broadcast through Redis rather than an in-process registry because the two
    sockets routinely land on different workers, and an in-process set would
    hold "the newest socket *this worker* has seen", which is not the rule.
    """
    return f"supersede/gsu/{station_id}"


@router.websocket("/broker")
async def broker(websocket: WebSocket) -> None:
    """A station's telemetry up and its commands down, on one socket."""
    auth = websocket.headers.get("authorization", "")
    secret = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    # **Accept first, then close 4401.** Never reject the upgrade.
    #
    # This is a contract rule (`transport.md`, the relay's wire format) and it
    # reads like a technicality until you follow it through. There is no socket
    # to carry a close code until the handshake completes, so closing before
    # `accept()` makes Starlette answer with an HTTP 403 and the station sees no
    # close code at all.
    #
    # 4401 is the entire recovery path for a box whose credential expired while
    # it was offline: it renews once and reconnects. Without the code the box
    # reconnects on a five-minute backoff for ever and never calls `/renew` —
    # so the seven-day grace window in `enrolment.md` §4, which exists purely to
    # make that a non-event, is defeated by the absence of one frame. That is a
    # site visit per station, and it is what this endpoint used to do.
    await websocket.accept()

    if not secret:
        # Log why, rather than a silent 4401. A broker upgrade with no bearer
        # credential means either the client sent none or something between it
        # and here stripped the Authorization header on the WebSocket upgrade —
        # very different fixes, and both otherwise look like an unexplained flap.
        log.warning(
            "Broker 4401 for %s: no bearer credential (authorization header %s).",
            websocket.client,
            "absent"
            if websocket.headers.get("authorization") is None
            else "present but not 'Bearer …'",
        )
        await websocket.close(code=4401)
        return

    with PrivilegedSessionLocal() as db:
        found = enrolment.authenticate(db, secret=secret)
        if found is None:
            # A well-formed bearer that matches no live credential: the header
            # survived, so this is a stale/revoked secret on the box, not a
            # stripped header. Distinguished from the case above on purpose.
            log.warning("Broker 4401 for %s: bearer credential not recognised.",
                        websocket.client)
            await websocket.close(code=4401)
            return
        station, credential = found
        station_id = station.id
        organization_id = station.organization_id
        credential_id = credential.id
        db.commit()

    connection = uuid.uuid4()
    log.info("Broker relay open for station %s (connection %s).",
             station_id, connection)
    await _announce(organization_id, station_id, online=True)

    client = aioredis.Redis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    pump = None
    revoked = None
    refused = 0
    try:
        # The Redis setup is inside the guard on purpose. If the broker's Redis
        # is unreachable or refuses auth, the subscribe or the supersede publish
        # raises — and an UNCAUGHT exception here (it used to sit above the try)
        # tears the socket down with no close code at all. The box then sees "no
        # reason given", reconnects, and hot-loops, and the finally below never
        # runs so the station is never even announced offline. Inside the guard
        # it becomes a coded close and a logged fault, the same as any other
        # failure on this socket — which is what makes a Redis outage diagnosable
        # instead of a silent fleet-wide flap.
        await pubsub.subscribe(command_channel(station_id),
                               _supersede_channel(station_id))
        pump = asyncio.create_task(
            _pump_down(websocket, pubsub, station_id, connection))
        # Authentication happens once, when the socket opens. Without this a
        # revoked station keeps publishing until something else drops its
        # connection — which on a healthy link is never.
        revoked = asyncio.create_task(enrolment.close_when_revoked(
            credential_id, station_id, lambda: websocket.close(code=4401),
        ))
        # Announce last, so an older socket is displaced only once this one is
        # actually able to receive what it is taking over.
        await client.publish(_supersede_channel(station_id),
                             json.dumps({"connection": str(connection)}))

        while True:
            raw = await websocket.receive_text()
            if len(raw) > MAX_FRAME_BYTES:
                log.warning("Station %s sent %d bytes; closing.",
                            station_id, len(raw))
                await websocket.close(code=1009)
                return
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            stream = message.get("stream")
            payload = message.get("payload")
            if not isinstance(stream, str) or not isinstance(payload, dict):
                continue

            channel = station_topics.channel_for_stream(stream, station_id)
            if channel is None:
                # Told, not ignored. `c` upward is the likely case — a station
                # echoing what it received — and the socket stays up because a
                # refusal is a diagnosis, not a disconnection.
                refused += 1
                if refused <= 3:
                    log.warning("Station %s published on stream %r.",
                                station_id, stream)
                await websocket.send_text(json.dumps({
                    "type": "refused",
                    "stream": stream,
                    "reason": "a station may publish on t, a or e only",
                }))
                continue

            await client.publish(channel, json.dumps(payload))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - one station must not take the worker down
        # Covers the Redis-setup failures now guarded above as well as a
        # mid-stream drop. Close with a code so the box backs off rather than
        # hot-looping on a codeless drop; 1013 ("try again later") is exactly
        # what a transient platform-side fault is.
        log.exception("Broker relay failed for station %s.", station_id)
        with contextlib.suppress(Exception):
            await websocket.close(code=1013)
    finally:
        if pump is not None:
            pump.cancel()
        if revoked is not None:
            revoked.cancel()
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(command_channel(station_id),
                                     _supersede_channel(station_id))
            await pubsub.aclose()
            await client.aclose()
        # The socket closing is the earliest and most certain evidence a
        # station has gone. Offline was otherwise a timeout — telemetry stops,
        # last_seen_at ages, and the console decides it is stale some seconds
        # later. This is what MQTT would have given us as a Last Will, and the
        # relay gets it for free by knowing when its own socket ends.
        await _announce(organization_id, station_id, online=False)
        log.info("Broker relay closed for station %s (connection %s).",
                 station_id, connection)


async def _announce(organization_id: uuid.UUID, station_id: uuid.UUID,
                    *, online: bool) -> None:
    """Tell the console a station arrived or left, on the org status channel."""
    with contextlib.suppress(Exception):
        # A console notice must never break the link.
        await hub.publish_status(organization_id, station_id, {"online": online})


async def _pump_down(websocket: WebSocket, pubsub, station_id: uuid.UUID,
                     connection: uuid.UUID) -> None:
    """Commands down the socket, and the notice that a newer one has arrived."""
    commands = command_channel(station_id)
    supersede = _supersede_channel(station_id)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            channel = message.get("channel")
            if isinstance(channel, bytes):
                channel = channel.decode("utf-8", "replace")
            try:
                payload = json.loads(data)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue

            if channel == supersede:
                if payload.get("connection") != str(connection):
                    log.info("Station %s opened a newer socket; closing %s.",
                             station_id, connection)
                    with contextlib.suppress(Exception):
                        await websocket.close(code=1012)
                    return
                continue

            # Commands, unrequested. No subscribe handshake exists: the
            # credential already determined whose these are.
            await websocket.send_text(json.dumps({
                "stream": "c", "payload": payload,
            }))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the read side reports the disconnect
        log.debug("Command pump ended for station %s.", station_id, exc_info=True)
