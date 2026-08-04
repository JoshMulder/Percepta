"""Where the live H.264 goes: fragmented MP4 over a WebSocket.

The platform decided this and the reasoning is its own to make — fMP4 keeps the
relay a byte pipe, so a second viewer costs a socket rather than a codec, and a
browser plays it through Media Source Extensions with no player library. What
the station owes is the shape:

    wss://<platform>/media/ingest        Authorization: Bearer <credential>

    text    {"codec": "avc1.640028"}     once, before the init segment
    text    init                         a new encoder session starts here
    binary  ftyp + moov                  the initialisation segment
    text    key                          the next fragment is a keyframe
    binary  moof + mdat                  one per frame, from then on

The `key` marker precedes every keyframe fragment so the relay can cache from
the last one — a viewer attaching mid-stream is then handed a decodable run and
sees a picture at once, rather than waiting for the next keyframe. The relay
must not read the media to find keyframes itself (it is a byte pipe), so the
station says, the same way it says the codec and the session start.

**The station id is never sent.** It is derived from the credential at the far
end, because a box holding a valid secret still cannot be trusted to say which
station it is (`contract/README.md` rule 1). The same secret as the broker, over
the same pinned TLS, opened **only while streaming** — a socket that exists when
nothing is being watched is a socket somebody has to reason about.

Three uplinks, one interface:

    MediaUplink   the real one
    FileUplink    writes the same fMP4 to a file, so that the first person with
                  a Pi can prove the camera and the encoder work with no
                  platform, no network and no console
    NullUplink    counts and discards, and says so everywhere it appears

**Nothing here queues.** When the link cannot carry the stream the frame is
dropped and the drop is counted, because on a metered link a buffered second of
1080p is several megabytes of a picture that is already out of date. After a
drop the uplink waits for the next keyframe before sending again: the frames in
between depend on one that never arrived, and sending them produces a smeared
picture that looks like a broken camera rather than a busy link.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import deque

from ..media.websocket import WebSocket, WebSocketError

log = logging.getLogger("gsu.stream")

#: How much a diagnostic capture may write before it stops itself. A stream is
#: megabytes a second and an SD card is not large.
FILE_CAP_BYTES = 64 * 1024 * 1024

#: The path the platform serves. Kept beside the URL derivation rather than
#: buried in it, because it is the platform's to change.
INGEST_PATH = "/media/ingest"

#: How often the encoder thread may retry a platform uplink that went away.
#: It is offered a fragment about thirty times a second and a failed connect
#: costs the connect and handshake timeouts, so retrying on each one would
#: spend the whole stream inside `socket.connect`.
REOPEN_INTERVAL_S = 5.0


class StreamUplink(ABC):
    """One live video connection to the platform."""

    #: Shown on the console and in telemetry, so "where is this going" is
    #: answerable without reading configuration.
    name = "uplink"

    def __init__(self) -> None:
        self.fragments = 0
        self.bytes = 0
        self.dropped = 0
        self.resyncs = 0
        self.reason = ""
        self._skipping = False

    @abstractmethod
    def open(self) -> bool: ...

    @abstractmethod
    def begin(self, codec: str, init_segment: bytes) -> bool:
        """Declare an encoder session and hand over its initialisation segment.

        Called again whenever the encoder's parameters change: the platform
        discards the init segment it was holding, because parameters that no
        longer match decode as corruption rather than as an error.
        """

    @abstractmethod
    def send(self, fragment: bytes, keyframe: bool) -> bool:
        """One fMP4 fragment. False means it was dropped, which is allowed and
        must be counted — never queued."""

    @abstractmethod
    def close(self) -> None: ...

    @property
    def connected(self) -> bool:
        """Whether this uplink still has somewhere to send.

        True by default, and that is the honest answer for most of them: an
        uplink writing to a file or to nowhere cannot lose a connection it
        never had, and is usable from `open()` until `close()`. `MediaUplink`
        is the one that can go away underneath us — the platform closes its
        socket on every restart — and it is the one that overrides this.
        """
        return True

    def describe(self) -> str:
        return self.name

    def stats(self) -> dict:
        return {
            "uplink": self.describe(),
            "fragments": self.fragments,
            "bytes": self.bytes,
            "dropped": self.dropped,
            # How many times the picture had to wait for a keyframe after a
            # drop. This is what a congested link costs an operator: not a
            # number of frames, but a gap in what they can see.
            "resyncs": self.resyncs,
            "reason": self.reason,
        }

    # --- what every uplink does about congestion ------------------------

    def _should_skip(self, keyframe: bool) -> bool:
        """Whether to hold this fragment back while waiting for a keyframe."""
        if not self._skipping:
            return False
        if not keyframe:
            return True
        self._skipping = False
        self.resyncs += 1
        log.info("Media uplink resynchronised on a keyframe after congestion.")
        return False

    def _drop(self) -> bool:
        self.dropped += 1
        self._skipping = True
        return False


class MediaUplink(StreamUplink):
    """The real one: fMP4 frames over an authenticated WebSocket."""

    def __init__(self, url: str, secret: str, trust=None) -> None:
        super().__init__()
        self.name = f"websocket:{url.split('?', 1)[0]}"
        self.url = url
        self.secret = secret
        self.trust = trust
        self.socket: WebSocket | None = None
        self.codec = ""

    def open(self) -> bool:
        # The credential goes in a header rather than in the URL: a URL is
        # logged by every proxy it passes and shows up in error pages.
        socket = WebSocket(
            self.url,
            headers={"Authorization": f"Bearer {self.secret}",
                     "User-Agent": "percepta-gsu"},
            trust=self.trust,
        )
        try:
            socket.connect()
        except Exception as exc:  # noqa: BLE001 - reported, never raised upward
            self.reason = f"the media uplink would not open: {exc}"[:200]
            log.error("%s", self.reason)
            return False
        self.socket = socket
        self.reason = ""
        self._skipping = False
        return True

    def begin(self, codec: str, init_segment: bytes) -> bool:
        if self.socket is None or not self.socket.connected:
            return False
        self.codec = codec
        # Order matters and is the platform's: the codec string first, because
        # Media Source Extensions needs it before a source buffer exists; then
        # `init`, which tells the platform to discard the segment it was
        # holding; then the segment itself.
        steps = (
            lambda: self.socket.send_json({"codec": codec}),
            lambda: self.socket.send_text("init"),
            lambda: self.socket.send_binary(init_segment),
        )
        for step in steps:
            if not step():
                self.reason = self.socket.close_reason or (
                    "the media uplink would not accept the initialisation segment"
                )
                return False
        self.bytes += len(init_segment)
        log.info("Media session started: %s, %d byte initialisation segment.",
                 codec, len(init_segment))
        return True

    def send(self, fragment: bytes, keyframe: bool) -> bool:
        if self.socket is None or not self.socket.connected:
            self.reason = (self.socket.close_reason if self.socket
                           else "the media uplink is not connected")
            return self._drop()
        if self._should_skip(keyframe):
            return self._drop()
        # A keyframe is announced before its fragment, so the relay can cache
        # from the last one and hand a viewer attaching mid-stream a decodable
        # run — a picture at once rather than a wait for the next keyframe. The
        # relay forwards bytes without reading them (it is a byte pipe), so the
        # station is what says which fragment is a keyframe, the same way it says
        # the codec and the session start. One text frame per keyframe interval,
        # negligible against the fragment it precedes.
        if keyframe and not self.socket.send_text("key"):
            if self.socket.close_reason:
                self.reason = self.socket.close_reason
            return self._drop()
        if not self.socket.send_binary(fragment):
            if self.socket.close_reason:
                self.reason = self.socket.close_reason
            return self._drop()
        self.fragments += 1
        self.bytes += len(fragment)
        return True

    @property
    def connected(self) -> bool:
        return self.socket is not None and self.socket.connected

    def close(self) -> None:
        socket, self.socket = self.socket, None
        if socket is not None:
            socket.close("the stream stopped")


class NullUplink(StreamUplink):
    """Counts and discards. What a station has when no media URL is configured.

    It says what it is everywhere it appears — a station reporting `streaming`
    into this is reporting that it is encoding, not that anyone can see
    anything.
    """

    name = "none (no media URL configured)"

    def open(self) -> bool:
        log.warning(
            "Starting the encoder with no media uplink: fragments are being "
            "counted and discarded. Set GSU_MEDIA_URL, or enrol against a "
            "platform that serves one."
        )
        return True

    def begin(self, codec: str, init_segment: bytes) -> bool:
        self.bytes += len(init_segment)
        return True

    def send(self, fragment: bytes, keyframe: bool) -> bool:
        self.fragments += 1
        self.bytes += len(fragment)
        return True

    def close(self) -> None:
        return None


class FileUplink(StreamUplink):
    """Writes the same fMP4 to a file, for one purpose only.

    `GSU_STREAM_SINK=/dev/shm/gsu.mp4` and `python -m gsu stream` are how the
    first person with a Pi finds out whether the camera and the hardware encoder
    actually work, without needing a platform, a network or a console: start it,
    stop it, copy the file off, play it. It is the same fragmented MP4 the
    platform receives, so it plays in anything — which makes it a check on the
    container as well as on the camera.

    Capped, because an unattended box must not fill its own card.
    """

    def __init__(self, path: str, cap: int = FILE_CAP_BYTES) -> None:
        super().__init__()
        self.path = path
        self.cap = cap
        self.name = f"file:{path}"
        self.capped = False
        self._handle = None
        self._lock = threading.Lock()

    def open(self) -> bool:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._handle = open(self.path, "wb")
        except OSError as exc:
            self.reason = f"cannot write the stream to {self.path}: {exc}"[:200]
            log.error("%s", self.reason)
            return False
        self.bytes = 0
        self.fragments = 0
        self.capped = False
        log.info("Writing fragmented MP4 to %s (cap %d MB).",
                 self.path, self.cap // (1024 * 1024))
        return True

    def begin(self, codec: str, init_segment: bytes) -> bool:
        # A second init segment mid-file would make the file unplayable from the
        # start, so it is written once and a later session is reported rather
        # than appended. Live, that case is a restarted encoder; in a file it is
        # a file that should have been two.
        if self.fragments:
            self.reason = (
                "the encoder restarted mid-capture; the rest was not written, "
                "because a second initialisation segment would make the file "
                "unplayable"
            )
            log.warning("%s", self.reason)
            self.capped = True
            return False
        return self._write(init_segment)

    def send(self, fragment: bytes, keyframe: bool) -> bool:
        if self._should_skip(keyframe):
            return self._drop()
        if not self._write(fragment):
            return self._drop()
        self.fragments += 1
        return True

    def _write(self, data: bytes) -> bool:
        with self._lock:
            if self._handle is None or self.capped:
                return False
            if self.bytes + len(data) > self.cap:
                self.capped = True
                log.warning(
                    "Stopped writing %s at %.1f MB: the cap is there so a "
                    "diagnostic capture cannot fill the card.",
                    self.path, self.bytes / 1e6,
                )
                return False
            try:
                self._handle.write(data)
            except OSError as exc:  # pragma: no cover - disk full, etc.
                self.reason = f"writing the stream failed: {exc}"[:200]
                log.error("%s", self.reason)
                return False
            self.bytes += len(data)
            return True

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass


def media_url(config, enrolment=None) -> str | None:
    """Where the media endpoint is.

    `GSU_MEDIA_URL` wins, for the same reason `GSU_BROKER_URL` exists: the
    address a platform states may only be routable from inside its own network,
    and the station is by definition somewhere else. Then whatever the enrolment
    response names, for when it names one. Otherwise it is derived from the
    platform API's address, which is the host that serves it — the scheme
    changes and the path is the platform's.
    """
    if getattr(config, "media_url", None):
        return config.media_url
    stated = getattr(getattr(enrolment, "broker", None), "media_url", None)
    if stated:
        return stated
    api = getattr(config, "platform_url", "") or ""
    scheme, separator, rest = api.partition("://")
    if not separator or not rest:
        return None
    return f"{'wss' if scheme.lower() == 'https' else 'ws'}://{rest.rstrip('/')}{INGEST_PATH}"


class LocalViewer:
    """One browser on the station's own setup page, holding fMP4 for it.

    Not a `StreamUplink`: an uplink is a connection this station opens and
    owns, and this is the other direction — somebody has connected to *us* and
    the console's HTTP thread is draining it. It is a queue with the same rule
    as everything else on this path, which is that video is dropped rather
    than queued. A buffered second of 1080p is several megabytes of a picture
    that is already out of date, and on a page whose entire reason to exist is
    "what is the camera seeing right now" it is worse than a gap.

    **Starts at a keyframe, never before.** A fragment that is not one decodes
    against parameter sets and a reference frame the viewer never received, so
    a viewer that joins mid-GOP is shown nothing until the next keyframe. The
    alternative is a few hundred milliseconds of macroblock soup, which reads
    as a broken camera.
    """

    #: How far behind a viewer may fall before it is resynchronised.
    #:
    #: **One fragment here is one access unit, not one group of pictures.** The
    #: figure this replaces was 8, with a comment reasoning "at a keyframe
    #: interval of one second this is a couple of seconds of slack" — which is
    #: the arithmetic for a fragment per keyframe. A fragment is a frame, so at
    #: 30 fps it was 0.27 seconds of slack, and any brief stall in one browser
    #: overflowed it.
    #:
    #: Overflowing is expensive out of proportion to the stall that caused it:
    #: the queue is cleared and the viewer then waits for the next keyframe,
    #: which is a whole intra period — two seconds on the synthetic source. So
    #: a hiccup of a few frames costs two seconds of picture, the drawn clock
    #: jumps by that much when it returns, and because the queue is per viewer,
    #: two browsers side by side do it at different moments. That is exactly
    #: how it was found.
    #:
    #: Two seconds at 30 fps. The bound that actually protects memory is
    #: MAX_BYTES below, which is what the original comment was reaching for
    #: when it worried about "a buffered second of 1080p".
    DEPTH = 60

    #: And a ceiling in bytes, because a count of fragments promises nothing
    #: about memory: these run from 0.8 kB for a P frame to half a megabyte for
    #: a synthetic keyframe, and a real 1080p camera's are larger again. This
    #: is the "video is dropped rather than queued" rule expressed in the unit
    #: the rule is actually about.
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self) -> None:
        self._queue: deque[bytes] = deque()
        self._queued_bytes = 0
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.closed = False
        self.dropped = 0
        #: Set once the init segment has been queued, and cleared whenever the
        #: encoder's parameters change, because the viewer then needs a new one.
        self._opened = False
        self._waiting_for_keyframe = True

    def begin(self, init_segment: bytes) -> None:
        with self._lock:
            self._queue.clear()
            self._queue.append(init_segment)
            self._queued_bytes = len(init_segment)
            self._opened = True
            self._waiting_for_keyframe = True
            self._wake.set()

    def feed(self, fragment: bytes, keyframe: bool) -> None:
        with self._lock:
            if self.closed or not self._opened:
                return
            if self._waiting_for_keyframe:
                if not keyframe:
                    return
                self._waiting_for_keyframe = False
            if (len(self._queue) >= self.DEPTH
                    or self._queued_bytes + len(fragment) > self.MAX_BYTES):
                # Behind, so throw away what is stale and resynchronise on the
                # next keyframe rather than delivering a growing delay.
                #
                # This costs a whole intra period of picture, not one fragment,
                # because of the line above that then holds everything back
                # until the next keyframe. It is the right trade against a
                # growing delay, and the wrong one to trigger on a hiccup —
                # which is why the depth is what it is.
                self._queue.clear()
                self._queued_bytes = 0
                self._waiting_for_keyframe = True
                self.dropped += 1
                return
            self._queue.append(fragment)
            self._queued_bytes += len(fragment)
            self._wake.set()

    def read(self, timeout: float = 1.0) -> bytes | None:
        """The next fragment, or None if nothing arrived before `timeout`.

        A timeout rather than a blocking wait so the caller gets the chance to
        notice the client has gone and to renew the session's lease.
        """
        self._wake.wait(timeout)
        with self._lock:
            self._wake.clear()
            if self._queue:
                fragment = self._queue.popleft()
                self._queued_bytes = max(0, self._queued_bytes - len(fragment))
                return fragment
        return None

    def close(self) -> None:
        with self._lock:
            self.closed = True
            self._queue.clear()
            self._wake.set()


class TeeUplink(StreamUplink):
    """The platform's uplink and any local viewers, fed from one encoder.

    The camera is a single device with a single owner, and the whole of
    `stream.py` is built on there being exactly one encoder. Serving the setup
    page by starting a second one would undo that — it is the bug that removed
    the snapshot channel, arriving again by a different door. So the setup page
    does not get its own stream; it gets a copy of the one that exists.

    The init segment is held because a viewer that joins mid-session needs it
    and the encoder will not produce another until its parameters change.
    """

    name = "tee"

    def __init__(self, primary: StreamUplink,
                 require_primary: bool = True) -> None:
        super().__init__()
        self.primary = primary
        #: Whether the platform's uplink failing to open fails the session.
        #:
        #: True for a stream the platform asked for: an encoder running with
        #: nowhere to send is the expensive mistake this whole file exists to
        #: prevent. False for one the setup page asked for, because the moment
        #: somebody most needs to aim a camera is the moment the box is not
        #: talking to the platform — a local preview that requires a working
        #: uplink is a preview that is missing whenever it is wanted.
        self.require_primary = require_primary
        self.primary_open = False
        self._viewers: set[LocalViewer] = set()
        self._lock = threading.Lock()
        self._codec = ""
        self._init: bytes | None = None
        #: Set by `open_primary` on whichever thread handled the command, acted
        #: on by `_reopen_if_wanted` on the encoder's. A plain bool rather than
        #: an Event: it is set in one place and cleared in one place, and the
        #: worst a lost edge can cost is one fragment, because the next one is
        #: thirty milliseconds behind it.
        self._reopen_wanted = False
        #: Minus infinity rather than zero so the first attempt is never the
        #: one the rate limit swallows: `time.monotonic()` has no defined
        #: epoch, and on a platform where it starts near zero a plain 0.0 would
        #: hold off the first reconnect for the interval.
        self._last_reopen_at = float("-inf")

    # --- local viewers --------------------------------------------------

    def add(self, viewer: LocalViewer) -> None:
        with self._lock:
            self._viewers.add(viewer)
            init = self._init
        if init is not None:
            viewer.begin(init)

    def remove(self, viewer: LocalViewer) -> None:
        with self._lock:
            self._viewers.discard(viewer)
        viewer.close()

    @property
    def local_viewers(self) -> int:
        with self._lock:
            return len(self._viewers)

    @property
    def local_resyncs(self) -> int:
        """Times a setup-page viewer fell behind and was resynchronised.

        Reported because it was invisible: the session's own `dropped` counts
        what the *uplink* refused, and a browser that stalls is a different
        thing entirely — one viewer of several, on its own queue. A stream
        showing 30 fps with nothing dropped while a picture froze every couple
        of seconds is the exact shape that hid this, and there was no number
        anywhere that disagreed with it.
        """
        with self._lock:
            return sum(viewer.dropped for viewer in self._viewers)

    # --- the uplink itself ----------------------------------------------

    def open(self) -> bool:
        self.primary_open = self.primary.open()
        if self.primary_open:
            return True
        self.reason = self.primary.reason or "the media uplink would not open"
        return not self.require_primary

    def open_primary(self) -> bool:
        """Ask for the platform's uplink. Never waits for it.

        Two situations arrive here and from the outside they look the same. A
        local session may already be running when the platform asks to watch —
        the setup page was open first. Or a session that *was* reaching the
        platform has had its socket closed from the far end, which is what
        every platform restart does. Either way the encoder is already running
        and the init segment is held, so nothing needs restarting; only a
        connection needs making.

        **Making it is not this thread's job.** This runs in the `video.start`
        handler, which is the relay's socket reader. A connect that sits out
        its timeouts here stops the station reading commands, and stops it
        answering pings for long enough that the platform's own liveness rule
        drops the relay — turning a reconnect into a disconnect, on the one
        path whose purpose is to recover. `_reopen_if_wanted` does the work on
        the encoder thread, which is offered a fragment thirty times a second
        and can afford to block.

        So the answer is about now, not about what has been arranged: False
        means the platform is not receiving this stream *yet*.
        """
        # Asking the socket rather than the flag is the whole point.
        # `primary_open` records that the socket was once opened, never that it
        # is still there — trusting it meant that after a platform restart this
        # returned True to every `video.start` for as long as the station
        # stayed up, answering "already streaming" once every ten seconds while
        # sending nothing and never reconnecting.
        if self.primary_open and self.primary.connected:
            return True
        self._reopen_wanted = True
        # The socket's own account of why it went, when it has one — "the
        # platform closed the media uplink" says more to whoever is reading the
        # command's answer than anything this method could invent.
        self.reason = (self.primary.reason
                       or "the platform's uplink is down; reconnecting")
        return False

    def _reopen_if_wanted(self, *, resend_init: bool) -> None:
        """Connect the platform's uplink, on the encoder's thread.

        Called from `begin` and `send` because they are the only two things the
        encoder does with this class, and therefore the only places where a
        connect is allowed to take its time. Rate limited: an unreachable
        platform must not leave the whole stream sitting in `socket.connect`,
        and fragments arrive far faster than a connect can fail.
        """
        if not self._reopen_wanted:
            return
        if self.primary_open and self.primary.connected:
            self._reopen_wanted = False
            return
        now = time.monotonic()
        if now - self._last_reopen_at < REOPEN_INTERVAL_S:
            return
        self._last_reopen_at = now
        if not self.primary.open():
            # `primary_open` is deliberately left as it was. It says the
            # platform is meant to be receiving this stream, and it still is —
            # clearing it here would make `send` treat every dropped fragment
            # as "nobody off-box is watching" and quietly stop counting them.
            self.reason = self.primary.reason or "the media uplink would not open"
            return
        self._reopen_wanted = False
        self.primary_open = True
        self.require_primary = True
        self.reason = ""
        if not resend_init:
            return
        # Whatever the platform was holding went with the socket, and a
        # fragment without its init segment decodes as nothing.
        with self._lock:
            codec, init = self._codec, self._init
        if init is not None:
            self.primary.begin(codec, init)

    def begin(self, codec: str, init_segment: bytes) -> bool:
        # A new encoder session is about to hand over a segment of its own, so
        # re-sending the held one on the way in would only be a duplicate.
        self._reopen_if_wanted(resend_init=False)
        with self._lock:
            self._codec, self._init = codec, init_segment
            viewers = list(self._viewers)
        for viewer in viewers:
            viewer.begin(init_segment)
        if not self.primary_open:
            return True
        return self.primary.begin(codec, init_segment)

    def send(self, fragment: bytes, keyframe: bool) -> bool:
        self._reopen_if_wanted(resend_init=True)
        with self._lock:
            viewers = list(self._viewers)
        for viewer in viewers:
            viewer.feed(fragment, keyframe)
        if not self.primary_open:
            # Nobody off-box is watching, and that is not a dropped frame:
            # `dropped` means the link could not take what it was offered.
            return True
        # The primary's answer is the one that counts for the session's stats:
        # a browser that cannot keep up is that browser's problem, and must not
        # be reported as the platform link dropping frames.
        return self.primary.send(fragment, keyframe)

    def close(self) -> None:
        # Before anything else: a session being torn down must not have a
        # reconnect arranged behind it. A late fragment from an encoder still
        # winding down would otherwise reopen the socket that was just closed.
        self._reopen_wanted = False
        with self._lock:
            viewers, self._viewers = list(self._viewers), set()
            self._init = None
        for viewer in viewers:
            viewer.close()
        self.primary.close()

    def describe(self) -> str:
        local = self.local_viewers
        where = self.primary.describe() if self.primary_open else "nowhere off-box"
        if not local:
            return where
        return f"{where} + {local} on the setup page"

    def stats(self) -> dict:
        stats = self.primary.stats()
        stats["uplink"] = self.describe()
        stats["local_viewers"] = self.local_viewers
        stats["local_resyncs"] = self.local_resyncs
        # Said plainly, because "streaming" with no uplink open is a state
        # somebody will otherwise read as the platform receiving video.
        stats["primary_open"] = self.primary_open
        if not self.primary_open:
            stats["reason"] = self.reason
        return stats


def build_uplink(config, enrolment=None, trust=None) -> StreamUplink:
    """Pick an uplink. One function, so there is one place this is decided.

    A file sink wins when it is set, because it is only ever set deliberately by
    somebody standing in front of the box trying to find out whether the camera
    works.
    """
    sink = getattr(config, "stream_sink", None)
    if sink:
        return FileUplink(sink)
    url = media_url(config, enrolment)
    secret = getattr(getattr(enrolment, "credential", None), "secret", None)
    if url and secret:
        return MediaUplink(url, secret, trust=trust)
    return NullUplink()
