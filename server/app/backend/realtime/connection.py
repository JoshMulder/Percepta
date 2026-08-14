"""One WebSocket connection and everything pinned to it."""

import asyncio
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket

from backend.auth.capabilities import Capability
from backend.auth.identity import Identity

# Bounded, drop-oldest. A client on a bad link must never stall fan-out for
# everyone else on the same station - and on a platform whose viewers sit behind
# Starlink, "a client on a bad link" is the normal case, not the exception.
# Dropping the oldest frame is right for live telemetry: a late frame is worth
# less than the current one. Remote-Radio's audio queue makes the same call for
# the same reason.
SEND_QUEUE_MAX = 64


@dataclass(eq=False)
class Connection:
    """Identity is pinned at connect and never re-read from client input.

    eq=False so connections hash by identity, not by field values - the registry
    stores them in sets and two connections with identical state are still two
    different sockets.
    """

    ws: WebSocket
    identity: Identity

    # Pinned at select_station. One station per connection, enforced rather than
    # displayed (docs/00-topology.md rule 5). Several tabs mean several
    # connections, each independently pinned.
    station_id: uuid.UUID | None = None

    # Capabilities for the pinned station, resolved at selection time and
    # refreshed by revalidation. Cached because it is consulted on every
    # subscribe, but it is a cache of an authorisation decision - so anything
    # that could change it must invalidate it, which is what the revocation
    # path exists for.
    capabilities: frozenset[Capability] = frozenset()

    # Stations this connection may see at all, for the org status channel.
    visible_stations: frozenset[uuid.UUID] = frozenset()

    send_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=SEND_QUEUE_MAX)
    )
    #: Stations this connection is guarding on an Odin watch, if any.
    #:
    #: SEPARATE from `station_id` and it must stay separate. The one-station pin
    #: is what leaves the fan-out hot path with no authorisation decision to get
    #: wrong: hub.select_station drops the previous station's groups in the SAME
    #: operation, so "this connection is in exactly one station's groups" is true
    #: at every instant rather than eventually. A watch is a different thing —
    #: several stations, read-only, across tenants — and folding it into
    #: station_id would trade that property away for the convenience of one
    #: field.
    watch: set = field(default_factory=set)

    #: The ONE station this connection is reading live telemetry from, if any.
    #:
    #: A separate field from `watch`, and separate on purpose. `watch` is the
    #: audio guard set and `watch_set` REPLACES it wholesale — so folding an
    #: attach into it would mean that guarding a different channel silently
    #: dropped the operator's live telemetry, or that releasing the last channel
    #: took the drawer's readings with it. Two ideas, two fields.
    #:
    #: Singular because the product is: telemetry is attached for the station an
    #: operator has deliberately opened in the drawer, one at a time. That is
    #: what keeps the cost bounded — the stream is undifferentiated, so an attach
    #: carries that site's whole ADS-B feed as well as its readings.
    attached: uuid.UUID | None = None
    closed: bool = False

    @property
    def user_id(self) -> uuid.UUID:
        return self.identity.user_id

    @property
    def organization_id(self) -> uuid.UUID:
        return self.identity.organization_id

    @property
    def session_id(self) -> uuid.UUID:
        return self.identity.session_id

    def enqueue(self, message: dict) -> None:
        """Non-blocking. Drops the oldest queued frame when full rather than
        blocking the publisher or growing without bound."""
        if self.closed:
            return
        try:
            self.send_queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                self.send_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.send_queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def __hash__(self) -> int:
        return id(self)
