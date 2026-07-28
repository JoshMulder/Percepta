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
