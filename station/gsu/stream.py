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
from .camera.h264 import StreamSettings, choose_encoder, probe_encoders
from .media.fmp4 import Fmp4Muxer
from .transport.stream import build_uplink, media_url

log = logging.getLogger("gsu.stream")

#: Lease bounds. Five seconds is the shortest that survives one missed command
#: on a link that drops; five minutes is the longest anyone should be able to
#: commit the link to without asking again.
MIN_LEASE_S = 5.0
MAX_LEASE_S = 300.0
DEFAULT_LEASE_S = 30.0


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
        self._session_open = False
        self._last_frame_at: float | None = None

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

        if self.state == "streaming":
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
        uplink = build_uplink(
            self.agent.config, self.agent.enrolment, trust=self.agent.api_trust,
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

        # The snapshot path must let go of the sensor before the encoder can
        # take it, and there are two different holds to break. Under picamera2
        # the camera is one long-lived object held between snapshots - the very
        # thing that makes that path fast - so it is closed here and reopens
        # lazily on the first snapshot after the stream ends. Under the cli
        # backend each snapshot is a subprocess that owns the sensor for the
        # best part of a second on this hardware, so at 2 fps the sensor is
        # busy more often than not: "starting" makes the publisher hold off
        # dispatching another (video.py, _stream_has_the_camera), and the
        # retries below outlast the one already in flight. Neither hold was
        # visible on a box where the camera is synthetic, which is every box
        # this ran on before a real one.
        # Only where snapshots and the encoder share one physical sensor — a
        # network camera serves both readers at once, and the synthetic pair
        # can run together. One predicate decides, shared with video.py.
        self.state = "starting"
        camera = getattr(self.agent, "camera", None)
        if sensor_exclusive(camera) and hasattr(camera, "close"):
            camera.close()
            # And wait out the capture already running. close() frees the
            # picamera2 hold, but a cli snapshot is a subprocess that owns the
            # sensor until it finishes, and it cannot be signalled - only
            # outlasted. The encoder spawns cleanly either way and dies
            # asynchronously when acquisition fails, which is why retrying the
            # spawn never helped. Two and a half seconds covers the slowest
            # capture this hardware has produced; a sensor-exclusive camera is
            # the only case that needs it, and the platform's viewer is
            # already seconds of tickets and websockets away from instant.
            time.sleep(2.5)

        self.frames = 0
        self.bytes_out = 0
        self.dropped = 0
        self._last_frame_at = None
        self._session_open = False
        self.codec = ""
        # One muxer per session. It holds the parameter sets and the decode
        # clock, so a new stream starts at zero rather than continuing a
        # timeline the platform has already forgotten.
        self.muxer = Fmp4Muxer(settings.width, settings.height, settings.fps)

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
        log.info(
            "Streaming %dx%d at %d fps, %d kbit/s target, to %s. Lease %.0fs.",
            settings.width, settings.height, settings.fps, settings.bitrate_kbps,
            uplink_name, lease,
        )
        self.agent.store.record_event(
            "video.stream_started", "info",
            f"Live stream started at {settings.width}x{settings.height}/"
            f"{settings.fps} for {self.viewers} viewer(s).",
        )
        return (
            f"streaming {settings.width}x{settings.height} at {settings.fps} fps "
            f"to {uplink_name}, lease {lease:.0f}s"
        )

    def stop(self, reason: str = "stopped by the platform") -> str:
        """`video.stop`, and every other way this ends. Safe to call at any
        time, including when nothing is running."""
        self.viewers = 0
        self.expires_at = None
        if self.state != "streaming":
            self.state = "idle"
            return "not streaming"
        source, self.source = self.source, None
        uplink, self.uplink = self.uplink, None
        self.muxer = None
        self._session_open = False
        if source is not None:
            source.stop()
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

    # --- the frames -----------------------------------------------------

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

        fragment, keyframe, changed = muxer.feed(unit)
        if changed or not self._session_open:
            # A new encoder session: parameters that no longer match decode as
            # corruption rather than as an error, so the platform is told to
            # discard what it was holding before the new segment arrives.
            segment = muxer.init_segment()
            if segment is None:
                # No parameter sets yet, so there is nothing a decoder could do
                # with a fragment. Waiting is correct; the next keyframe carries
                # them, and `--inline` means that is at most one keyframe away.
                return
            self.codec = muxer.codec()
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
                "no camera fitted, so there is nothing to stream. The video "
                "channel is publishing available: false for the same reason."
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
        # A remux source runs at the camera's own configured rate — the
        # station copies what arrives and cannot change it. The muxer's clock
        # is built from this figure, and pacing it to anything else plays the
        # stream fast or slow at the far end.
        camera_fps = getattr(camera, "stream_fps", None)
        if camera_fps:
            fps = float(camera_fps)
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
            "requested": {
                "width": settings.width, "height": settings.height,
                "fps": settings.fps, "bitrate_kbps": settings.bitrate_kbps,
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
