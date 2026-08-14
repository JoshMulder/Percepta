"""Receive what ground stations publish, and put it on the internal bus.

This is the boundary described in `contract/transport.md`. A station publishes
to channels named for itself and knows nothing about organisations, groups or
subscribers. Everything on the platform side of that line happens here.

    gsu/{station_id}/telemetry  ─┬─► ingest ─► org:{org}:gsu:{station}:{stream}
    gsu/{station_id}/audio      ─┘

Video is not here. The station's periodic JPEG channel was removed because two
readers of one sensor wedged the camera (station/gsu/video.py); live video goes
over the media WebSocket, which has its own relay.

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
from backend.realtime.bus import (
    ADSB_SNAPSHOT_TTL,
    HEALTH_SNAPSHOT_TTL,
    POWER_SNAPSHOT_TTL,
    adsb_snapshot_key,
    command_channel,
    health_snapshot_key,
    power_snapshot_key,
)
from backend.realtime.hub import hub
from backend.services import geocode, station_events, station_topics
from backend.services.enrolment import has_valid_credential
from backend.services.odin_digest import digest

log = logging.getLogger(__name__)

TELEMETRY_PATTERN, AUDIO_PATTERN, EVENTS_PATTERN = (
    station_topics.subscribed_by_platform()
)

#: Payload kinds the platform understands. Anything else is dropped rather than
#: rejected: a station may legitimately be newer than the platform, and the
#: contract promises unknown kinds are ignored. Logged once per kind so a typo
#: is still visible.
#:
#: `health` is the station describing itself rather than its surroundings - what
#: is actually attached, whether its credential is renewing, what it is holding
#: locally. It rides the telemetry stream and needs telemetry.view like the
#: rest.
#: `video` is deliberately absent. The snapshot channel it belonged to was
#: retired (`contract/transport.md`) and the schema defines no such kind, so
#: accepting one here would fan an MJPEG-sized payload out to every subscriber
#: for a stream nothing renders. Live video goes over the media WebSocket.
KNOWN_KINDS = {
    "adsb", "weather", "power", "radio", "light", "audio", "health",
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

#: How long before a station's locality is looked up again.
#:
#: The lookup is a call to somebody else's service with an 8-second timeout,
#: and the position that triggers it arrives in a frame the station controls at
#: a cadence the station chooses. Two things therefore keep it off the hot
#: path: it never runs inside `_handle` (see `_reconcile_position`), and it runs
#: at most this often per station. Without the second, a station whose position
#: jitters past the one-metre fingerprint spends a lookup per health frame -
#: for ever, and against a public endpoint that would rightly ban us for it.
#:
#: A real mast moves when somebody drives to it, so an hour of staleness on the
#: *name* of a place costs nothing. The coordinates themselves are written
#: immediately and are not subject to this.
GEOCODE_COOLDOWN_S = 3600.0


#: The bounds from the contract schemas that a console would otherwise turn
#: into an allocation. Deliberately **not** full schema validation: the
#: contract requires unknown fields and unknown kinds to pass through, a
#: station may legitimately be newer than the platform, and validating every
#: field of every frame at 1 Hz per station would put a large jsonschema
#: traversal on the fleet's one ingest loop for no safety this does not already
#: give.
#:
#: What it is for: the handful of values where "the schema says so" is not
#: enough, because the consumer is a browser that allocates from them. A
#: station is a box on somebody else's network and may be lying.
#: Each entry is (field, expected type, low, high). The type matters as much as
#: the range: a string where a list belongs passes a length check with a small
#: number and then reaches a console that calls .map on it.
LIMITS = {
    "audio": (
        ("rate", float, 8000, 48000),   # ctx.sampleRate / rate scales an allocation
        # Opus now, not base64 PCM. The console feeds these straight to
        # `AudioDecoder`, so the count is what bounds the work it is asked to
        # do in one frame — 200 packets is four seconds of speech, which is
        # already far more than a live stream should ever deliver at once.
        ("packets", list, 0, 200),
    ),
    "adsb": (("aircraft", list, 0, 500),),  # a map marker and DOM subtree each
    "health": (
        ("conditions", list, 0, 64),
        ("devices", list, 0, 64),
        ("resources", list, 0, 64),
    ),
}


def _out_of_bounds(kind: str, payload: dict) -> str | None:
    """Why this frame should not be forwarded, or None if it is fine.

    O(1) per field: a length or a comparison, never a walk of the payload.
    """
    for field, want, low, high in LIMITS.get(kind, ()):
        value = payload.get(field)
        if value is None:
            continue
        if want is float:
            # bool is an int in Python and is not a sample rate.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{field} is {type(value).__name__}, expected a number"
            size = value
        else:
            if not isinstance(value, want):
                return f"{field} is {type(value).__name__}, expected {want.__name__}"
            size = len(value)
        if not low <= size <= high:
            return f"{field} is {size}, outside {low}-{high}"
    return None


class StationIngest:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._redis: aioredis.Redis | None = None
        # Identifies this worker's claim on the lease, so a renewal can check
        # the lease is still the one it took rather than one it inherited.
        self._token = uuid.uuid4().hex
        self._registry: dict[uuid.UUID, tuple[uuid.UUID | None, datetime]] = {}
        self._seen: dict[uuid.UUID, datetime] = {}
        #: Last config version written per station, so a health frame every
        #: half minute does not become a write every half minute.
        self._config_version: dict[uuid.UUID, int] = {}
        #: Last known synthetic/real state per station, so the row is written
        #: when it changes rather than on every health frame.
        self._simulated: dict[uuid.UUID, bool] = {}
        #: Last position written per station, so a health frame every half
        #: minute is not a database write every half minute.
        self._position: dict[uuid.UUID, tuple[float, float] | None] = {}
        #: Earliest monotonic time each station may cost another locality
        #: lookup. Set *before* the task is spawned, so it doubles as the
        #: in-flight guard: a second frame arriving mid-lookup is inside the
        #: cooldown and spawns nothing.
        self._geocode_after: dict[uuid.UUID, float] = {}
        #: Locality lookups in flight. Held so the event loop cannot garbage
        #: collect a running task, and so `stop` can cancel them.
        self._locality_tasks: set[asyncio.Task] = set()
        self._unknown_kinds: set[str] = set()
        self._unknown_stations: set[uuid.UUID] = set()
        #: Stations currently sending frames outside the contract's bounds,
        #: so the log says so once rather than at their publish rate.
        self._oversized: set[uuid.UUID] = set()

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
        # Locality lookups are best effort and can be mid-timeout, so shutdown
        # abandons them rather than waiting up to 8 seconds each on a name.
        for task in list(self._locality_tasks):
            task.cancel()
        self._locality_tasks.clear()
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
        # No video pattern. The station's periodic JPEG channel was removed
        # because two readers of one sensor wedged the camera
        # (station/gsu/video.py); live video goes over the media WebSocket
        # instead. Nothing has published here for some time, and subscribing to
        # it suggested otherwise.
        await pubsub.psubscribe(TELEMETRY_PATTERN, AUDIO_PATTERN, EVENTS_PATTERN)
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

    async def _handle_events(self, station_id: uuid.UUID, payload: dict) -> None:
        """Store a batch, then acknowledge it on the command channel.

        The acknowledgement is a *command* — `events.ack` is in
        `command.schema.json` like any other — so it needs no second path down
        to the station: it rides the socket the station is already holding.

        **Acknowledged only after the rows are committed, never on receipt.**
        Acking what is still in memory is how a crash silently loses a site's
        history: the station has been told it may delete, and it has.
        """
        org_id = await self._organization(station_id)
        if org_id is None:
            # No ack. An unknown or deactivated station keeps its events
            # locally and re-sends, which is the right outcome — the
            # alternative tells a box that may simply be waiting on an admin to
            # throw its history away.
            return

        through_seq = await asyncio.to_thread(
            self._store_events, org_id, station_id, payload)
        if through_seq is None or self._redis is None:
            return
        await self._redis.publish(command_channel(station_id), json.dumps({
            "kind": "events.ack", "through_seq": through_seq,
        }))

    def _store_events(self, org_id: uuid.UUID, station_id: uuid.UUID,
                      payload: dict) -> int | None:
        with PrivilegedSessionLocal() as db:
            return station_events.accept_batch(
                db, organization_id=org_id, station_id=station_id,
                payload=payload,
            )

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

        if stream == "events":
            await self._handle_events(station_id, payload)
            return

        kind = str(payload.get("kind", ""))
        if kind not in KNOWN_KINDS:
            if kind not in self._unknown_kinds:
                self._unknown_kinds.add(kind)
                log.info("Ignoring unknown telemetry kind %r from %s.",
                         kind, station_id)
            return

        oversized = _out_of_bounds(kind, payload)
        if oversized is not None:
            if station_id not in self._oversized:
                self._oversized.add(station_id)
                log.warning("Dropping %s from %s: %s.", kind, station_id, oversized)
            return
        self._oversized.discard(station_id)

        organization_id = await self._organization(station_id)
        if organization_id is None:
            return

        if kind == "health":
            await self._reconcile_simulated(station_id, payload)
            await self._reconcile_position(station_id, payload)
            await self._reconcile_config_version(station_id, payload)
            await self._cache_health(station_id, payload)
        elif kind == "adsb":
            await self._cache_adsb(station_id, payload)
        elif kind == "power":
            await self._cache_power(station_id, payload)

        # The Odin digest, for every kind it cares about. A dict assignment and
        # nothing more — see services/odin_digest.py on why this must not grow
        # an await: this line runs once per frame per station on the one loop
        # that carries the whole fleet, and it is the ceiling on how many
        # stations the platform holds.
        digest.note(station_id, kind, payload)

        # Onto the internal fan-out, where authorisation and per-subscriber
        # delivery already apply. Nothing downstream needs to know the frame
        # came from outside.
        await hub.publish_station(organization_id, station_id, stream, payload)
        await self._touch(station_id)

    async def _reconcile_config_version(
        self, station_id: uuid.UUID, payload: dict,
    ) -> None:
        """Believe the station about which config it is running.

        `ground_stations.config_version` was written by nothing at all. It
        defaulted to 1, was handed to the station at enrolment, read back in
        four API responses, and never moved — so the console reported "version
        1" about a station running whatever the setup page had been used to
        change, for ever.

        The station is the source, the same way it is for position and for
        `is_simulated`: site policy is typed on the box by somebody standing at
        it, because every threshold in it must work with the platform
        unreachable. This makes the platform's copy a *display* of that rather
        than a number nobody maintains.

        **This is deliberately not the other direction.** `contract/enrolment.md`
        §7 used to say the platform sends `config.set` when its version is
        newer, which could never happen and should not: the only settings the
        platform holds that the station also has are position and elevation,
        and `station/gsu/config.py` records the decision that those must not be
        settable from two ends. If the platform is ever given policy of its own
        to push — fleet-wide alert thresholds, say — that is the moment to build
        the push, and it needs a real answer to which side wins.

        Written only when it changes. Health arrives every half minute and this
        moves when somebody edits the setup page, not per frame.
        """
        version = payload.get("config_version")
        if not isinstance(version, int):
            return
        if self._config_version.get(station_id) == version:
            return
        try:
            await asyncio.to_thread(self._write_config_version, station_id, version)
        except Exception:  # noqa: BLE001 - a display field must not stop ingest
            log.exception("Could not record config version for %s.", station_id)
            return
        self._config_version[station_id] = version

    def _write_config_version(self, station_id: uuid.UUID, version: int) -> None:
        with PrivilegedSessionLocal() as db:
            db.execute(
                update(GroundStation)
                .where(GroundStation.id == station_id)
                .values(config_version=version)
            )
            db.commit()

    async def _cache_adsb(self, station_id: uuid.UUID, payload: dict) -> None:
        """Keep this station's latest aircraft list in Redis for the platform
        dashboard to aggregate across the fleet.

        ADS-B exists only on the live fan-out — nothing stores it — so a
        platform-wide view would otherwise have to subscribe to every station.
        Instead the one ingest leader drops the current list into a short-lived
        key (`adsb_snapshot_key`), TTL'd so a station that goes quiet stops
        contributing traffic that is no longer in the air. Best-effort: a failed
        write costs that station a tick on the fleet map, never a stall in
        ingest, which is why it is swallowed like the other display-only writes.
        """
        aircraft = payload.get("aircraft")
        if not isinstance(aircraft, list):
            return
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                adsb_snapshot_key(station_id),
                ADSB_SNAPSHOT_TTL,
                json.dumps({"aircraft": aircraft}),
            )
        except Exception:  # noqa: BLE001 - a cache write must never stop ingest
            log.debug("Could not cache ADS-B for %s.", station_id, exc_info=True)

    async def _cache_health(self, station_id: uuid.UUID, payload: dict) -> None:
        """Keep this station's latest health frame in Redis so the console's
        per-station stats view can read it without holding a live subscription —
        the same trick as `_cache_adsb`, TTL'd so a station gone quiet stops
        showing its last stats as current. Best-effort: a failed write costs a
        stats read, never a stall in ingest.
        """
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                health_snapshot_key(station_id),
                HEALTH_SNAPSHOT_TTL,
                json.dumps(payload),
            )
        except Exception:  # noqa: BLE001 - a cache write must never stop ingest
            log.debug("Could not cache health for %s.", station_id, exc_info=True)

    async def _cache_power(self, station_id: uuid.UUID, payload: dict) -> None:
        """Keep this station's latest power frame in Redis.

        Added for Odin: a wall showing every station at once needs a state of
        charge per tile, and on a solar site in a wilderness it is the number
        that decides whether anything else on the tile will still be true in six
        hours. Same trick, same TTL discipline and same best-effort failure as
        `_cache_health` — a failed write costs a tile reading, never a stall in
        ingest.
        """
        if self._redis is None:
            return
        try:
            await self._redis.setex(
                power_snapshot_key(station_id),
                POWER_SNAPSHOT_TTL,
                json.dumps(payload),
            )
        except Exception:  # noqa: BLE001 - a cache write must never stop ingest
            log.debug("Could not cache power for %s.", station_id, exc_info=True)

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
            elevation: float | None = None
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
            # Part of the same position and owned by the same person, so it is
            # stored with it. Optional: a station that cannot measure its
            # height omits it, and omitting is not the same as retracting, so
            # the stored value is left alone rather than nulled. Bounded to
            # the range a ground station can physically occupy.
            elevation = position.get("elevation_m")
            try:
                elevation = None if elevation is None else float(elevation)
            except (TypeError, ValueError):
                elevation = None
            if elevation is not None and not -500.0 <= elevation <= 9000.0:
                elevation = None
        else:
            return

        if self._position.get(station_id, "unset") == (fix, elevation):
            return
        self._position[station_id] = (fix, elevation)
        # The coordinates are a local write and are awaited. The locality is a
        # call to somebody else's service, so it is not: this coroutine reads
        # the pubsub for every station in every organisation, and awaiting an
        # 8-second timeout here would stop the platform's telemetry on one
        # station's say-so. It used to.
        await asyncio.to_thread(self._write_position, station_id, fix, elevation)
        if fix is not None and self._locality_due(station_id):
            task = asyncio.create_task(
                asyncio.to_thread(self._write_locality, station_id, fix)
            )
            self._locality_tasks.add(task)
            task.add_done_callback(self._locality_tasks.discard)

    def _locality_due(self, station_id: uuid.UUID) -> bool:
        """Whether this station may cost a locality lookup now.

        Reserves the slot as it answers, so this is also the in-flight guard -
        a lookup takes seconds and the cooldown is an hour.
        """
        now = time.monotonic()
        if now < self._geocode_after.get(station_id, 0.0):
            return False
        self._geocode_after[station_id] = now + GEOCODE_COOLDOWN_S
        return True

    def _write_position(
        self,
        station_id: uuid.UUID,
        fix: tuple[float, float] | None,
        elevation: float | None = None,
    ) -> None:
        """The coordinates, and nothing that needs the network.

        A stale locality beside a new position is the one inconsistency this
        allows, and it lasts as long as the lookup does. The alternative —
        writing both together — is what put an 8-second timeout inside the
        ingest loop, and a wrong town name is a smaller problem than a
        platform that stops carrying telemetry.
        """
        values: dict = {
            "latitude": fix[0] if fix else None,
            "longitude": fix[1] if fix else None,
        }
        if fix is None:
            # A retraction takes the height with it: an elevation with no
            # coordinates is not a position, it is a leftover.
            values |= {"locality": None, "region": None, "locality_for": None,
                       "elevation_m": None}
        elif elevation is not None:
            values["elevation_m"] = elevation
        try:
            with PrivilegedSessionLocal() as db:
                db.execute(
                    update(GroundStation)
                    .where(GroundStation.id == station_id)
                    .values(**values)
                )
                db.commit()
            log.info(
                "Station %s position %s.",
                station_id,
                f"set to {fix[0]:.5f}, {fix[1]:.5f}" if fix else "cleared by the station",
            )
        except Exception:
            log.exception("Could not update position for %s.", station_id)

    def _write_locality(
        self, station_id: uuid.UUID, fix: tuple[float, float]
    ) -> None:
        """Look the position up and name it. Off the ingest path, best effort.

        Writes only when the station is still at the position that prompted
        this: the lookup takes seconds, and a station that moved meanwhile has
        its own newer lookup coming.
        """
        try:
            with PrivilegedSessionLocal() as db:
                place = self._locality_for(db, station_id, fix)
                if not place:
                    return
                db.execute(
                    update(GroundStation)
                    .where(
                        GroundStation.id == station_id,
                        GroundStation.latitude == fix[0],
                        GroundStation.longitude == fix[1],
                    )
                    .values(**place)
                )
                db.commit()
        except Exception:
            log.exception("Could not resolve the locality for %s.", station_id)

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
        self, db, station_id: uuid.UUID, fix: tuple[float, float]
    ) -> dict:
        """Columns naming a position, or `{}` if the stored name already fits.

        Skips the network entirely when the position has not really changed,
        which is the normal case: a fixed site sends the same one for months.
        """
        wanted = self._fingerprint(fix)
        current = db.execute(
            select(GroundStation.locality_for).where(GroundStation.id == station_id)
        ).scalar_one_or_none()
        if current == wanted:
            return {}

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
