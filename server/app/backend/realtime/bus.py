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
    # Slash-separated to match contract/transport.md, and because the intended
    # production transport is MQTT where this is a topic path. Redis does not
    # care; the contract does.
    return f"cmd/gsu/{station_id}"


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

        if channel.startswith("rt:g:"):
            group = channel[len("rt:g:") :]
            # Local delivery only. The group's membership was authorised at
            # subscribe time on this worker; a frame arriving from another
            # process does not re-open that decision.
            self.hub.deliver_local(group, payload)
