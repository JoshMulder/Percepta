"""The broker, reached over 443.

    WS   /broker      station ↔ platform, `{topic, payload}` JSON frames

**This is the deployment path for station traffic.** Redis on 6380 works on a
LAN and nowhere else: behind a reverse proxy — which is what a public
deployment is — that port is shut, and 443 is the one that is open at every
site, on every corporate network, over Starlink.

A RELAY, NOT A PROXY
--------------------
Nothing here speaks RESP to the station. Tunnelling Redis would let a box
`SUBSCRIBE` to any channel on this platform, which is every other station's
telemetry and every other organisation's commands. What crosses this socket is
one JSON object per message, and this endpoint decides what may be published.

**The station's identity comes from the credential, never from the frame.**
That is `contract/README.md` rule 1, the same rule `/media/ingest` states: a
box holding a valid secret still cannot claim to be a different station. Here
it has teeth — the topic a station may publish to is *derived* from the
authenticated id and compared, and anything else is refused and counted rather
than published. Without that check this endpoint would be a way for one
compromised station to forge every other station's telemetry.

Downstream sees nothing new: accepted messages are published into the same
Redis channels the direct transport uses, so `station_ingest` cannot tell which
way a station arrived.
"""

from __future__ import annotations

import asyncio
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
from backend.services import enrolment

log = logging.getLogger(__name__)

router = APIRouter(tags=["broker"])

#: What a station may publish, formatted with its own id. Anything else is
#: refused. Kept as an explicit list rather than a prefix match: `gsu/{id}/`
#: would also admit a topic nobody consumes, and a station inventing channels
#: on a shared broker is the thing this is here to prevent.
def _permitted(station_id: uuid.UUID) -> frozenset[str]:
    return frozenset({
        f"gsu/{station_id}/telemetry",
        f"gsu/{station_id}/audio",
    })


#: A frame larger than this is dropped and the connection closed. Matches the
#: station's own cap; anything bigger is a bug upstream and a socket that
#: stalls under it is a worse way to find out.
MAX_FRAME_BYTES = 512 * 1024


@router.websocket("/broker")
async def broker(websocket: WebSocket) -> None:
    """A station's telemetry up and its commands down, on one socket."""
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
    permitted = _permitted(station_id)
    log.info("Broker relay open for station %s.", station_id)
    await _announce(organization_id, station_id, online=True)

    # Commands travel the other way on the same socket. Subscribed here rather
    # than through the shared ingest listener because this is one station's
    # channel and this connection is the only thing that wants it — there is no
    # leader election to do and nothing to coordinate between workers.
    client = aioredis.Redis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    await pubsub.subscribe(command_channel(station_id))
    commands = asyncio.create_task(_pump_commands(websocket, pubsub, station_id))
    # Authentication happens once, when the socket opens. Without this a
    # revoked station keeps publishing until something else drops its
    # connection — which on a healthy link is never. See
    # `enrolment.close_when_revoked`.
    revoked = asyncio.create_task(enrolment.close_when_revoked(
        credential_id, station_id, lambda: websocket.close(code=4401),
    ))

    refused = 0
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > MAX_FRAME_BYTES:
                log.warning("Station %s sent %d bytes; closing.", station_id, len(raw))
                await websocket.close(code=1009)
                return
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            topic = message.get("topic")
            payload = message.get("payload")
            if not isinstance(topic, str) or not isinstance(payload, dict):
                continue

            if topic not in permitted:
                # Told, not ignored. A station silently dropping everything it
                # publishes looks exactly like a station with nothing to say,
                # and this is the fault most likely to be a misconfiguration
                # rather than an attack.
                refused += 1
                if refused <= 3:
                    log.warning("Station %s tried to publish to %s.",
                                station_id, topic)
                await websocket.send_text(json.dumps({
                    "type": "refused",
                    "topic": topic,
                    "reason": "not a topic this station may publish to",
                }))
                continue

            await client.publish(topic, json.dumps(payload))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - one station must not take the worker down
        log.exception("Broker relay failed for station %s.", station_id)
    finally:
        commands.cancel()
        revoked.cancel()
        try:
            await pubsub.unsubscribe(command_channel(station_id))
            await pubsub.aclose()
            await client.aclose()
        except Exception:  # noqa: BLE001 - teardown, on a socket already gone
            pass
        # The socket closing is the earliest and most certain evidence a
        # station has gone. Offline was otherwise a timeout — telemetry stops,
        # last_seen_at ages, and the console decides it is stale some seconds
        # later. This is what MQTT would have given us as a Last Will, and the
        # relay gets it for free by knowing when its own socket ends.
        await _announce(organization_id, station_id, online=False)
        log.info("Broker relay closed for station %s.", station_id)


async def _announce(organization_id: uuid.UUID, station_id: uuid.UUID,
                    *, online: bool) -> None:
    """Tell the console a station arrived or left, on the org status channel."""
    try:
        await hub.publish_status(organization_id, station_id, {"online": online})
    except Exception:  # noqa: BLE001 - a console notice must never break the link
        log.debug("Could not announce station %s.", station_id, exc_info=True)


async def _pump_commands(websocket: WebSocket, pubsub, station_id: uuid.UUID) -> None:
    """Forward this station's commands down the socket it is already holding."""
    topic = command_channel(station_id)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            try:
                payload = json.loads(data)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            await websocket.send_text(json.dumps({
                "topic": topic, "payload": payload,
            }))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the read side reports the disconnect
        log.debug("Command pump ended for station %s.", station_id, exc_info=True)
