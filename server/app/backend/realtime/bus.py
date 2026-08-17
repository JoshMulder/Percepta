"""Cross-process fan-out and revocation over Redis.

Without this the hub delivers only to connections held by its own worker, so
more than one uvicorn worker would silently serve a subset of the people
entitled to the data.

Channel design - one Redis channel per group, not one shared firehose:

    rt:g:{group_name}   frames for exactly that group
    rt:revoke           revocation events, every worker

A worker subscribes to a group's channel only while it actually holds an
authorised local subscriber, and unsubscribes when the last one leaves. So a
worker serving only org B never receives a single byte of org A's video, rather
than receiving it and discarding it. A shared channel would have been simpler
and would have quietly made every worker a place where every tenant's frames
are present in memory; that is a poor trade on this platform.

Publishing is deliberately split:

  * receiving is async, on the event loop, in this module's reader task.
  * publishing is sync (`publish_sync`), so ordinary request and repository code
    can raise a revocation without needing an event loop or an await. It is
    fire-and-forget on a tiny payload.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import redis
import redis.asyncio as aioredis

from backend.core.config import settings
from backend.services import station_topics

if TYPE_CHECKING:
    from backend.realtime.hub import Hub

log = logging.getLogger(__name__)

REVOKE_CHANNEL = "rt:revoke"


def group_channel(group: str) -> str:
    return f"rt:g:{group}"


def redacted_url(url: str) -> str:
    """A connection URL safe to log.

    The broker URL carries a password, and a log line is the easiest place in
    the system for a secret to end up somewhere it was never meant to be - a
    terminal, a bug report, a log aggregator with wider access than the host.
    Nothing needs the password to understand a log line; the host and scheme
    are the useful part.
    """
    import re

    return re.sub(r"://([^/@]*):([^/@]*)@", r"://\1:***@", url)


def url_without_credentials(url: str) -> str:
    """Strip any embedded username and password from a connection URL.

    Needed because of a trap in redis-py: `ConnectionPool.from_url` ends with
    `kwargs.update(url_options)`, so **the URL wins over the keyword
    arguments**. Connecting as a station with

        Redis.from_url(settings.redis_url, username=..., password=...)

    silently authenticates as whoever the URL names instead - which, now that
    the platform's own URL carries a password, means the station credential is
    ignored and the connection either fails confusingly or succeeds with far
    more privilege than intended. Both are worse than an error.

    Anything connecting as somebody else must pass a credential-free URL.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def command_channel(station_id) -> str:
    """Outbound commands for one ground station.

    Per station, not shared: the onboard computer subscribes to exactly its own
    channel, so a broker ACL can pin it there and a compromised station cannot
    listen to commands meant for another org's hardware.
    """
    return station_topics.command(station_id)


_sync_client: redis.Redis | None = None


def _get_sync_client() -> redis.Redis | None:
    """Lazily-built publish-only client for sync callers.

    Never raises to the caller: failing to publish a fan-out frame should not
    take down the request that produced it. Revocation is different and says so
    at its call site - the poll-based sweep is the backstop there.
    """
    global _sync_client
    if not settings.realtime_bus_enabled:
        return None
    if _sync_client is None:
        try:
            _sync_client = redis.Redis.from_url(
                settings.redis_url, socket_connect_timeout=2, socket_timeout=2
            )
        except Exception:
            log.exception("Could not build the Redis publish client.")
            return None
    return _sync_client


def publish_sync(channel: str, payload: dict[str, Any]) -> bool:
    client = _get_sync_client()
    if client is None:
        return False
    try:
        client.publish(channel, json.dumps(payload))
        return True
    except Exception:
        log.warning("Redis publish to %s failed.", channel, exc_info=True)
        return False


#: How long a station's last ADS-B snapshot is worth reading. ADS-B is only
#: meaningful live, so a fix from a station that has since gone quiet must age
#: out rather than linger on the platform map as traffic that is no longer there.
ADSB_SNAPSHOT_TTL = 45


def adsb_snapshot_key(station_id) -> str:
    """Redis key holding one station's most recent ADS-B aircraft list.

    The platform dashboard aggregates ADS-B across the whole fleet, but the
    telemetry only exists on the live per-station WebSocket fan-out — there is no
    stored copy. Rather than have the dashboard hold a subscription to every
    station, the ingest writes each station's latest list here (TTL'd), and the
    dashboard reads the set in one shot. Fleet-wide, not per-viewer."""
    return f"latest:adsb:{station_id}"


#: How long a station's last health snapshot is worth reading. Health arrives
#: about every 30 s, so this survives a missed frame or two but ages out a
#: station that has gone quiet, so its stats stop reading as current.
HEALTH_SNAPSHOT_TTL = 120


def health_snapshot_key(station_id) -> str:
    """Redis key holding one station's most recent health frame. Health exists
    only on the live per-station fan-out — nothing stores it — so the console's
    per-station stats view reads the ingest-cached copy here rather than holding
    a subscription, exactly as the fleet map does for ADS-B. TTL'd so a station
    gone quiet ages out instead of showing its last stats as current."""
    return f"latest:health:{station_id}"


#: Weather is published at 0.2 Hz, so a frame is at most five seconds old and a
#: gap of two minutes means the head has stopped rather than that the wind has.
#: Longer than the power TTL for that reason: a slower sensor needs a wider
#: window before its silence means anything.
WEATHER_SNAPSHOT_TTL = 150


def weather_snapshot_key(station_id) -> str:
    """Redis key holding one station's most recent weather frame.

    Added so the wall's POLLED feed can show weather at all. The pushed digest
    reads weather live off the ingest hot path, and if only that path had it,
    the wall would show wind and visibility while its socket was up and drop
    them the moment it fell back to polling — the exact asymmetry
    `services/station_vitals` exists to make impossible, arriving from the other
    direction.

    Cached rather than read from `weather_samples`, for the same reason power is:
    that table is a downsampled HISTORY written by the recorder, and reading its
    newest row per station per poll is a query to learn something a cache
    already holds.
    """
    return f"percepta:weather:{station_id}"


#: Power is published once a second, so a stale frame is worthless and a short
#: TTL is the honest failure: an Odin tile with no battery reading says "unknown"
#: rather than a number from twenty minutes ago. Longer than health's cadence
#: gap, short enough that a station gone quiet stops claiming a state of charge.
POWER_SNAPSHOT_TTL = 90


def power_snapshot_key(station_id) -> str:
    """Redis key holding one station's most recent power frame.

    Power, like health, lives only on the live per-station fan-out — the
    power_samples table is a minute-resolution HISTORY, written by the recorder,
    and reading the newest row from it to fill a tile would be a query per
    station per poll to learn something a cache already knows.

    Odin's wall needs a state of charge for every station at once without
    subscribing to any of them. That is the same problem health and ADS-B solved,
    so it gets the same answer rather than a new one.
    """
    return f"percepta:power:{station_id}"


#: How long a poster is worth showing. Posters arrive once a minute while
#: somebody is watching, so this survives two missed captures — a station
#: restarting its camera, a retried upload — and then the tile goes back to its
#: placeholder. That is the honest answer: a five-minute-old picture of a place
#: is not what the site looks like now, and a wall exists to be believed.
POSTER_TTL = 180


def poster_key(station_id) -> str:
    """Redis key holding one station's most recent camera still.

    In Redis rather than on disk or in the database for the reason every other
    snapshot here is: ANY worker must be able to serve it. The live video relay
    in `api/media.py` holds its frames in process memory, which is precisely why
    that path cannot run behind more than one worker — the poster path is the
    deliberate opposite, and a wall of twenty-four tiles is only affordable
    because of it.

    Not in the database: a JPEG a minute per watched station is a write rate no
    row wants, for data whose whole value expires in three minutes.
    """
    return f"percepta:poster:{station_id}"


def poster_stamp_key(station_id) -> str:
    """When this station's poster was captured — the JPEG's timestamp, alone.

    A SEPARATE KEY FROM THE PICTURE, and that is the whole reason it exists. The
    wall's digest needs to tell each tile that its poster changed, for up to
    twenty-four stations at a time; if the stamp lived with the image, building
    one digest would mean dragging every JPEG in the fleet out of Redis to read
    a date off each. This key is about thirty bytes and rides along in the
    digest's existing MGET with power and weather.

    The tile then asks for the image itself with `?v=<stamp>` — an `<img>` shows
    no response headers to the page, and a stable `src` is never re-fetched, so
    without a changing URL a tile would hold its first picture until the page
    was reloaded.
    """
    return f"percepta:poster_at:{station_id}"


def write_poster_sync(key: str, jpeg: bytes, *, ttl: int = POSTER_TTL) -> bool:
    """Store one station's poster. Binary-safe, fail-soft.

    BINARY-SAFE MATTERS HERE. Every other value on this bus is JSON, and the
    client is built WITHOUT `decode_responses`, so bytes go in and come back out
    untouched. If anyone ever adds that flag for convenience, this is what
    breaks and it will break as a corrupt image rather than an exception — hence
    this note next to the only binary value in the system.

    SETEX, not SET-then-EXPIRE: one round trip, and no window in which a poster
    exists with no expiry at all. A key that missed its EXPIRE would pin a
    station's last picture in Redis for ever and show it as current.
    """
    client = _get_sync_client()
    if client is None:
        return False
    try:
        client.setex(key, ttl, jpeg)
        return True
    except Exception:
        log.warning("Redis poster write to %s failed.", key, exc_info=True)
        return False


def read_latest_sync(keys: list[str]) -> list:
    """MGET a batch of snapshot keys for a sync caller. Empty on any failure — a
    dashboard read must not raise because Redis hiccuped, the same fail-soft
    posture as the publish path above."""
    client = _get_sync_client()
    if client is None or not keys:
        return []
    try:
        return client.mget(keys)
    except Exception:
        log.warning("Redis mget of %d keys failed.", len(keys), exc_info=True)
        return []


def publish_roster_sync(organization_id) -> bool:
    """Nudge every console in the org to re-pull its station list, because a
    station was just created, deleted or renamed.

    On the org status channel, but a `roster` type rather than `status`: `status`
    is filtered per recipient by which stations they may see, and a station that
    has only just been created is in nobody's snapshot yet — so the very event we
    want would be dropped. `roster` is contentless (it carries no station data),
    and each console answers it by re-fetching the list through the authorised
    endpoint, so fanning it out org-wide leaks nothing. Fire-and-forget, like
    every other sync publish here."""
    from backend.realtime.groups import status_group

    return publish_sync(status_group(organization_id), {"type": "roster", "payload": {}})


class RedisBus:
    """The receiving half: one pubsub connection, one reader task per worker."""

    def __init__(self, hub: "Hub", url: str | None = None) -> None:
        self.hub = hub
        self.url = url or settings.redis_url
        self._redis: aioredis.Redis | None = None
        self._pubsub: Any = None
        self._reader: asyncio.Task | None = None
        self._subscribed: set[str] = set()
        self._running = False

    async def start(self) -> None:
        self._redis = aioredis.Redis.from_url(self.url)
        self._pubsub = self._redis.pubsub()
        # Subscribe to revocation first so the pubsub connection is never empty -
        # get_message() on a pubsub with no subscriptions is not useful.
        await self._pubsub.subscribe(REVOKE_CHANNEL)
        # And the Odin wall. Subscribed unconditionally, like revocation, rather
        # than on demand: it is ONE channel carrying one frame every few seconds
        # for the entire product, and making it conditional would add a
        # subscribe/unsubscribe dance to save nothing measurable.
        from backend.realtime.odin import WALL_CHANNEL

        await self._pubsub.subscribe(WALL_CHANNEL)
        self._running = True
        self._reader = asyncio.create_task(self._read_loop())
        log.info("Realtime bus connected (%s).", redacted_url(self.url))

    async def stop(self) -> None:
        self._running = False
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    # --- group subscription ---------------------------------------------

    async def ensure_subscribed(self, group: str) -> None:
        """Called when a group gains its first local member."""
        if self._pubsub is None:
            return
        channel = group_channel(group)
        if channel in self._subscribed:
            return
        await self._pubsub.subscribe(channel)
        self._subscribed.add(channel)

    async def drop_if_empty(self, group: str) -> None:
        """Called when a group loses a member; unsubscribes only once no local
        member remains, so this worker stops receiving data it cannot deliver."""
        if self._pubsub is None:
            return
        if self.hub.groups.members(group):
            return
        channel = group_channel(group)
        if channel not in self._subscribed:
            return
        try:
            await self._pubsub.unsubscribe(channel)
        finally:
            self._subscribed.discard(channel)

    # --- publishing ------------------------------------------------------

    async def publish(self, group: str, message: dict) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.publish(group_channel(group), json.dumps(message))
        except Exception:
            log.warning("Redis fan-out publish failed for %s.", group, exc_info=True)

    # --- receiving -------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._pubsub is not None
        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Realtime bus reader errored; retrying shortly.")
                await asyncio.sleep(1.0)
                continue

            if message is None:
                continue
            try:
                await self._dispatch(message)
            except Exception:
                log.exception("Failed to dispatch a realtime bus message.")

    async def _dispatch(self, message: dict) -> None:
        channel = message.get("channel")
        if isinstance(channel, bytes):
            channel = channel.decode()
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode()
        if not channel or not isinstance(data, str):
            return

        try:
            payload = json.loads(data)
        except ValueError:
            log.warning("Ignoring non-JSON payload on %s.", channel)
            return

        if channel == REVOKE_CHANNEL:
            from backend.realtime.revocation import apply_revocation

            await apply_revocation(self.hub, payload)
            return

        from backend.realtime.odin import WALL_CHANNEL, wall

        if channel == WALL_CHANNEL:
            # Straight to this worker's own wall sockets. Not through the hub:
            # a wall connection has no group membership by construction, which
            # is what stops the cross-tenant fan-out and the per-tenant one
            # sharing a mechanism they could ever be confused between.
            wall.broadcast(payload)
            return

        if channel.startswith("rt:g:"):
            group = channel[len("rt:g:") :]
            # Local delivery only. The group's membership was authorised at
            # subscribe time on this worker; a frame arriving from another
            # process does not re-open that decision.
            self.hub.deliver_local(group, payload)
