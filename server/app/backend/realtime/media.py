"""The media relay: a station's video stream, re-originated to viewers.

`docs/03-realtime-isolation.md` §7 settled the shape and it is not a
performance compromise - it is the whole point:

    station ──(outbound TLS)──► platform ──(per viewer)──► browser

**Nothing reaches a viewer directly from a station** (topology rule 8). No
peer-to-peer, no direct WebRTC even TURN-relayed, because the efficient thing to
build is exactly the thing that is forbidden. The platform terminates the
station's stream and re-originates it, so a viewer never learns a station's
address and a station never learns a viewer's.

**Fragmented MP4 on the wire, not Annex B.** The relay is then a byte pipe: it
forwards fragments without parsing, transcoding or re-muxing, so a second viewer
costs a socket rather than a codec. A browser plays fMP4 through Media Source
Extensions with no player library. The one piece of state the relay must keep is
the **initialisation segment** - `ftyp` + `moov`, which arrives once at the start
of a stream and which every later viewer needs before any fragment will decode.
A viewer that attaches mid-stream and is simply given the next fragment sees
nothing at all, and it looks exactly like a dead camera.

**On demand, driven by attachment.** Video is the heaviest thing a station sends
and Starlink is metered, so the station is asked to start when the first viewer
attaches and to stop when the last one leaves. The lease is renewed while anyone
is watching; if the platform stops asking - crashed, restarted, network gone -
the station stops on its own rather than transmitting to nobody.
"""

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

#: How long a start lease lasts. The station stops when it expires, so this is
#: also how long a stream survives the platform disappearing. Short enough that
#: a crashed console is not billed for minutes of satellite bandwidth, long
#: enough to ride out a re-deploy.
LEASE_SECONDS = 30

#: How often the platform renews while anyone is watching. Comfortably inside
#: the lease so a single dropped renewal does not stop the stream.
RENEW_SECONDS = 10

#: Fragments held for a viewer that attaches mid-stream. Only the init segment
#: is strictly required; one keyframe fragment beyond it means a new viewer sees
#: a picture in about a second rather than waiting for the next one.
#:
#: Deliberately tiny. A buffer here is latency for everyone and a memory leak
#: per station, and this is a live view - a viewer who wants the last minute
#: wants recordings, which is a different feature entirely.
KEEP_FRAGMENTS = 1


@dataclass
class StationStream:
    """One station's live stream, and everyone watching it."""

    station_id: uuid.UUID
    organization_id: uuid.UUID
    #: ftyp + moov. Every viewer needs this before any fragment decodes.
    init_segment: bytes | None = None
    #: The exact codec string, e.g. "avc1.640028". Media Source Extensions
    #: needs it before a buffer can be created, and a wrong one fails silently -
    #: the video simply never appears. The station knows what its encoder
    #: produced, so it says, rather than the browser guessing.
    codec: str | None = None
    recent: list[bytes] = field(default_factory=list)
    #: A queue carries bytes (media), str (control, e.g. the codec) or None to
    #: close. Control has to travel the same path as media because a viewer
    #: attaches *before* the station starts - see set_codec.
    viewers: set["asyncio.Queue[bytes | str | None]"] = field(default_factory=set)
    #: Set when a station is actually connected and sending.
    publishing: bool = False
    started_at: datetime | None = None
    bytes_in: int = 0
    fragments_in: int = 0

    def snapshot_for_new_viewer(self) -> list[bytes]:
        if self.init_segment is None:
            return []
        return [self.init_segment, *self.recent]


class MediaRelay:
    """Per-process registry of live streams.

    Deliberately in-process and not shared through Redis. Video is bulk data on
    a control bus that must not be delayed by it, and a viewer is already pinned
    to the worker holding its socket. The cost is that a station and its viewers
    must land on the same process, which is why the ingest endpoint and the view
    endpoint are served by the same application rather than split.
    """

    def __init__(self) -> None:
        self._streams: dict[uuid.UUID, StationStream] = {}
        self._lock = asyncio.Lock()
        #: Called when a station should start or stop sending. Wired to the
        #: command channel by the API layer, so this module never imports it.
        self.on_demand_changed = None

    async def stream(
        self, station_id: uuid.UUID, organization_id: uuid.UUID
    ) -> StationStream:
        async with self._lock:
            existing = self._streams.get(station_id)
            if existing is None:
                existing = StationStream(
                    station_id=station_id, organization_id=organization_id
                )
                self._streams[station_id] = existing
            return existing

    def get(self, station_id: uuid.UUID) -> StationStream | None:
        return self._streams.get(station_id)

    # --- the station side ------------------------------------------------

    async def publisher_connected(
        self, station_id: uuid.UUID, organization_id: uuid.UUID
    ) -> StationStream:
        stream = await self.stream(station_id, organization_id)
        stream.publishing = True
        stream.started_at = datetime.now(UTC)
        # A new connection means a new encoder session, so the old init segment
        # describes a stream that no longer exists. Keeping it would hand the
        # next viewer parameters that do not match the fragments they receive,
        # which decodes as corruption rather than as an error.
        stream.init_segment = None
        stream.codec = None
        stream.recent.clear()
        log.info("Media: station %s started publishing.", station_id)
        return stream

    async def publisher_gone(self, station_id: uuid.UUID) -> None:
        stream = self._streams.get(station_id)
        if stream is None:
            return
        stream.publishing = False
        stream.init_segment = None
        stream.recent.clear()
        # Close every viewer rather than leaving them on a stream that has
        # stopped. A frozen last frame is indistinguishable from a working
        # camera looking at something that is not moving.
        for queue in list(stream.viewers):
            queue.put_nowait(None)
        log.info("Media: station %s stopped publishing.", station_id)

    async def set_codec(self, station_id: uuid.UUID, codec: str) -> None:
        """Record the codec and tell everyone already waiting.

        This has to fan out, not just be stored. Video is on demand, so the
        order is always: viewer attaches, platform asks the station to start,
        station connects and sends its codec. At attach there is nothing to
        send - so a relay that only hands the codec over at attach time never
        hands it over at all, and every viewer receives bytes it cannot decode.
        """
        stream = self._streams.get(station_id)
        if stream is None or stream.codec == codec:
            return
        # A *change*, not a first announcement: the station's encoder is now
        # producing something else - an operator changed the camera's encoder
        # from H.265 to H.264, and it is a checkbox in the camera's own web
        # interface. Every fragment already buffered was the old codec, and
        # the init segment we are holding describes it, so both have to go or
        # the viewer decodes new bytes against old parameters and shows a
        # picture that is subtly, silently wrong.
        changed = stream.codec is not None and stream.codec != codec
        if changed:
            log.info(
                "Media: station %s changed codec %s -> %s; resetting viewers.",
                station_id, stream.codec, codec,
            )
            stream.init_segment = None
            stream.recent.clear()
        stream.codec = codec
        message = json.dumps({"codec": codec, "reset": changed})
        for queue in list(stream.viewers):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    async def publish(
        self, station_id: uuid.UUID, fragment: bytes, *, is_init: bool = False
    ) -> None:
        stream = self._streams.get(station_id)
        if stream is None:
            return
        stream.bytes_in += len(fragment)
        stream.fragments_in += 1

        if is_init:
            stream.init_segment = fragment
            # Forwarded as well as kept, for the same reason as the codec: the
            # viewers that made the station start are already attached, and
            # without this they wait for an initialisation segment that has
            # already been and gone.
            for queue in list(stream.viewers):
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(fragment)
            return

        stream.recent.append(fragment)
        del stream.recent[:-KEEP_FRAGMENTS]

        for queue in list(stream.viewers):
            try:
                queue.put_nowait(fragment)
            except asyncio.QueueFull:
                # A viewer that cannot keep up is dropped rather than allowed to
                # slow the stream for everyone else. Buffering for the slowest
                # viewer is how one bad connection degrades every other, and on
                # a live view the right answer for a lagging client is to fall
                # behind and reconnect, not to hold the station back.
                log.info(
                    "Media: dropping a viewer of %s that cannot keep up.",
                    station_id,
                )
                queue.put_nowait(None) if not queue.full() else None
                stream.viewers.discard(queue)

    # --- the viewer side -------------------------------------------------

    async def attach(
        self, station_id: uuid.UUID, organization_id: uuid.UUID
    ) -> tuple[StationStream, "asyncio.Queue[bytes | str | None]"]:
        stream = await self.stream(station_id, organization_id)
        # Bounded. Unbounded is a memory leak wearing a queue's clothes: a
        # viewer that stops reading would otherwise accumulate the whole stream.
        queue: asyncio.Queue[bytes | str | None] = asyncio.Queue(maxsize=64)
        first = len(stream.viewers) == 0
        stream.viewers.add(queue)
        if first and self.on_demand_changed is not None:
            await self.on_demand_changed(stream, True)
        return stream, queue

    async def detach(
        self, station_id: uuid.UUID, queue: "asyncio.Queue[bytes | str | None]"
    ) -> None:
        stream = self._streams.get(station_id)
        if stream is None:
            return
        stream.viewers.discard(queue)
        if not stream.viewers and self.on_demand_changed is not None:
            await self.on_demand_changed(stream, False)

    def viewer_count(self, station_id: uuid.UUID) -> int:
        stream = self._streams.get(station_id)
        return len(stream.viewers) if stream else 0

    def watched_stations(self) -> list[StationStream]:
        return [s for s in self._streams.values() if s.viewers]


relay = MediaRelay()
