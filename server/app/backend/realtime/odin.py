"""The Odin wall registry: fan-out to command-centre screens.

Deliberately OUTSIDE the Hub, and modelled on realtime/media.py, which is the
existing precedent for a fan-out registry that is not the hub. Three consequences
follow from that choice, and all three are the point rather than side effects:

  - A wall socket structurally CANNOT join a station or organisation group. It
    has no `select_station`, no `subscribe`, no group membership of any kind —
    the only thing it can ever receive is the digest published on one channel.
    The tenant fan-out and the cross-tenant one cannot be confused because they
    do not share a mechanism.
  - realtime/groups.py needs no new function. No broadcast_all, no registry
    iteration, no "every station" primitive that would then exist for anything
    else to reach for.
  - These connections never enter the hub's per-connection revalidation sweep,
    which would otherwise cost two blocking database queries per operator per
    minute to re-answer a question whose answer is "yes, they are still platform
    staff" — and which the socket's own close-on-revocation already covers.

The wall is authorised once, at connect (api/odin.py). What flows afterwards is
one pre-computed frame, identical for every viewer, so there is nothing
per-viewer to re-authorise: an operator either sees the whole fleet or they are
not here at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: The Redis channel the digest is published on. One channel for the whole
#: product: the frame is identical for every operator, so per-viewer channels
#: would be N copies of one computation.
WALL_CHANNEL = "odin:wall"

#: Depth TWO, not the connection default of 64 (realtime/connection.py:18).
#:
#: A backlog of fleet snapshots is worthless by construction: frame N+1 wholly
#: supersedes frame N, and a wall that is four frames behind is not "catching
#: up", it is lying about the present with total confidence. Two rather than one
#: so a frame in flight does not force a drop of the one behind it.
#:
#: A wall that skipped a frame is fine. A wall that is behind is the failure this
#: whole phase exists to remove.
QUEUE_DEPTH = 2


@dataclass(eq=False)
class WallViewer:
    """One command-centre screen."""

    id: uuid.UUID
    user_id: uuid.UUID
    queue: asyncio.Queue[str] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_DEPTH)
    )
    #: Counted rather than logged per occurrence: on a slow link this is the
    #: normal case, and a line per dropped frame would be a line every three
    #: seconds for as long as the operator's connection is poor.
    dropped: int = 0

    def offer(self, frame: str) -> None:
        """Hand this viewer the newest frame, discarding the oldest if full."""
        while True:
            try:
                self.queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    # Drained by the sender between the two calls. Try again;
                    # there is now room.
                    continue


class WallRegistry:
    """Every wall socket on THIS worker.

    Per-worker, like every other fan-out here: each worker delivers only to its
    own connections, and the digest reaches all of them because every worker
    subscribes to the one Redis channel. Nothing here is shared state between
    processes, so nothing here needs a lock beyond the event loop.
    """

    def __init__(self) -> None:
        self._viewers: dict[uuid.UUID, WallViewer] = {}

    def register(self, user_id: uuid.UUID) -> WallViewer:
        viewer = WallViewer(id=uuid.uuid4(), user_id=user_id)
        self._viewers[viewer.id] = viewer
        log.info("Odin wall attached (%s viewer(s) on this worker).", len(self._viewers))
        return viewer

    def unregister(self, viewer: WallViewer) -> None:
        if self._viewers.pop(viewer.id, None) is not None and viewer.dropped:
            # Reported once, at the end, where it says something about the whole
            # session rather than about one bad second.
            log.info(
                "Odin wall detached after dropping %s stale frame(s).", viewer.dropped
            )

    @property
    def count(self) -> int:
        return len(self._viewers)

    def broadcast(self, payload: dict) -> None:
        """Hand one digest to every wall on this worker.

        Serialised ONCE for all of them: the frame is identical by construction,
        and encoding it per viewer would be the per-viewer cost this design
        exists to avoid, reintroduced at the last possible moment.
        """
        if not self._viewers:
            return
        try:
            frame = json.dumps(payload)
        except (TypeError, ValueError):
            log.warning("Odin digest was not serialisable; dropping the frame.")
            return
        for viewer in self._viewers.values():
            viewer.offer(frame)


#: One per worker process.
wall = WallRegistry()
