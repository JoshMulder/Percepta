"""Receive what ground stations publish, and put it on the internal bus.

This is the boundary described in `contract/transport.md`. A station publishes
to channels named for itself and knows nothing about organisations, groups or
subscribers. Everything on the platform side of that line happens here.

    gsu/{station_id}/telemetry  ─┐
    gsu/{station_id}/audio      ─┴─► ingest ─► org:{org}:gsu:{station}:{stream}

**The organisation is resolved here, from the device registry, keyed on the
station id — never from anything the station sent.** That single rule is what
makes the rest of the isolation model true: a compromised station can forge its
own sensor readings, which is unavoidable because it owns the sensors, and can
do nothing else. There is deliberately no `organization_id` field in the
contract, and if one ever appears it must still be ignored here.

A station must be enrolled and hold a valid credential for anything it sends to
reach a subscriber (`contract/enrolment.md`). That check lives here as a second
line: the broker is supposed to have refused the connection already. It has real
value anyway - revoking a credential stops the data within one registry TTL even
if the broker never noticed - but note what it does *not* do. It confirms the
named station is entitled to publish; it cannot confirm the publisher is that
station. Only broker authentication does that, and on the development stack
nothing authenticates to Redis at all, so anything with access can still forge
any enrolled station. See `services/broker_acl.py` for what closes it.

**Exactly one ingest runs at a time**, elected by a Redis lease. Everything else
in the realtime layer is safe to run in every worker, because each worker only
delivers to its own connections. This is the exception: it *republishes*, so two
of them would put two copies of every frame on the fan-out and every console
would render each reading twice. That failure looks like flapping data rather
than like duplicate processes, so it is prevented here rather than diagnosed
later. A worker that is not the leader idles cheaply and takes over within a
lease period if the leader dies — a gap of a few seconds in a stream the
contract already declares droppable.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy import select, update

from backend.core.config import settings
from backend.database.models.ground_station import GroundStation
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.hub import hub
from backend.services.enrolment import has_valid_credential

log = logging.getLogger(__name__)

TELEMETRY_PATTERN = "gsu/*/telemetry"
AUDIO_PATTERN = "gsu/*/audio"

#: Payload kinds the platform understands. Anything else is dropped rather than
#: rejected: a station may legitimately be newer than the platform, and the
#: contract promises unknown kinds are ignored. Logged once per kind so a typo
#: is still visible.
KNOWN_KINDS = {"adsb", "weather", "power", "radio", "light", "audio"}

#: How often a station's last_seen_at is written. Telemetry arrives several
#: times a second; the console's online threshold is two minutes, so writing
#: every frame would be thousands of pointless updates an hour per station.
SEEN_INTERVAL = timedelta(seconds=15)

#: How long a station id → organisation mapping is trusted before re-reading.
#: Short enough that deactivating a station takes effect promptly, long enough
#: that 1 Hz telemetry does not become 1 Hz of database traffic.
REGISTRY_TTL = timedelta(seconds=30)

#: Leadership lease. Held by whichever worker is running the ingest, renewed
#: while it lives, and allowed to expire if it dies so another can take over.
LEADER_KEY = "percepta:ingest:leader"
LEASE_SECONDS = 15
RENEW_SECONDS = 5.0
CONTEND_SECONDS = 3.0


class StationIngest:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._redis: aioredis.Redis | None = None
        # Identifies this worker's claim on the lease, so a renewal can check
        # the lease is still the one it took rather than one it inherited.
        self._token = uuid.uuid4().hex
        self._registry: dict[uuid.UUID, tuple[uuid.UUID | None, datetime]] = {}
        self._seen: dict[uuid.UUID, datetime] = {}
        self._unknown_kinds: set[str] = set()
        self._unknown_stations: set[uuid.UUID] = set()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._release()
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    async def _run(self) -> None:
        while True:
            try:
                self._redis = aioredis.Redis.from_url(settings.redis_url)
                while not await self._acquire():
                    await asyncio.sleep(CONTEND_SECONDS)
                log.info(
                    "Station ingest leading (%s); listening on %s and %s.",
                    self._token, TELEMETRY_PATTERN, AUDIO_PATTERN,
                )
                try:
                    await self._lead()
                finally:
                    await self._release()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Station ingest failed; retrying in 5s.")
                await asyncio.sleep(5)

    async def _lead(self) -> None:
        """Consume until the lease is lost. Losing it is not an error: it means
        this worker stalled long enough that another has legitimately taken over,
        and continuing would produce exactly the duplication the lease prevents.
        """
        assert self._redis is not None
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(TELEMETRY_PATTERN, AUDIO_PATTERN)
        try:
            next_renew = time.monotonic() + RENEW_SECONDS
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is not None:
                    await self._handle(message)
                if time.monotonic() >= next_renew:
                    if not await self._renew():
                        log.warning(
                            "Station ingest lost its lease; standing down."
                        )
                        return
                    next_renew = time.monotonic() + RENEW_SECONDS
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                pass

    # --- leadership -----------------------------------------------------

    async def _acquire(self) -> bool:
        assert self._redis is not None
        return bool(
            await self._redis.set(
                LEADER_KEY, self._token, nx=True, ex=LEASE_SECONDS
            )
        )

    async def _renew(self) -> bool:
        """Extend the lease, but only if it is still ours."""
        assert self._redis is not None
        return bool(
            await self._redis.eval(
                """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                  return redis.call('expire', KEYS[1], ARGV[2])
                end
                return 0
                """,
                1, LEADER_KEY, self._token, LEASE_SECONDS,
            )
        )

    async def _release(self) -> None:
        """Give the lease up on a clean shutdown, so a restart is not delayed by
        a lease this process no longer intends to honour."""
        if self._redis is None:
            return
        try:
            await self._redis.eval(
                """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                  return redis.call('del', KEYS[1])
                end
                return 0
                """,
                1, LEADER_KEY, self._token,
            )
        except Exception:
            pass

    # --- registry -------------------------------------------------------

    def _lookup(self, station_id: uuid.UUID) -> uuid.UUID | None:
        """Organisation for a station, or None if it may not publish.

        Read on the privileged connection deliberately: this runs before any org
        context exists, which is the whole point - it is the step that
        *establishes* which org the data belongs to.

        A station must be active *and* hold a valid credential. The credential
        check is what gives revocation teeth here: the broker should already
        have closed the connection, but if it has not - and on the development
        stack it cannot, because nothing authenticates to Redis - a revoked
        station's data stops reaching subscribers within one registry TTL
        anyway. It is a second line, not the first.
        """
        with PrivilegedSessionLocal() as db:
            org_id = db.execute(
                select(GroundStation.organization_id).where(
                    GroundStation.id == station_id,
                    GroundStation.is_active.is_(True),
                )
            ).scalar_one_or_none()
            if org_id is None:
                return None
            if not has_valid_credential(db, station_id=station_id):
                return None
            return org_id

    async def _organization(self, station_id: uuid.UUID) -> uuid.UUID | None:
        now = datetime.now(UTC)
        cached = self._registry.get(station_id)
        if cached is not None and now - cached[1] < REGISTRY_TTL:
            return cached[0]

        org_id = await asyncio.to_thread(self._lookup, station_id)
        self._registry[station_id] = (org_id, now)

        if org_id is None:
            # Once per station, not once per frame - an unknown station
            # publishing at 1 Hz would otherwise fill the log.
            if station_id not in self._unknown_stations:
                self._unknown_stations.add(station_id)
                log.warning(
                    "Dropping telemetry from %s: unknown, deactivated, or "
                    "holding no valid credential.", station_id,
                )
        else:
            self._unknown_stations.discard(station_id)
        return org_id

    # --- handling -------------------------------------------------------

    async def _handle(self, message: dict) -> None:
        channel = message.get("channel")
        if isinstance(channel, bytes):
            channel = channel.decode()
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode()
        if not channel or not isinstance(data, str):
            return

        # gsu/{station_id}/{stream}
        parts = channel.split("/")
        if len(parts) != 3 or parts[0] != "gsu":
            return
        stream = parts[2]
        try:
            station_id = uuid.UUID(parts[1])
        except ValueError:
            return

        try:
            payload = json.loads(data)
        except ValueError:
            log.warning("Ignoring non-JSON payload on %s.", channel)
            return
        if not isinstance(payload, dict):
            return

        kind = str(payload.get("kind", ""))
        if kind not in KNOWN_KINDS:
            if kind not in self._unknown_kinds:
                self._unknown_kinds.add(kind)
                log.info("Ignoring unknown telemetry kind %r from %s.",
                         kind, station_id)
            return

        organization_id = await self._organization(station_id)
        if organization_id is None:
            return

        # Onto the internal fan-out, where authorisation and per-subscriber
        # delivery already apply. Nothing downstream needs to know the frame
        # came from outside.
        await hub.publish_station(organization_id, station_id, stream, payload)
        await self._touch(station_id)

    async def _touch(self, station_id: uuid.UUID) -> None:
        """Record that the station is alive, at most every SEEN_INTERVAL."""
        now = datetime.now(UTC)
        last = self._seen.get(station_id)
        if last is not None and now - last < SEEN_INTERVAL:
            return
        self._seen[station_id] = now
        await asyncio.to_thread(self._write_seen, station_id, now)

    def _write_seen(self, station_id: uuid.UUID, when: datetime) -> None:
        try:
            with PrivilegedSessionLocal() as db:
                db.execute(
                    update(GroundStation)
                    .where(GroundStation.id == station_id)
                    .values(last_seen_at=when)
                )
                db.commit()
        except Exception:
            log.exception("Could not update last_seen_at for %s.", station_id)


station_ingest = StationIngest()
