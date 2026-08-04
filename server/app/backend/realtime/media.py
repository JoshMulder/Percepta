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

#: Held for a viewer that attaches mid-stream: the init segment, and the current
#: group of pictures — everything since the last keyframe. Handing a new viewer a
#: keyframe plus the frames that follow it is a decodable run, so a picture
#: appears at once instead of waiting up to a whole keyframe interval for the
#: next one. Keeping only the single most recent fragment (as this once did) did
#: not deliver that: the most recent fragment is almost always a delta, which
#: decodes as nothing on its own.
#:
#: This does not buffer the live path — every fragment is still forwarded to
#: watchers the instant it arrives; the group is a side-cache read only at
#: attach. It is bounded in bytes AND in fragment count, because a pathological
#: keyframe interval must not turn a live cache into an unbounded per-station
#: leak, and because the whole group is replayed to a joiner in one burst — the
#: viewer's own staging queue has to hold it intact, so the count here is what
#: keeps that bounded and the two must be sized together (web/useVideoStream.ts).
#: A group that outgrows either bound is dropped whole — never truncated, which
#: would strand a keyframe-less run that decodes as nothing — and the joiner
#: then waits for the next keyframe, exactly as it did before the cache existed.
GOP_CACHE_MAX_BYTES = 8 * 1024 * 1024
GOP_CACHE_MAX_FRAGMENTS = 120


#: Live viewer sockets, by the session that opened them.
#:
#: A viewer is keyed by a single-use ticket, so revocation used to have nothing
#: to address: signing out or having a session revoked left the camera running
#: in that tab until the next poll noticed, on the heaviest stream in the
#: platform. The console's own socket has had a push signal all along, because
#: the hub holds connections by session and `revocation.apply_revocation` can
#: find them. This is the equivalent for viewers - something for a push to
#: find. The poll stays as the backstop for everything a push cannot reach: a
#: withdrawn grant, a deactivated station, a worker that never saw the event.
_viewers: dict[uuid.UUID, set[asyncio.Event]] = {}


def watch_session(session_id: uuid.UUID) -> asyncio.Event:
    """Register a viewer. Set when that session should stop watching."""
    stop = asyncio.Event()
    _viewers.setdefault(session_id, set()).add(stop)
    return stop


def unwatch_session(session_id: uuid.UUID, stop: asyncio.Event) -> None:
    holders = _viewers.get(session_id)
    if holders is None:
        return
    holders.discard(stop)
    if not holders:
        _viewers.pop(session_id, None)


def close_session_viewers(session_id: uuid.UUID) -> int:
    """Stop every viewer opened by a session. Returns how many."""
    holders = _viewers.get(session_id) or set()
    for stop in holders:
        stop.set()
    return len(holders)


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
    #: The current group of pictures — the last keyframe fragment and every
    #: delta since — replayed to a viewer that attaches mid-stream so it has a
    #: decodable run at once. `recent_bytes` is its running size, for the cap.
    recent: list[bytes] = field(default_factory=list)
    recent_bytes: int = 0
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
        #: Seconds to keep a station streaming after its last viewer leaves, so a
        #: reload or a tab-flip returns to a live stream (instantly, with the
        #: group-of-pictures cache above) rather than paying the encoder spin-up
        #: again. 0 stops the instant the last viewer goes — the strict on-demand
        #: posture. Set by the API layer from configuration.
        self.linger_seconds = 0
        #: A pending "stop after the linger" task per station, so a viewer coming
        #: back inside the window cancels the stop it would otherwise have sent.
        self._linger: dict[uuid.UUID, asyncio.Task] = {}

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
        stream.recent_bytes = 0
        log.info("Media: station %s started publishing.", station_id)
        return stream

    async def publisher_gone(self, station_id: uuid.UUID) -> None:
        stream = self._streams.get(station_id)
        if stream is None:
            return
        stream.publishing = False
        stream.init_segment = None
        stream.recent.clear()
        stream.recent_bytes = 0
        # A linger stop queued for a stream that has already gone is moot; drop
        # it rather than firing a video.stop at a station that is no longer there.
        pending = self._linger.pop(station_id, None)
        if pending is not None:
            pending.cancel()
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
            stream.recent_bytes = 0
        stream.codec = codec
        message = json.dumps({"codec": codec, "reset": changed})
        for queue in list(stream.viewers):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    async def publish(
        self, station_id: uuid.UUID, fragment: bytes, *,
        is_init: bool = False, keyframe: bool = False,
    ) -> None:
        stream = self._streams.get(station_id)
        if stream is None:
            return
        stream.bytes_in += len(fragment)
        stream.fragments_in += 1

        if is_init:
            stream.init_segment = fragment
            stream.recent.clear()
            stream.recent_bytes = 0
            # Forwarded as well as kept, for the same reason as the codec: the
            # viewers that made the station start are already attached, and
            # without this they wait for an initialisation segment that has
            # already been and gone.
            for queue in list(stream.viewers):
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(fragment)
            return

        # Hold the current group of pictures for a late joiner. A keyframe (the
        # station marks it — the relay must not read the media to find out)
        # starts a fresh group; a delta extends it; a delta arriving before any
        # keyframe is not held, because on its own it decodes as nothing. A group
        # that outgrows the cap is dropped rather than held unbounded — the
        # joiner then waits for the next keyframe, as it did before this cache.
        if keyframe:
            stream.recent = [fragment]
            stream.recent_bytes = len(fragment)
        elif stream.recent:
            stream.recent.append(fragment)
            stream.recent_bytes += len(fragment)
            if (stream.recent_bytes > GOP_CACHE_MAX_BYTES
                    or len(stream.recent) > GOP_CACHE_MAX_FRAGMENTS):
                stream.recent.clear()
                stream.recent_bytes = 0

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
        # A viewer returning inside the linger window cancels the stop that was
        # queued when the last one left. The stream never stopped, so this is the
        # instant return the linger exists to give — no encoder spin-up, and the
        # start below is skipped because this is not the first viewer of a cold
        # stream.
        pending = self._linger.pop(station_id, None)
        if pending is not None:
            pending.cancel()
        if first and self.on_demand_changed is not None:
            # Idempotent at the station if it was lingering (already streaming),
            # a real start if it was cold.
            await self.on_demand_changed(stream, True)
        return stream, queue

    async def detach(
        self, station_id: uuid.UUID, queue: "asyncio.Queue[bytes | str | None]"
    ) -> None:
        stream = self._streams.get(station_id)
        if stream is None:
            return
        stream.viewers.discard(queue)
        if stream.viewers or self.on_demand_changed is None:
            return
        if self.linger_seconds <= 0:
            await self.on_demand_changed(stream, False)
            return
        # Keep it running for a moment rather than stopping the instant the last
        # viewer goes: a reload is the common reason a viewer leaves, and a cold
        # restart of the encoder is the cost this avoids. If nobody is back when
        # the window elapses, `_linger_then_stop` stops it.
        existing = self._linger.pop(station_id, None)
        if existing is not None:
            existing.cancel()
        self._linger[station_id] = asyncio.create_task(
            self._linger_then_stop(stream)
        )

    async def _linger_then_stop(self, stream: StationStream) -> None:
        try:
            await asyncio.sleep(self.linger_seconds)
        except asyncio.CancelledError:
            return
        self._linger.pop(stream.station_id, None)
        # Re-checked, never assumed: a viewer may have come and gone again during
        # the window. The stop only fires if the stream is still unwatched, which
        # also makes the cancel above an optimisation rather than the guarantee.
        if not stream.viewers and self.on_demand_changed is not None:
            await self.on_demand_changed(stream, False)

    def viewer_count(self, station_id: uuid.UUID) -> int:
        stream = self._streams.get(station_id)
        return len(stream.viewers) if stream else 0

    def watched_stations(self) -> list[StationStream]:
        return [s for s in self._streams.values() if s.viewers]


relay = MediaRelay()
