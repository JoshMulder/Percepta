"""Connection registry, subscribe-time authorisation, and fan-out.

Every decision that matters happens in `select_station` and `subscribe`. After
those, membership of a group *is* the permission (see groups.py), so the
publishing path performs no authorisation at all - by design, and only safe
because these two functions are the sole way into a group.

Fan-out crosses workers via Redis (bus.py) when REALTIME_BUS_ENABLED is on, which
it is by default. With it off the hub still works, but only within one process -
so WEB_CONCURRENCY must be 1, or subscribers on other workers silently miss
frames.

Revocation has two paths: an immediate Redis push (revocation.py) and the
`_revalidation_loop` sweep every STREAM_REVALIDATE_SECONDS as the backstop.
"""

import asyncio
import logging
import uuid

from sqlalchemy.orm import Session

from backend.auth.authorization import capabilities_for, visible_station_ids
from backend.auth.capabilities import Capability
from backend.core.config import settings
from backend.database.session import SessionLocal, set_request_org_context
from backend.realtime.connection import Connection
from backend.realtime.groups import GroupRegistry, station_group, status_group
from backend.repositories.auth_session_repository import AuthSessionRepository

log = logging.getLogger(__name__)

#: Which capability each subscribable stream requires. A stream absent from this
#: map cannot be subscribed to at all - unknown streams fail closed rather than
#: defaulting to something permissive.
STREAM_CAPABILITY: dict[str, Capability] = {
    "status": Capability.STATION_VIEW,
    "telemetry": Capability.TELEMETRY_VIEW,
    "video": Capability.VIDEO_VIEW,
    "audio": Capability.RADIO_LISTEN,
}


class AuthorizationError(Exception):
    """Refused. The message is deliberately vague on the wire - see endpoint.py."""


class Hub:
    def __init__(self) -> None:
        self.groups = GroupRegistry()
        self._connections: set[Connection] = set()
        self._revalidator: asyncio.Task | None = None
        self.bus = None  # set in start(); typed loosely to avoid a cycle

    # --- lifecycle ------------------------------------------------------

    async def register(self, conn: Connection) -> None:
        self._connections.add(conn)
        # The org status channel is joined at connect, not at station selection:
        # a user watching station A still has to learn that station B has
        # dropped or raised an alarm. Its membership is the set of stations the
        # user may see, so it is authorised per station too, just at the coarser
        # station.view capability.
        conn.visible_stations = await asyncio.to_thread(
            self._visible_stations, conn
        )
        await self._join(conn, status_group(conn.organization_id))

    async def unregister(self, conn: Connection) -> None:
        conn.closed = True
        groups = list(self.groups.groups_of(conn))
        self.groups.leave_all(conn)
        self._connections.discard(conn)
        await self._drop_bus_subscriptions(groups)

    def connection_count(self) -> int:
        return len(self._connections)

    # --- group membership, kept in step with the bus --------------------

    async def _join(self, conn: Connection, group: str) -> None:
        first_local_member = not self.groups.members(group)
        self.groups.join(conn, group)
        if self.bus is not None and first_local_member:
            await self.bus.ensure_subscribed(group)

    async def _leave(self, conn: Connection, group: str) -> None:
        self.groups.leave(conn, group)
        if self.bus is not None:
            await self.bus.drop_if_empty(group)

    async def _drop_bus_subscriptions(self, groups: list[str]) -> None:
        if self.bus is None:
            return
        for group in groups:
            await self.bus.drop_if_empty(group)

    # --- connection lookups, used by revocation -------------------------

    def connections_for_session(self, session_id: uuid.UUID) -> list[Connection]:
        return [c for c in self._connections if c.session_id == session_id]

    def connections_for_user(self, user_id: uuid.UUID) -> list[Connection]:
        return [c for c in self._connections if c.user_id == user_id]

    def connections_for_organization(self, org_id: uuid.UUID) -> list[Connection]:
        return [c for c in self._connections if c.organization_id == org_id]

    def connections_for_station(self, station_id: uuid.UUID) -> list[Connection]:
        """Connections pinned to the station, plus any that can merely see it -
        a deactivated station must disappear from the switcher and the status
        channel too, not only from whoever happened to be watching it."""
        return [
            c
            for c in self._connections
            if c.station_id == station_id or station_id in c.visible_stations
        ]

    def close_connection(self, conn: Connection, *, reason: str) -> None:
        conn.enqueue({"type": "revoked", "reason": reason})
        conn.closed = True

    # --- authorisation --------------------------------------------------

    def _visible_stations(self, conn: Connection) -> frozenset[uuid.UUID]:
        with SessionLocal() as db:
            self._bind_org(db, conn)
            return frozenset(
                visible_station_ids(
                    db,
                    user_id=conn.user_id,
                    organization_id=conn.organization_id,
                )
            )

    @staticmethod
    def _bind_org(db: Session, conn: Connection) -> None:
        """Every query this connection makes is org-scoped by row-level
        security, using the org pinned at connect - never one from a message."""
        set_request_org_context(
            db, organization_id=conn.organization_id, bypass=False
        )

    def _capabilities(self, conn: Connection, station_id: uuid.UUID) -> frozenset:
        with SessionLocal() as db:
            self._bind_org(db, conn)
            return capabilities_for(
                db,
                user_id=conn.user_id,
                organization_id=conn.organization_id,
                ground_station_id=station_id,
            )

    async def select_station(
        self, conn: Connection, station_id: uuid.UUID
    ) -> frozenset[Capability]:
        """Pin a station to this connection.

        Refuses unless the user holds at least station.view there. Anything the
        connection was subscribed to for a previous station is dropped in the
        same operation - not afterwards, or the connection would briefly hold
        two stations and the one-station-at-a-time property would not be true.
        """
        capabilities = await asyncio.to_thread(self._capabilities, conn, station_id)
        if Capability.STATION_VIEW not in capabilities:
            raise AuthorizationError("station not available")

        dropped = self.groups.leave_matching(conn, f"org:{conn.organization_id}:gsu:")
        await self._drop_bus_subscriptions(dropped)

        conn.station_id = station_id
        conn.capabilities = capabilities
        return capabilities

    async def subscribe(self, conn: Connection, stream: str) -> str:
        """Join the group for `stream` on the pinned station.

        Note the client does not name a station: it is already pinned, so a
        connection cannot ask for one it was never authorised into.
        """
        if conn.station_id is None:
            raise AuthorizationError("no station selected")

        capability = STREAM_CAPABILITY.get(stream)
        if capability is None:
            raise AuthorizationError("unknown stream")

        # Re-read rather than trusting the cache from selection time. Cheap
        # relative to how long a subscription lasts, and it closes the window
        # between selecting a station and subscribing to it.
        capabilities = await asyncio.to_thread(
            self._capabilities, conn, conn.station_id
        )
        conn.capabilities = capabilities
        if capability not in capabilities:
            raise AuthorizationError("not permitted")

        group = station_group(conn.organization_id, conn.station_id, stream)
        await self._join(conn, group)
        return group

    async def unsubscribe(self, conn: Connection, stream: str) -> None:
        if conn.station_id is None:
            return
        await self._leave(
            conn, station_group(conn.organization_id, conn.station_id, stream)
        )

    # --- fan-out --------------------------------------------------------

    def deliver_local(self, group: str, message: dict) -> int:
        """Deliver to this worker's members of `group`. Returns the count.

        No authorisation here on purpose: membership was authorised at subscribe
        time and is the permission. That is only true because groups.py offers
        no way to reach a connection that did not join.

        The one extra filter is the org status channel. Its members are every
        connection in the org, but each may see a different set of stations, so
        visibility is applied per recipient at delivery. That filter has to run
        on the worker holding the connection - it cannot be baked into the
        message - which is why status frames cross the bus to the whole org
        group and are narrowed here.
        """
        members = self.groups.members(group)
        if message.get("type") == "status":
            try:
                station_id = uuid.UUID(str(message.get("station_id")))
            except (TypeError, ValueError):
                return 0
            sent = 0
            for conn in members:
                if station_id in conn.visible_stations:
                    conn.enqueue(message)
                    sent += 1
            return sent

        for conn in members:
            conn.enqueue(message)
        return len(members)

    async def publish(self, group: str, message: dict) -> int:
        """Fan out across every worker when the bus is on, locally otherwise.

        With the bus enabled this does *not* also deliver locally - the frame
        comes back through this worker's own bus subscription, so delivering
        here as well would send it twice.
        """
        if self.bus is not None:
            await self.bus.publish(group, message)
            return -1
        return self.deliver_local(group, message)

    @staticmethod
    def station_message(station_id: uuid.UUID, stream: str, payload: dict) -> dict:
        return {
            "type": "event",
            "stream": stream,
            "station_id": str(station_id),
            "payload": payload,
        }

    @staticmethod
    def status_message(station_id: uuid.UUID, payload: dict) -> dict:
        return {"type": "status", "station_id": str(station_id), "payload": payload}

    async def publish_station(
        self,
        organization_id: uuid.UUID,
        station_id: uuid.UUID,
        stream: str,
        payload: dict,
    ) -> int:
        return await self.publish(
            station_group(organization_id, station_id, stream),
            self.station_message(station_id, stream, payload),
        )

    async def publish_status(
        self, organization_id: uuid.UUID, station_id: uuid.UUID, payload: dict
    ) -> int:
        """Low-rate org-wide channel: station online/offline, alarms, health.

        The one place a connection legitimately hears about a station other than
        its pinned one, and it carries nothing but status.
        """
        return await self.publish(
            status_group(organization_id), self.status_message(station_id, payload)
        )

    # --- revocation -----------------------------------------------------

    def _revalidate_one(self, conn: Connection) -> tuple[bool, frozenset]:
        """(still_valid, capabilities). Runs on a worker thread."""
        with SessionLocal() as db:
            session = AuthSessionRepository(db).get_active(
                session_id=conn.session_id
            )
            if session is None:
                return False, frozenset()
            self._bind_org(db, conn)
            conn.visible_stations = frozenset(
                visible_station_ids(
                    db,
                    user_id=conn.user_id,
                    organization_id=conn.organization_id,
                )
            )
            if conn.station_id is None:
                return True, frozenset()
            return True, capabilities_for(
                db,
                user_id=conn.user_id,
                organization_id=conn.organization_id,
                ground_station_id=conn.station_id,
            )

    async def revalidate(self, conn: Connection) -> bool:
        """Re-check one connection. Returns False if it should be closed.

        Drops any subscription whose capability has gone away, so revoking
        radio.listen actually stops the audio rather than merely preventing the
        next subscribe.
        """
        valid, capabilities = await asyncio.to_thread(self._revalidate_one, conn)
        if not valid:
            return False

        previous, conn.capabilities = conn.capabilities, capabilities
        if previous == capabilities or conn.station_id is None:
            return True

        lost = previous - capabilities
        for stream, capability in STREAM_CAPABILITY.items():
            if capability in lost:
                await self.unsubscribe(conn, stream)
        if Capability.STATION_VIEW in lost:
            # Lost the station entirely - drop everything for it and unpin.
            dropped = self.groups.leave_matching(
                conn, f"org:{conn.organization_id}:gsu:"
            )
            await self._drop_bus_subscriptions(dropped)
            conn.station_id = None
            conn.enqueue({"type": "station_revoked", "reason": "access changed"})
        return True

    async def revalidate_all(self) -> None:
        for conn in list(self._connections):
            if conn.closed:
                continue
            try:
                if not await self.revalidate(conn):
                    conn.enqueue({"type": "revoked", "reason": "session ended"})
                    conn.closed = True
            except Exception:
                log.exception("Revalidation failed for a connection; closing it.")
                conn.closed = True

    async def _revalidation_loop(self) -> None:
        """The backstop. Redis push (revocation.py) is the fast path; this
        bounds the worst case if a push is ever missed, which push alone cannot
        do because a process that misses a message fails silently."""
        while True:
            await asyncio.sleep(settings.stream_revalidate_seconds)
            try:
                await self.revalidate_all()
            except Exception:
                log.exception("Revalidation sweep failed.")

    async def start(self) -> None:
        if settings.realtime_bus_enabled and self.bus is None:
            from backend.realtime.bus import RedisBus

            bus = RedisBus(self)
            try:
                await bus.start()
                self.bus = bus
            except Exception:
                # Starting without the bus is a degraded but working single-worker
                # deployment, so this is a loud warning rather than a hard failure.
                # It is not safe with WEB_CONCURRENCY > 1, hence the wording.
                log.exception(
                    "Realtime bus failed to start - fan-out is THIS WORKER ONLY "
                    "and revocation is poll-only. Do not run multiple workers."
                )
        if self._revalidator is None:
            self._revalidator = asyncio.create_task(self._revalidation_loop())

    async def stop(self) -> None:
        if self._revalidator is not None:
            self._revalidator.cancel()
            try:
                await self._revalidator
            except asyncio.CancelledError:
                pass
            self._revalidator = None
        if self.bus is not None:
            await self.bus.stop()
            self.bus = None


hub = Hub()
