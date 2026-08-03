"""The live H.264 stream, which runs only while somebody is watching.

A 1080p30 stream is 2-4 Mbit/s. On a metered satellite link, running that into a
console nobody has open is the most expensive mistake this station can make, and
it is a mistake that makes no noise: the picture is fine, the station is
healthy, and the bill arrives a month later. So the stream is off, and the
platform asks for it.

Four properties, each of which is a failure that has to be designed out rather
than noticed:

**Idempotent.** Two viewers attaching must not start two encoders — the camera
is a single device and the second `rpicam-vid` fails with a device-busy that
reads like broken hardware. `video.start` while streaming extends the lease and
counts a viewer; it does not restart anything.

**Fail closed on silence.** The stream stops unless the platform keeps asking
for it. A `video.start` carries a lease; when the lease expires the encoder
stops. That is deliberately the opposite of "stop when told to stop": the
failure to design for is the console closing, or the link dropping, while the
station keeps paying for a stream nobody can see. Losing the link is the normal
case here, not the exception.

**A ceiling as well as a lease.** A stream that somehow keeps being renewed
still stops at `stream_max_minutes`, because "somehow" on an unattended site
means nobody is watching it either.

**Reported, never assumed.** The platform confirms nothing and waits to see the
change in telemetry (`contract/transport.md`), so the actual state — running or
not, since when, at what measured bitrate, and why not if not — goes out in
every health frame. A `video.start` that silently did nothing is visible.
"""

from __future__ import annotations

import logging
import time

from . import clock
from .camera import sensor_exclusive
from .camera.h264 import (
    H264, StreamSettings, choose_encoder, probe_encoders, sniff_codec,
    split_annexb,
)
from .media.fmp4 import Fmp4Muxer
from .transport.stream import (
    LocalViewer, TeeUplink, build_uplink, media_url,
)

log = logging.getLogger("gsu.stream")

#: Lease bounds. Five seconds is the shortest that survives one missed command
#: on a link that drops; five minutes is the longest anyone should be able to
#: commit the link to without asking again.
MIN_LEASE_S = 5.0
MAX_LEASE_S = 300.0
DEFAULT_LEASE_S = 30.0

#: How long a starting stream waits for the sensor before giving up and naming
#: whoever has it. This replaced a flat `time.sleep(2.5)` that ran on every
#: start whether or not anything else held the camera: the sleep was tuned to
#: outlast the slowest snapshot this hardware had produced, which is a guess
#: about somebody else's subprocess. Waiting on the lease waits exactly as long
#: as the holder actually takes, and no time at all in the normal case where
#: nobody is holding it.
SENSOR_WAIT_S = 10.0


class StreamSession:
    """The one live encoder this station has, and the rules about when it runs."""

    def __init__(self, agent) -> None:
        self.agent = agent
        self.source = None
        self.uplink = None
        self.state = "idle"
        self.viewers = 0
        self.started_at: float | None = None
        self.started_clock = None
        self.expires_at: float | None = None
        self.stopped_reason = ""
        self.reason = ""
        self.frames = 0
        self.bytes_out = 0
        self.dropped = 0
        self.encoder_choice = ""
        self.muxer: Fmp4Muxer | None = None
        self.codec = ""
        #: The sensor lease token, while this session owns the camera. None for
        #: a network camera, which has no local sensor to own.
        self._sensor_token: str | None = None
        #: The rate the muxer's clock is actually paced to, and where that
        #: number came from. Reported, because a stream paced wrong looks like
        #: a stuttering camera and nothing in the telemetry used to say
        #: otherwise.
        self.paced_fps = 0.0
        self.pacing_source = ""
        #: Set by the encoder thread when the bytes arriving are not the codec
        #: this session was built for; acted on by `tick()`, which is the only
        #: thread that may stop a stream. See `_codec_agrees`.
        self._codec_mismatch = ""
        self._session_open = False
        self._last_frame_at: float | None = None
        #: True while the only reason this encoder is running is somebody on
        #: the station's own setup page. The platform never asked, so nothing
        #: will ever renew the lease on its behalf and nothing will send a
        #: `video.stop` — the last local viewer leaving has to end it, or the
        #: camera stays busy until a lease nobody is holding runs out.
        self._local_only = False

    # --- what the platform asks for -------------------------------------

    def start(self, request: dict | None = None) -> str:
        """`video.start`. Idempotent, leased, and honest about refusing.

        Returns a sentence for the command log; the *state* it produces is what
        the platform actually reads, in the next health frame.
        """
        request = request or {}
        # `lease_seconds` is the platform's name for it. The other two are
        # accepted because they were this station's provisional names before the
        # platform answered, and a station that only understands the newest
        # spelling of a field is a station that breaks on the day someone
        # deploys an older console.
        lease = _lease_seconds(next(
            (request[key] for key in ("lease_seconds", "lease_s", "ttl_s")
             if key in request),
            None,
        ))
        self.viewers = max(1, int(request.get("viewers", 1) or 1))
        self.expires_at = time.monotonic() + lease
        if not request.get("_local"):
            # The platform is watching now, so the session outlives the setup
            # page and ends the way every other stream does.
            self._local_only = False

        if self.state == "streaming":
            # The setup page may have started this before the platform asked.
            # The encoder is already running and the init segment is held, so
            # the platform joins without restarting anything.
            if not request.get("_local") and isinstance(self.uplink, TeeUplink):
                if not self.uplink.open_primary():
                    return (
                        f"already streaming locally, but {self.uplink.reason}"
                    )
            return (
                f"already streaming; lease extended {lease:.0f}s, "
                f"{self.viewers} viewer(s)"
            )

        settings = self.settings(request)
        source = self._build_source(settings)
        if source is None:
            self.state = "unavailable"
            return self.reason

        # Opened here and closed on stop: the connection exists only while
        # somebody is watching, which is the same rule as the encoder. Held in
        # a local as well as on self, and everything below reads the local:
        # the moment the state says "streaming", the monitor half of this class
        # (tick, on the sensing thread) may stop the stream and null
        # `self.uplink` — it cannot null a local. The first real station
        # proved the race is real; reading the attribute "just once" only
        # narrowed it.
        # Wrapped in a tee so the setup page can watch the same encoder. The
        # camera is a single device with a single owner and this whole class is
        # built on there being exactly one encoder; giving the setup page its
        # own would be the two-readers bug arriving by a different door.
        uplink = TeeUplink(
            build_uplink(
                self.agent.config, self.agent.enrolment,
                trust=self.agent.api_trust,
            ),
            # A stream the setup page asked for does not need the platform's
            # uplink to open. The moment somebody most needs to aim a camera is
            # the moment the box is not talking to the platform, and a local
            # preview that requires a working uplink is missing whenever it is
            # wanted. A stream the platform asked for still needs it: an
            # encoder running with nowhere to send is the expensive mistake.
            require_primary=not (request or {}).get("_local"),
        )
        self.uplink = uplink
        if not uplink.open():
            self.state = "unavailable"
            self.reason = (
                uplink.reason
                or f"the media uplink {uplink.describe()} could not be opened"
            )
            self.uplink = None
            return self.reason

        # Take the sensor, by name, and hold it for the session. This replaced
        # a `camera.close()` followed by `time.sleep(2.5)` — a relinquish that
        # asked the other reader nicely, and then a fixed wait tuned to outlast
        # its slowest subprocess. Both were guesses about somebody else's
        # timing, and neither could say who actually had the camera when the
        # guess was wrong. The lease waits exactly as long as the holder takes,
        # returns immediately when nobody holds it, and names the holder when
        # it gives up. See camera/ownership.py.
        #
        # Only where the two paths share one physical sensor: a network camera
        # serves any number of readers from its own encoder, and the synthetic
        # source is a drawing routine. One predicate decides, shared with
        # video.py.
        self.state = "starting"
        camera = getattr(self.agent, "camera", None)
        if sensor_exclusive(camera):
            token = self.agent.sensor_lease.acquire("the live stream", SENSOR_WAIT_S)
            if token is None:
                self.state = "unavailable"
                self.reason = (
                    f"the camera is held by "
                    f"{self.agent.sensor_lease.holder or 'something else'} and did "
                    f"not come free within {SENSOR_WAIT_S:.0f}s, so the stream was "
                    f"not started. Nothing is broken; try again."
                )[:200]
                log.warning("%s", self.reason)
                uplink.close()
                self.uplink = None
                return self.reason
            self._sensor_token = token

        self.frames = 0
        self.bytes_out = 0
        self.dropped = 0
        self._last_frame_at = None
        self._session_open = False
        self.codec = ""
        self._codec_mismatch = ""
        # One muxer per session, built AFTER the source and paced from it. That
        # ordering is the fix for a real fault, not a tidy-up: `settings()`
        # reads the site's configured frame rate, `_build_source()` is what
        # probes the camera for the rate it is actually sending at, and
        # building the clock from `settings` therefore used the stale figure on
        # the first stream after every restart. A muxer clocked at 30 against a
        # camera sending 25 advances its timeline 20% faster than frames
        # arrive, which a viewer shows as stutter and catch-up — and which
        # looked intermittent, because every later stream reused the cached
        # probe and was right.
        #
        # It is also told which codec's access units to expect, by the source
        # that produces them: the two rpicam encoders and the synthetic one are
        # H.264 by construction, and a network camera is whatever it was
        # configured for when the session started.
        self.paced_fps = float(getattr(source, "stream_fps", None) or settings.fps)
        self.pacing_source = (
            "measured from the camera's own stream"
            if getattr(source, "stream_fps", None)
            else "this station's configured rate — the source did not state one"
        )
        self.muxer = Fmp4Muxer(
            settings.width, settings.height, self.paced_fps,
            rules=getattr(source, "nal_rules", H264),
        )

        # No retry loop here, deliberately - one was tried and it was dead
        # weight. source.start() returns False only for a missing tool or a
        # spawn error, neither of which waiting cures, while losing the sensor
        # to an in-flight snapshot shows up as an asynchronous death that only
        # the encoder's own pump thread ever sees. The respawn lives there
        # (camera/h264.py, _pump), where the death is actually observed.
        if not source.start(self._on_unit):
            self.state = "unavailable"
            self.reason = source.reason or "the encoder would not start"
            uplink.close()
            self.uplink = None
            # Given back on the way out. A start that failed holding the sensor
            # would lock every later attempt out of a camera nothing is using,
            # which is the wedge this whole change exists to end — arriving by
            # the front door this time.
            self._release_sensor()
            return self.reason

        self.source = source
        self.state = "streaming"
        self.started_at = time.monotonic()
        self.started_clock = clock.now()
        self.reason = ""
        self.stopped_reason = ""
        # The local, never the attribute: if the encoder dies in its first
        # second, the monitor thread stops the stream and nulls `self.uplink`
        # while this method is still composing its own report - which is not
        # hypothetical, it is how the first real station turned a dead encoder
        # into an AttributeError on top of it. (Reading the attribute once was
        # the first version of this fix, and it only made the window smaller;
        # tests/test_video.py holds the door open and slams it.)
        uplink_name = uplink.describe()
        # What is happening, not what was asked for. Used by the log, the
        # stored event and the sentence the command log shows, so that all
        # three say the same thing — they did not, and the one an operator
        # sees first was the one still quoting site policy.
        # A source that states its own picture size is believed over the
        # settings it was handed. The synthetic encoder is the case: its
        # macroblocks are uncompressed, so it shrinks the picture until a
        # keyframe is one a decoder will take, and quoting the configured size
        # here would name a resolution nothing is sending.
        width = getattr(source, "width", None) or settings.width
        height = getattr(source, "height", None) or settings.height
        if getattr(source, "enforces_settings", True):
            shape = f"{width}x{height} at {settings.fps} fps"
        else:
            codec = getattr(source, "codec", "").upper() or "video"
            shape = f"the camera's own {codec} at {self.paced_fps:.3g} fps"
        # On a remux source the resolution, rate and bitrate belong to the
        # camera and this station applies none of them, so stating them read as
        # fact when it was site policy. Telemetry has reported `requested` and
        # `delivered` separately for a while; this is everything an operator
        # actually reads catching up.
        if getattr(source, "enforces_settings", True):
            log.info(
                "Streaming %dx%d at %d fps, %d kbit/s target, to %s. "
                "Lease %.0fs.",
                width, height, settings.fps,
                settings.bitrate_kbps, uplink_name, lease,
            )
        else:
            log.info(
                "Streaming the camera's own %s, paced at %.3g fps (%s), to %s. "
                "Lease %.0fs. Site policy asks for %dx%d at %d fps and "
                "%d kbit/s; a remux applies none of it.",
                getattr(source, "codec", "").upper() or "video", self.paced_fps,
                self.pacing_source or "unknown", uplink_name, lease,
                settings.width, settings.height, settings.fps,
                settings.bitrate_kbps,
            )
        self.agent.store.record_event(
            "video.stream_started", "info",
            f"Live stream started at {shape} for {self.viewers} viewer(s).",
        )
        return (
            f"streaming {shape} to {uplink_name}, lease {lease:.0f}s"
        )

    # --- somebody on the station's own setup page -----------------------

    def attach_local(self) -> LocalViewer | None:
        """A viewer on the setup page, starting the encoder if it is idle.

        Returns None if the stream cannot run at all, so the caller can say why
        rather than serving an empty response that looks like a hung camera.
        """
        if self.state != "streaming":
            was_idle = self.state == "idle"
            self.start({"viewers": 1, "_local": True})
            if self.state != "streaming":
                return None
            if was_idle:
                self._local_only = True
        uplink = self.uplink
        if not isinstance(uplink, TeeUplink):
            return None
        viewer = LocalViewer()
        uplink.add(viewer)
        return viewer

    def renew_local(self) -> None:
        """Keep the session alive while a setup page is still reading it.

        The lease is the same fail-closed mechanism the platform uses: a
        browser that goes away without closing its socket stops renewing, and
        the encoder stops on its own.
        """
        if self.state == "streaming":
            self.expires_at = time.monotonic() + DEFAULT_LEASE_S

    def detach_local(self, viewer: LocalViewer) -> None:
        uplink = self.uplink
        if isinstance(uplink, TeeUplink):
            uplink.remove(viewer)
            if self._local_only and uplink.local_viewers == 0:
                self.stop("the setup page stopped watching")
        else:
            viewer.close()

    def stop(self, reason: str = "stopped by the platform") -> str:
        """`video.stop`, and every other way this ends. Safe to call at any
        time, including when nothing is running."""
        self.viewers = 0
        self.expires_at = None
        self._local_only = False
        if self.state != "streaming":
            self.state = "idle"
            # Released even on this path. A start that got as far as taking the
            # sensor and then failed leaves `state` at "unavailable", and a
            # `video.stop` arriving afterwards is the one thing that will ever
            # tidy up after it.
            self._release_sensor()
            return "not streaming"
        source, self.source = self.source, None
        uplink, self.uplink = self.uplink, None
        self.muxer = None
        self._session_open = False
        if source is not None:
            source.stop()
        # After source.stop(), never before: stop() does not return until the
        # encoder process is dead and waited for, and giving the sensor back
        # while `rpicam-vid` still had it would hand the next reader a camera
        # the lease says is free and the kernel says is not.
        self._release_sensor()
        if uplink is not None:
            # Closed here, not left open between sessions: an idle socket to the
            # platform is a thing somebody has to reason about, and this one
            # carries a credential.
            uplink.close()
        elapsed = time.monotonic() - (self.started_at or time.monotonic())
        self.state = "idle"
        self.stopped_reason = reason
        log.info(
            "Stream stopped after %.0fs and %d frames (%.1f MB): %s",
            elapsed, self.frames, self.bytes_out / 1e6, reason,
        )
        self.agent.store.record_event(
            "video.stream_stopped", "info",
            f"Live stream stopped after {elapsed:.0f}s, {self.bytes_out / 1e6:.1f} MB: "
            f"{reason}.",
        )
        return f"stopped after {elapsed:.0f}s, {self.frames} frames"

    def tick(self) -> None:
        """Called from the sensing loop. This is the fail-closed half.

        Everything that stops a stream without being told to stop happens here:
        an expired lease, the ceiling, and an encoder that has died on its own.
        """
        if self.state != "streaming":
            return
        if self._codec_mismatch:
            # The bitstream disagreed with the container. Noticed on the
            # encoder's thread, stopped here — see `_codec_agrees`.
            reason, self._codec_mismatch = self._codec_mismatch, ""
            self.reason = reason
            log.error("%s", reason)
            self.stop(reason)
            self.state = "unavailable"
            return
        now = time.monotonic()
        if self.expires_at is not None and now > self.expires_at:
            self.stop(
                "the platform stopped renewing the lease — nobody is watching, "
                "or the link is down"
            )
            return
        ceiling = float(self.agent.site.stream_max_minutes or 0) * 60
        if ceiling and self.started_at and now - self.started_at > ceiling:
            self.stop(f"the {ceiling / 60:.0f} minute ceiling was reached")
            return
        if self.source is not None and not self.source.running:
            self.reason = self.source.reason or "the encoder exited"
            self.stop(self.reason)
            self.state = "unavailable"

    # --- ownership --------------------------------------------------------

    def _release_sensor(self) -> None:
        """Give the camera back, if this session had it. Safe to call twice.

        The token is cleared before the release so that a second call cannot
        release a lease some *later* session has since been granted — the same
        stale-holder mistake the lease itself refuses, caught one level up
        where it is cheaper to reason about.
        """
        token, self._sensor_token = self._sensor_token, None
        if token is not None:
            self.agent.sensor_lease.release(token)

    # --- the frames -----------------------------------------------------

    def _codec_agrees(self, nals: list[bytes], muxer) -> bool:
        """Are these bytes the codec this session was built for?

        The container's codec is chosen before a single byte arrives — from an
        `ffprobe` run at start-up, which picks the ffmpeg muxer and the NAL
        grammar together. When that answer is wrong the failure is silent and
        total: `-c copy` pours the bytes, ffmpeg exits zero, the browser is
        handed a codec string that does not describe them, and what an operator
        sees is a degraded picture rather than an error. A station announced
        `hvc1.1.6.L153.a0` over H.264 bytes for a whole session after somebody
        changed the encoder in the camera's own web interface.

        So the probe is not trusted — it is *checked*, against the bitstream,
        on every access unit. `sniff_codec` reads nothing but NAL headers, so
        this costs a couple of byte comparisons per frame and needs no
        configuration to be right.

        A disagreement ends the session rather than trying to rebuild it in
        place. The muxer's decode clock, its parameter sets and the platform's
        init segment all belong to the old codec, and the platform's viewer
        cannot swap a `MediaSource` codec mid-session either — so the honest
        move is to end this stream with a reason and let the next `video.start`
        probe again and build a session that matches.

        It is *recorded* here and acted on in `tick()`, not stopped from this
        thread. This runs on the encoder's own pump thread, and `stop()` joins
        that thread — calling it from here is a thread joining itself, which
        raises rather than stopping anything. `tick()` runs on the sensing loop
        and is already where every not-told-to-stop stop happens.
        """
        arrived = sniff_codec(nals)
        expected = muxer.rules.name
        if arrived is None or arrived == expected:
            return True
        self._codec_mismatch = (
            f"the camera is sending {arrived.upper()} and this session was built "
            f"for {expected.upper()}. The encoder was changed underneath the "
            f"stream; it is being stopped rather than sent as a container that "
            f"says one thing and carries another. Starting it again reads the "
            f"camera afresh."
        )[:200]
        return False

    def _on_unit(self, unit) -> None:
        """One access unit, from the encoder's own thread.

        Muxed into a fragment here rather than in the encoder, so all three
        encoders produce identical container output, and dropped rather than
        queued when the uplink will not take it — the same rule as telemetry,
        and more important here: a buffered second of 1080p is several megabytes
        of a picture that is already out of date.
        """
        self.frames += 1
        self.bytes_out += len(unit.data)
        self._last_frame_at = time.monotonic()
        uplink, muxer = self.uplink, self.muxer
        if uplink is None or muxer is None:
            return
        # From the reader, which had the list before it joined the bytes.
        # `split_annexb` walks every byte of the frame in Python — on a 4K
        # stream tens of milliseconds a frame on a Pi 2B — and it was being run
        # here to rebuild exactly what `AnnexBReader._emit` had just discarded.
        # The fallback is for an AccessUnit that did not come from the reader.
        nals = list(unit.nals) or split_annexb(unit.data)
        if not self._codec_agrees(nals, muxer):
            return

        fragment, keyframe, changed = muxer.feed(unit, nals)
        if changed or not self._session_open:
            # A new encoder session: parameters that no longer match decode as
            # corruption rather than as an error, so the platform is told to
            # discard what it was holding before the new segment arrives.
            segment = muxer.init_segment()
            if segment is None:
                # No parameter sets yet, so there is nothing a decoder could do
                # with a fragment. Waiting is correct; the next keyframe carries
                # them, and `--inline` means that is at most one keyframe away.
                #
                # Unless the muxer has one and could not use it, which is a
                # different situation with the same shape: frames arriving,
                # nothing sendable, and — without this line — a stream that
                # reports `streaming` forever while the console shows black.
                if muxer.reason:
                    self.reason = muxer.reason
                return
            self.codec = muxer.codec()
            # A parameter set that would not read is recoverable — the next
            # keyframe carries another one — so the reason it left behind is
            # cleared here rather than lingering in telemetry over a stream
            # that has since started working.
            self.reason = ""
            self._session_open = uplink.begin(self.codec, segment)
            if not self._session_open:
                self.dropped += 1
                return
        if fragment is None:
            return
        if not uplink.send(fragment, keyframe):
            self.dropped += 1

    def _build_source(self, settings: StreamSettings):
        """The encoder: whichever one this box actually has.

        Three implementations of one interface, and the station discovers which
        it can use rather than being built for one — a board with a
        fixed-function encode block and a board without are the same code and a
        different probe. Which one ran, and what it achieved, goes out in
        telemetry so that nobody has to work it out later.
        """
        camera = getattr(self.agent, "camera", None)
        simulated = bool(getattr(camera, "describe", None)) and camera.describe().simulated
        if simulated:
            from .camera.h264_synthetic import SyntheticH264Source

            name = self.agent.enrolment.site.name if self.agent.enrolment else ""
            self.encoder_choice = "synthetic encoder — the fitted camera is simulated"
            return SyntheticH264Source(settings, station_name=name)
        if camera is None:
            self.reason = (
                "no camera fitted, so there is nothing to stream. The setup "
                "page's preview is blank for the same reason."
            )
            return None
        # A camera that brings its own stream brings it whole: an RTSP camera
        # encodes on the far end and the station remuxes, so probing this
        # box's encoders would answer a question nobody asked.
        build_source = getattr(camera, "stream_source", None)
        if build_source is not None:
            source = build_source(settings)
            if source is None:
                self.reason = (
                    getattr(camera, "unavailable_reason", "")
                    or "the camera cannot provide a live stream"
                )[:200]
                return None
            self.encoder_choice = getattr(source, "kind", "camera-provided stream")
            log.info("Streaming from the camera's own source: %s", self.encoder_choice)
            return source
        encoder, why = choose_encoder(self.agent.config.encoder)
        self.encoder_choice = why
        if encoder is None:
            self.reason = why[:200]
            return None
        log.info("Encoding with the %s", why)
        return encoder(settings)

    def settings(self, request: dict | None = None) -> StreamSettings:
        """What to encode at: the site's policy, narrowed by what was asked for.

        The platform may ask for less than the site allows — a thumbnail viewer
        does not need 1080p — and may not ask for more. Bandwidth policy belongs
        to whoever pays for the link, not to whoever opened a console.

        **These are instructions to an encoder, not a description of a stream.**
        The distinction was not made before and it cost a real fault. This used
        to reach into the camera for `stream_fps` and use it here — but for a
        remux source there is no encoder to instruct, the numbers are the
        camera's own, and reading them at *this* point in the start sequence
        read them before anything had probed the camera. The stream's real rate
        now travels on the source (`source.stream_fps`) and reaches the muxer
        directly, which is the only consumer that ever needed it. What is left
        here is site policy, which is what it always should have been.
        """
        request = request or {}
        site = self.agent.site
        width = min(int(site.stream_width), int(request.get("width", site.stream_width)))
        height = min(int(site.stream_height), int(request.get("height", site.stream_height)))
        fps = min(float(site.stream_fps), float(request.get("fps", site.stream_fps)))
        bitrate = min(
            int(site.stream_bitrate_kbps),
            int(request.get("bitrate_kbps", site.stream_bitrate_kbps)),
        )
        camera = getattr(self.agent, "camera", None)
        return StreamSettings(
            width=max(160, width - width % 16 if width % 16 else width),
            height=max(120, height),
            fps=max(1, int(fps)),
            bitrate_kbps=max(100, bitrate),
            intra_period=max(1, int(fps) * 2),
            rotation=getattr(camera, "rotation", 0) or 0,
            camera_num=getattr(camera, "camera_num", 0) or 0,
        )

    # --- what it is doing -----------------------------------------------

    def state_payload(self) -> dict:
        """The live half of `video` in the health frame.

        Deliberately says `uplink` and `codec` out loud: a station with no
        media URL configured is encoding into a counter, and a console must not
        be able to read that as a working stream.
        """
        now = time.monotonic()
        elapsed = max(0.001, now - self.started_at) if self.started_at else 0.0
        settings = self.settings()
        payload = {
            "state": self.state,
            "viewers": self.viewers,
            "since": self.started_clock.isoformat() if (
                self.started_clock and self.state == "streaming") else None,
            "lease_remaining_s": round(self.expires_at - now, 1) if (
                self.expires_at and self.state == "streaming") else 0.0,
            "frames": self.frames,
            "bytes": self.bytes_out,
            "dropped": self.dropped,
            "bitrate_bps": round(self.bytes_out * 8 / elapsed) if elapsed else 0,
            "fps_measured": round(self.frames / elapsed, 1) if elapsed else 0.0,
            # What this station *asked* for. On a remux source it is asking
            # nobody anything — the camera decided all of it before the station
            # connected — so `delivered` below is the one to read, and the two
            # being different is information rather than a fault.
            "requested": {
                "width": settings.width, "height": settings.height,
                "fps": settings.fps, "bitrate_kbps": settings.bitrate_kbps,
            },
            # What is actually in the container, read from the bitstream. The
            # three fields here were each wrong in a different way on a real
            # camera — a stale codec cache, a frame rate seeded from a removed
            # configuration field, and dimensions taken from site policy rather
            # than from the sequence parameter set — and each failed silently,
            # so each is now reported next to what was requested.
            "delivered": {
                "width": self.muxer.picture_width if self.muxer else 0,
                "height": self.muxer.picture_height if self.muxer else 0,
                "fps": round(self.paced_fps, 3),
                "fps_source": self.pacing_source,
            },
            "uplink": self.uplink.describe() if self.uplink else
                      build_uplink(self.agent.config, self.agent.enrolment).describe(),
            "media_url": media_url(self.agent.config, self.agent.enrolment),
            "codec": self.codec,
            # Which encoder is doing the work, what this box could have used,
            # and what it actually achieved. A station on the software path at
            # 11 fps and one on the hardware path at 30 are the same telemetry
            # shape and completely different situations, and the difference
            # should never have to be inferred.
            "encoder": getattr(self.source, "name", None) or
                       getattr(self.source, "tool", None),
            "encoder_kind": getattr(self.source, "kind", None),
            "encoder_choice": self.encoder_choice,
            "encoders_available": [probe.to_dict() for probe in probe_encoders()],
            "keyframes": getattr(self.source, "keyframes", 0),
            "reason": self.reason or self.stopped_reason,
        }
        # What the uplink itself saw: fragments away, bytes, and — the number
        # that matters on a congested link — how many times the picture had to
        # wait for a keyframe after a drop.
        if self.uplink is not None:
            payload.update(self.uplink.stats())
        return payload


def _lease_seconds(value) -> float:
    try:
        lease = float(value)
    except (TypeError, ValueError):
        lease = DEFAULT_LEASE_S
    if lease <= 0:
        lease = DEFAULT_LEASE_S
    return max(MIN_LEASE_S, min(MAX_LEASE_S, lease))
