"""Prove the 443 broker relay, including the property it exists to enforce.

    docker compose exec app python -m backend.scripts.verify_broker

`/broker` is what a station uses when the broker's own port is unreachable,
which behind a reverse proxy is always. It is a message relay rather than a
Redis proxy for one reason: a station must be able to publish its own telemetry
and nothing else. Tunnelling RESP would hand a box the ability to `SUBSCRIBE`
to every other station's channel and every other organisation's commands.

That check is forty lines of ours rather than a broker's audited ACL engine,
which is the honest cost of not running a second broker — so it is worth a
script that proves it rather than a comment claiming it.

Exercised against the endpoint's own logic with a stub socket: the wire is
covered by the station's client and by running one, and what is interesting
here is the decision, not the framing.
"""

import asyncio
import json
import logging
import sys
import uuid

from backend.api import broker

log = logging.getLogger("verify_broker")
_failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    if ok:
        log.info("  PASS  %s", label)
    else:
        _failures.append(label)
        log.error("  FAIL  %s%s", label, f"  ({detail})" if detail else "")


class StubSocket:
    """Enough WebSocket for the endpoint's decision path."""

    def __init__(self, frames: list[str]) -> None:
        self.headers = {"authorization": "Bearer stub"}
        self._incoming = list(frames)
        self.sent: list[dict] = []
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if not self._incoming:
            from fastapi.websockets import WebSocketDisconnect
            raise WebSocketDisconnect(code=1000)
        return self._incoming.pop(0)

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


class StubRedis:
    """Records what the relay decided to publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, json.loads(data)))

    def pubsub(self):
        return StubPubSub()

    async def aclose(self) -> None:
        pass


class StubPubSub:
    async def subscribe(self, *_channels) -> None: ...
    async def unsubscribe(self, *_channels) -> None: ...
    async def aclose(self) -> None: ...

    async def listen(self):
        # Never yields: the command direction is proven by running a station,
        # and a generator that blocks for ever here would hang the script.
        await asyncio.sleep(3600)
        yield {}


async def run_relay(station_id: uuid.UUID, frames: list[str]):
    """Drive the endpoint with a stubbed socket, Redis and authentication."""
    socket = StubSocket(frames)
    redis = StubRedis()

    class Found:
        id = station_id
        organization_id = uuid.uuid4()

    class Credential:
        # The endpoint keeps this to re-check revocation on the open socket.
        # The watcher itself is proven against a real credential and a real
        # socket by verify_enrolment §5; here it only has to exist.
        id = uuid.uuid4()

    # The console notice is not what these checks are about, and publishing it
    # would need a live hub.
    original_announce = broker._announce

    async def quiet(*_a, **_k):
        return None

    broker._announce = quiet
    original_auth = broker.enrolment.authenticate
    original_redis = broker.aioredis.Redis.from_url
    original_session = broker.PrivilegedSessionLocal
    broker.enrolment.authenticate = lambda db, secret: (Found(), Credential())
    broker.aioredis.Redis.from_url = lambda *_a, **_k: redis

    class NullSession:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def commit(self): pass

    broker.PrivilegedSessionLocal = NullSession
    try:
        await broker.broker(socket)
    finally:
        broker.enrolment.authenticate = original_auth
        broker.aioredis.Redis.from_url = original_redis
        broker.PrivilegedSessionLocal = original_session
        broker._announce = original_announce
    return socket, redis


async def main_async() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mine = uuid.uuid4()
    other = uuid.uuid4()

    log.info("A station publishing its own telemetry")
    socket, redis = await run_relay(mine, [
        json.dumps({"topic": f"gsu/{mine}/telemetry", "payload": {"kind": "power"}}),
    ])
    check(len(redis.published) == 1, "it reaches Redis unchanged",
          f"published {redis.published}")
    check(redis.published and redis.published[0][0] == f"gsu/{mine}/telemetry",
          "on its own channel")
    check(not socket.sent, "and nothing is refused")

    log.info("The same station reaching for somebody else's")
    socket, redis = await run_relay(mine, [
        json.dumps({"topic": f"gsu/{other}/telemetry", "payload": {"x": 1}}),
        json.dumps({"topic": f"cmd/gsu/{other}", "payload": {"kind": "light.set"}}),
        json.dumps({"topic": "rt:g:everyone", "payload": {"x": 1}}),
        json.dumps({"topic": "gsu/*/telemetry", "payload": {"x": 1}}),
    ])
    check(not redis.published, "nothing is published",
          f"leaked {redis.published}")
    check(len(socket.sent) == 4, "every attempt is refused",
          f"{len(socket.sent)} refusals for 4 attempts")
    check(all(m.get("type") == "refused" for m in socket.sent),
          "and the station is told, not silently ignored")

    log.info("Frames that are not messages")
    socket, redis = await run_relay(mine, [
        "not json", "[]", json.dumps({"topic": 1, "payload": {}}),
        json.dumps({"topic": "x", "payload": "not an object"}),
    ])
    check(not redis.published, "none of them publish anything")

    log.info("A frame past the size cap")
    socket, redis = await run_relay(mine, [
        json.dumps({"topic": f"gsu/{mine}/telemetry",
                    "payload": {"pad": "y" * (broker.MAX_FRAME_BYTES + 100)}}),
    ])
    check(not redis.published, "is not published")
    check(socket.close_code == 1009, "and the socket is closed",
          f"close code {socket.close_code}")

    if _failures:
        log.error("FAILED: %d check(s): %s", len(_failures), _failures)
        return 1
    log.info("All checks passed.")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
