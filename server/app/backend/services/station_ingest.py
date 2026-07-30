"""Receive what ground stations publish, and put it on the internal bus.

This is the boundary described in `contract/transport.md`. A station publishes
to channels named for itself and knows nothing about organisations, groups or
subscribers. Everything on the platform side of that line happens here.

    gsu/{station_id}/telemetry  ─┐
    gsu/{station_id}/audio      ─┤
    gsu/{station_id}/video      ─┴─► ingest ─► org:{org}:gsu:{station}:{stream}

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
from backend.services import geocode
from backend.services.enrolment import has_valid_credential

log = logging.getLogger(__name__)

TELEMETRY_PATTERN = "gsu/*/telemetry"
AUDIO_PATTERN = "gsu/*/audio"
VIDEO_PATTERN = "gsu/*/video"

#: Payload kinds the platform understands. Anything else is dropped rather than
#: rejected: a station may legitimately be newer than the platform, and the
#: contract promises unknown kinds are ignored. Logged once per kind so a typo
#: is still visible.
#:
#: `health` is the station describing itself rather than its surroundings - what
#: is actually attached, whether its credential is renewing, what it is holding
#: locally. It rides the telemetry stream and needs telemetry.view like the
#: rest.
KNOWN_KINDS = {
    "adsb", "weather", "power", "radio", "light", "audio", "health", "video",
}

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
        #: Last known synthetic/real state per station, so the row is written
        #: when it changes rather than on every health frame.
        self._simulated: dict[uuid.UUID, bool] = {}
        #: Last position written per station, so a health frame every half
        #: minute is not a database write every half minute.
        self._position: dict[uuid.UUID, tuple[float, float] | None] = {}
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
                    "Station ingest leading (%s); listening on %s, %s and %s.",
                    self._token, TELEMETRY_PATTERN, AUDIO_PATTERN, VIDEO_PATTERN,
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
        await pubsub.psubscribe(TELEMETRY_PATTERN, AUDIO_PATTERN, VIDEO_PATTERN)
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

        if kind == "health":
            await self._reconcile_simulated(station_id, payload)
            await self._reconcile_position(station_id, payload)

        # Onto the internal fan-out, where authorisation and per-subscriber
        # delivery already apply. Nothing downstream needs to know the frame
        # came from outside.
        await hub.publish_station(organization_id, station_id, stream, payload)
        await self._touch(station_id)

    async def _reconcile_simulated(self, station_id: uuid.UUID, payload: dict) -> None:
        """Believe the station about whether its own data is synthetic.

        `ground_stations.is_simulated` was previously set only by the platform's
        own simulator, so a real station agent running simulated drivers - no
        hardware attached yet - showed as a live station with nothing to say it
        was not. The station reports the truth per device; this writes it down
        so the station *list* can be honest too, not just the panel of whichever
        station happens to be selected.

        Only written when it changes. A station reports health every half minute
        and this is a fact that changes when hardware is fitted, not per frame.
        """
        devices = payload.get("devices")
        if not isinstance(devices, list) or not devices:
            return
        # Every *selected* sensor synthetic, not merely one of them.
        #
        # `any` was right while a station was all real or all simulated. It
        # stopped being right once demo became a per-slot choice: a bench box
        # with a live camera and a demo weather head is a real station being
        # worked on, and badging the whole thing DEMO in the switcher would
        # invite somebody to disbelieve the camera. Empty slots are not
        # evidence either way and are ignored, so a station with one demo
        # sensor and nothing else fitted still counts.
        #
        # The panels do not use this. Each one is badged from its own stream,
        # which is the only way a half-real station can be described honestly.
        # This is purely the chip in the station list, where there is no room
        # for nuance and no health frame for the stations you are not watching.
        configured = [
            d for d in devices
            if isinstance(d, dict) and d.get("configured") is True
        ]
        simulated = bool(configured) and all(
            d.get("simulated") is True for d in configured
        )
        if self._simulated.get(station_id) == simulated:
            return
        self._simulated[station_id] = simulated
        await asyncio.to_thread(self._write_simulated, station_id, simulated)

    async def _reconcile_position(self, station_id: uuid.UUID, payload: dict) -> None:
        """Believe the station about where it is.

        The station owns its position: it is set on the box, by whoever is
        standing at the site, and the platform stores what it is told rather
        than offering a second field that can disagree. The console's
        latitude and longitude are read-only for the same reason - two places
        to set one fact is two places for it to be wrong, and the one with a
        person and a handset at it wins.

        An **absent** `position` means "not telling you", not "I have none":
        a station enrolled before the field existed sends nothing, and reading
        that as a clearing instruction would silently take the map away from
        every station in the field. Clearing is therefore explicit -
        `"position": null` - which is the retraction the station side raised
        as unanswerable from its end (CONTRACT-QUESTIONS item 16) and which is
        the platform's to define. This is that definition.
        """
        if "position" not in payload:
            return
        position = payload.get("position")

        if position is None:
            fix: tuple[float, float] | None = None
        elif isinstance(position, dict):
            try:
                latitude = float(position["latitude"])
                longitude = float(position["longitude"])
            except (KeyError, TypeError, ValueError):
                log.warning(
                    "Station %s reported a position that could not be read: %r",
                    station_id, position,
                )
                return
            if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
                log.warning(
                    "Station %s reported a position out of range: %r",
                    station_id, position,
                )
                return
            fix = (latitude, longitude)
        else:
            return

        if station_id in self._position and self._position[station_id] == fix:
            return
        self._position[station_id] = fix
        await asyncio.to_thread(self._write_position, station_id, fix)

    def _write_position(
        self, station_id: uuid.UUID, fix: tuple[float, float] | None
    ) -> None:
        try:
            with PrivilegedSessionLocal() as db:
                # The locality is derived from the position, so it is written
                # in the same statement — there is no moment where a station
                # carries one position's coordinates and another's town.
                place = self._locality_for(db, station_id, fix)
                db.execute(
                    update(GroundStation)
                    .where(GroundStation.id == station_id)
                    .values(
                        latitude=fix[0] if fix else None,
                        longitude=fix[1] if fix else None,
                        **place,
                    )
                )
                db.commit()
            log.info(
                "Station %s position %s.",
                station_id,
                f"set to {fix[0]:.5f}, {fix[1]:.5f}" if fix else "cleared by the station",
            )
        except Exception:
            log.exception("Could not update position for %s.", station_id)

    @staticmethod
    def _fingerprint(fix: tuple[float, float] | None) -> str | None:
        """The coordinates a stored locality belongs to.

        Rounded to five places — about a metre — because a station reporting a
        position derived from GNSS jitters in the last digits, and looking a
        town up again because a mast moved 30 cm would be a request per health
        frame for ever.
        """
        return None if fix is None else f"{fix[0]:.5f},{fix[1]:.5f}"

    def _locality_for(
        self, db, station_id: uuid.UUID, fix: tuple[float, float] | None
    ) -> dict:
        """Columns to write alongside a new position.

        Skips the network entirely when the position has not really changed,
        which is the normal case: this runs on every health frame that carries
        a position, and a fixed site sends the same one for months.
        """
        wanted = self._fingerprint(fix)
        current = db.execute(
            select(GroundStation.locality_for).where(GroundStation.id == station_id)
        ).scalar_one_or_none()
        if current == wanted:
            return {}
        if fix is None:
            return {"locality": None, "region": None, "locality_for": None}

        place = geocode.describe(fix[0], fix[1])
        if place is None:
            # Remember that this position was tried, so a station over water
            # does not re-ask on every frame. A retry costs a position change.
            return {"locality": None, "region": None, "locality_for": wanted}
        log.info("Station %s is at %s.", station_id, place["label"])
        return {
            "locality": place["locality"],
            "region": place["region"],
            "locality_for": wanted,
        }

    def _write_simulated(self, station_id: uuid.UUID, simulated: bool) -> None:
        try:
            with PrivilegedSessionLocal() as db:
                db.execute(
                    update(GroundStation)
                    .where(
                        GroundStation.id == station_id,
                        GroundStation.is_simulated.is_(not simulated),
                    )
                    .values(is_simulated=simulated)
                )
                db.commit()
        except Exception:
            log.exception("Could not update is_simulated for %s.", station_id)

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
