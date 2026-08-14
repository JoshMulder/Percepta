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

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.authorization import capabilities_for, visible_station_ids
from backend.auth.capabilities import Capability
from backend.core.config import settings
from backend.database.session import SessionLocal, set_request_org_context
from backend.database.models.ground_station import GroundStation
from backend.repositories.organization_membership_repository import (
    OrganizationMembershipRepository,
)
from backend.realtime.connection import Connection
from backend.realtime.groups import GroupRegistry, station_group, status_group
from backend.repositories.auth_session_repository import AuthSessionRepository
from backend.services import audit

log = logging.getLogger(__name__)

#: How many channels one operator may guard at once.
#:
#: Not an arbitrary tidiness limit. Every guarded channel is an independent Opus
#: stream into ONE send queue that drops oldest when full, so the cost of an
#: extra channel is paid by the channels already there. Eight is also about the
#: limit of what a person can actually monitor — past that the strip is
#: decoration, and decoration that degrades the audio it decorates.
WATCH_MAX = 8

#: Which capability each subscribable stream requires. A stream absent from this
#: map cannot be subscribed to at all - unknown streams fail closed rather than
#: defaulting to something permissive.
STREAM_CAPABILITY: dict[str, Capability] = {
    "status": Capability.STATION_VIEW,
    "telemetry": Capability.TELEMETRY_VIEW,
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
        """Connections belonging to the org, PLUS any watch guarding one of its
        stations.

        The second half is the one that matters and the one that was missing.
        Odin crosses tenant boundaries by design, so the levers a TENANT pulls to
        stop being watched — deactivating a station, deactivating the org — must
        reach a connection that does not belong to them. Without this branch a
        tenant's stop lever would miss, and the listen would continue until the
        next revalidation sweep noticed, up to a minute later.
        """
        watching = [
            c
            for c in self._connections
            if c.watch and org_id in self._watch_orgs(c)
        ]
        direct = [c for c in self._connections if c.organization_id == org_id]
        return list({id(c): c for c in direct + watching}.values())

    def _watch_orgs(self, conn: Connection) -> set:
        """The organisations a watch connection is reaching into.

        Read from the groups it actually joined rather than from a cache: the
        group name carries the org, it was resolved from the station row on a
        privileged session when the guard was granted, and it is therefore the
        one place this cannot drift from what the connection can really hear.
        """
        orgs: set = set()
        for group in self.groups.groups_of(conn):
            parts = group.split(":")
            if len(parts) >= 3 and parts[0] == "org":
                try:
                    orgs.add(uuid.UUID(parts[1]))
                except ValueError:
                    continue
        return orgs

    def connections_for_station(self, station_id: uuid.UUID) -> list[Connection]:
        """Connections pinned to the station, plus any that can merely see it -
        a deactivated station must disappear from the switcher and the status
        channel too, not only from whoever happened to be watching it."""
        return [
            c
            for c in self._connections
            # `or station_id in c.watch` is not a convenience. A watch
            # connection has station_id=None and an empty visible set in the
            # platform org, so without it a tenant deactivating a station would
            # NOT push-stop a cross-tenant listen — it would wait for the sweep.
            # For the one feature that crosses tenants by design, the tenant's
            # own stop levers are precisely the ones that were missing.
            if c.station_id == station_id
            or station_id in c.visible_stations
            or station_id in c.watch
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

    async def watch_join(self, conn: Connection, station_id: uuid.UUID) -> str:
        """Guard one station's airband on an Odin watch.

        Joins the TENANT'S OWN existing audio group — `org:{their org}:gsu:{their
        station}:audio` — rather than anything new. Three things follow from that
        and every one of them is the reason to do it this way:

          - Lease renewal needs no new code. `groups.stations_subscribed_to`
            parses group NAMES positionally, so a watch connection sitting in the
            real audio group is already counted by audio_demand.renew. No union,
            no second registry, no parallel lease path to fall out of step.
          - There is only ever one audio fan-out per station, so a watch cannot
            receive something a tenant's own console would not.
          - The org in the group name is resolved HERE, from the station row on a
            privileged session, and never from anything the client said. That is
            the whole cross-tenant guard: a client that could name its own org
            could name somebody else's.

        Deliberately does NOT join :telemetry. That group is undifferentiated —
        one stream carries adsb, power, radio, light, weather and health — so
        joining it to light a squelch lamp would drag that site's full ADS-B at
        1 Hz, priced at 68-153 kbit/s continuously against 30-36 kbit/s of
        squelch-gated Opus. Worse, with a drop-oldest queue, twelve channels of
        aircraft positions would evict Opus packets under any stall and the
        operator would hear a clipped over that is indistinguishable from a quiet
        channel. Guarding MORE channels would silently make the radio worse.

        The talk light is free without it: the station builds an audio payload
        only while its gate is open and publishes only when audio is wanted, so a
        frame ARRIVING on a channel is the gate, at 125 ms granularity.
        """
        from backend.auth.odin import odin_capabilities_for

        def _resolve() -> tuple[uuid.UUID | None, frozenset]:
            with SessionLocal() as db:
                # The role, re-read, on EVERY join rather than once at connect.
                # A socket outlives a shift: an operator taken off the rota still
                # holds an open connection, and the revalidation sweep empties
                # their guard set but cannot stop them asking for another. This
                # is the door, so it is checked at the door — one indexed row,
                # against a message a person sends by clicking a channel.
                #
                # Asked FIRST, and asked under the connection's own org with
                # bypass off — the ordinary context every other query here runs
                # in. The elevation below happens only after this has passed.
                self._bind_org(db, conn)
                if not self._is_watch_staff(db, conn):
                    return None, frozenset()

                # RLS BYPASSED, DELIBERATELY, AND ONLY HERE.
                #
                # An org-scoped read cannot see another tenant's station — that
                # is the whole point of the policy, and the default context is an
                # empty org under which it matches nothing, so this fails CLOSED
                # rather than open. Reading across the boundary has to be asked
                # for, and this is the one place in the socket layer that asks.
                #
                # Narrow in both directions: it is reached only past the role
                # check above, and the session is closed two statements later —
                # the pool's checkin wipes the org context, so the elevation
                # cannot be inherited by whatever borrows the connection next.
                # Anything added below these two reads must put it back first.
                set_request_org_context(
                    db, organization_id=conn.organization_id, bypass=True
                )
                caps = odin_capabilities_for(db, station_id=station_id)
                if not caps:
                    return None, frozenset()
                org = db.execute(
                    select(GroundStation.organization_id).where(
                        GroundStation.id == station_id
                    )
                ).scalar_one_or_none()
                return org, caps

        organization_id, capabilities = await asyncio.to_thread(_resolve)
        if organization_id is None or Capability.RADIO_LISTEN not in capabilities:
            raise AuthorizationError("station not available")

        group = station_group(organization_id, station_id, "audio")
        await self._join(conn, group)
        conn.watch.add(station_id)

        # Ask the station to start sending now, rather than waiting up to a
        # renewal period for the sweep to notice a new listener. Without this the
        # first over on a freshly guarded channel is simply missed, which is the
        # over an operator guarded it for.
        try:
            from backend.services import audio_demand

            audio_demand.request(station_id)
        except Exception:  # noqa: BLE001 - a missed first over, not a failure
            log.debug("Could not prompt audio for %s.", station_id, exc_info=True)

        # WRITTEN TO THE WATCHED TENANT'S ORG, not to the platform's.
        #
        # This is the disclosure. A customer can be listened to by staff they
        # have never met, over a link they cannot see, and the only honest place
        # for that fact is the audit trail they can already read. Filing it under
        # the platform org would make it a record we keep about ourselves, which
        # is not the same thing and is not what it is for.
        #
        # One row per JOIN, not per frame: an over is not an event worth a row,
        # and a row per over would bury the fact that anyone was listening at all
        # under the evidence of what they heard.
        await asyncio.to_thread(
            audit.record,
            action="odin.watch.join",
            organization_id=organization_id,
            actor_user_id=conn.user_id,
            target_type="ground_station",
            target_id=str(station_id),
            ground_station_id=station_id,
        )
        return group

    async def watch_leave(self, conn: Connection, station_id: uuid.UUID) -> None:
        """Stop guarding one station.

        Leaves only the group for THIS station, so unguarding one channel cannot
        silence the rest of the watch. The lease stops on its own: nobody renews
        it, and silence is what the station listens for.
        """
        conn.watch.discard(station_id)
        for group in list(self.groups.groups_of(conn)):
            if group.endswith(f":gsu:{station_id}:audio"):
                await self._leave(conn, group)

    async def watch_set(
        self, conn: Connection, station_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Make the guard set exactly `station_ids`. Returns what is guarded.

        ONE message carrying the WHOLE set, rather than a pair of add/remove
        messages, and the reason is reconnection. A watch is a shift-long thing
        on a screen nobody is looking at closely; sockets drop and come back. If
        the client replayed "guard this" per channel, a reconnect that lost a
        message would leave the server guarding something the operator can no
        longer see on their strip — audio with no lamp, and no way to stop it.
        Sending the set makes reconnect and toggling the same operation, and
        makes it idempotent: the client's picture always wins.

        Over the cap, EXTRAS ARE REFUSED rather than the whole message. An
        operator who ends up over the limit keeps the channels they had.
        """
        wanted = list(dict.fromkeys(station_ids))[:WATCH_MAX]
        current = set(conn.watch)

        for station_id in current - set(wanted):
            await self.watch_leave(conn, station_id)
        for station_id in wanted:
            if station_id in conn.watch:
                continue
            try:
                await self.watch_join(conn, station_id)
            except AuthorizationError:
                # One unavailable station does not fail the rest of the set. The
                # client is told what IS guarded and reconciles from that, which
                # is the same path a revocation takes.
                continue
        return set(conn.watch)

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
        # Audio is sent only while somebody is listening — 21.7 kbit/s of Opus
        # while an over lasts (this comment said 512 kbit/s until the encoder
        # landed; see services/audio_demand.py). Asked here as well as by the
        # renewal loop, or the first listener waits up to RENEW_SECONDS to hear
        # anything — which on airband is indistinguishable from a quiet
        # channel.
        #
        # Imported inside the function: services.audio_demand imports this
        # module, and the cycle at import time is not worth restructuring two
        # files to avoid for one call.
        if stream == "audio":
            from backend.services import audio_demand

            audio_demand.request(conn.station_id)
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

    @staticmethod
    def _is_watch_staff(db: Session, conn: Connection) -> bool:
        """Is this connection still entitled to a cross-tenant watch?

        Read from the platform membership ROW, every time, rather than trusted
        from the identity minted when the socket opened. A watch position lasts a
        shift and a socket outlives a great deal: taking somebody off the rota
        has to stop the audio they are already hearing, not merely the next
        session they start.

        Two conditions, and both are needed. The connection's pinned org must BE
        the platform org — a platform admin who has descended into a customer's
        tenant is acting as that tenant and has no business reaching sideways out
        of it — and the row must carry a watch role.
        """
        from backend.auth.platform import PLATFORM_ORGANIZATION_ID, WATCH_ROLES

        if conn.organization_id != PLATFORM_ORGANIZATION_ID:
            return False
        membership = OrganizationMembershipRepository(db).get(
            user_id=conn.user_id, organization_id=PLATFORM_ORGANIZATION_ID
        )
        return membership is not None and bool(
            WATCH_ROLES.intersection(membership.roles or [])
        )

    def _revalidate_watch(self, db: Session, conn: Connection) -> frozenset:
        """Re-check a whole guard set, and return the stations it lost.

        TWO questions, and both of them are somebody's stop lever:

          - Is this operator still watch staff? Their platform membership row
            can be stripped mid-shift, and if that only took effect at the next
            reconnect then removing somebody from the watch rota would not
            actually stop them listening. The socket's org was pinned at connect
            and cannot answer this; the row can.
          - Is each guarded station still active? Deactivating a station is how
            a TENANT stops being listened to, and they must not have to ask us.

        Checked as a SET, in one statement each. Asking per station would
        multiply an already-serial sweep by every operator's guard count for a
        question that is one row apiece.
        """
        from backend.auth.odin import odin_capabilities_for_all

        was = frozenset(conn.watch)

        if not self._is_watch_staff(db, conn):
            conn.watch = set()
            return was

        # RLS BYPASSED, DELIBERATELY AND NARROWLY. The guarded stations belong to
        # other tenants, so an org-scoped read returns nothing — which would drop
        # every guard on the first sweep and look exactly like the watch working
        # until an operator noticed the wall had gone quiet.
        set_request_org_context(
            db, organization_id=conn.organization_id, bypass=True
        )
        try:
            allowed = odin_capabilities_for_all(db, station_ids=list(conn.watch))
            conn.watch = {sid for sid, caps in allowed.items() if caps}
        finally:
            # Put it back before anything else runs on this session. The session
            # closes immediately today; leaving an elevated context behind for
            # the next line of code somebody adds here is how that stops being
            # true quietly.
            set_request_org_context(
                db, organization_id=conn.organization_id, bypass=False
            )
        return was - frozenset(conn.watch)

    def _revalidate_one(
        self, conn: Connection
    ) -> tuple[bool, frozenset, frozenset]:
        """(still_valid, capabilities, stations dropped from the watch).

        Runs on a worker thread, which is why the third element exists: leaving
        a group is async and cannot happen here, so what was revoked is handed
        back for `revalidate` to act on. Revoking a watch in the DB and leaving
        the audio group are two halves of one thing, and the half that actually
        stops the sound is the second one.
        """
        with SessionLocal() as db:
            session = AuthSessionRepository(db).get_active(
                session_id=conn.session_id
            )
            if session is None:
                return False, frozenset(), frozenset()
            self._bind_org(db, conn)
            conn.visible_stations = frozenset(
                visible_station_ids(
                    db,
                    user_id=conn.user_id,
                    organization_id=conn.organization_id,
                )
            )
            if conn.watch:
                dropped = self._revalidate_watch(db, conn)
                return True, frozenset(), dropped

            if conn.station_id is None:
                # Without the watch branch above this returned early, so a watch
                # connection was never re-checked at all: an operator who lost
                # their platform role mid-shift would have kept listening until
                # the socket happened to close.
                return True, frozenset(), frozenset()
            return (
                True,
                capabilities_for(
                    db,
                    user_id=conn.user_id,
                    organization_id=conn.organization_id,
                    ground_station_id=conn.station_id,
                ),
                frozenset(),
            )

    async def revalidate(self, conn: Connection) -> bool:
        """Re-check one connection. Returns False if it should be closed.

        Drops any subscription whose capability has gone away, so revoking
        radio.listen actually stops the audio rather than merely preventing the
        next subscribe.
        """
        valid, capabilities, dropped_watch = await asyncio.to_thread(
            self._revalidate_one, conn
        )
        if not valid:
            return False

        # Leave the groups for anything the watch just lost. Clearing conn.watch
        # alone would stop the connection COUNTING as a listener while it went on
        # receiving the audio — the fan-out is driven by group membership, not by
        # that set.
        for station_id in dropped_watch:
            await self.watch_leave(conn, station_id)
        if dropped_watch:
            conn.enqueue({
                "type": "watch_revoked",
                "stations": [str(s) for s in dropped_watch],
            })

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
