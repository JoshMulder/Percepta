"""Publishing video, which is the heaviest thing this station will ever send.

`gsu/{station_id}/video`, one complete JPEG per message, per
`contract/schemas/video.schema.json`. Four decisions are worth having in one
place, because each of them is a rule the rest of the code assumes.

**It runs on its own thread, not in the sensing tick.** A capture can take a
second — a subprocess start, a camera open, auto-exposure settling — and the
sensing loop has a squelch to run at 1 Hz. Video is also the one stream whose
cadence is not a multiple of the tick, so it would need its own clock even if
capture were instant.

**Nothing is ever queued.** A frame that cannot be sent now is dropped, exactly
as telemetry is: on a link that comes back after two minutes, a two-minute-old
picture of a site is worse than no picture, because it is presented as the view.
`contract/transport.md` — favour dropping data over queueing it.

**A stream with no camera says so, on a cadence, and never goes quiet.** Same
rule as telemetry. The unavailable frame is sent at most once a second, which is
telemetry's own cadence for the same statement, and no faster than the video
frame rate: at ninety bytes it is nothing next to a frame, and a console that
stops hearing anything cannot tell "no camera" from "station gone".

**The station measures what it costs and reports it.** Bytes per frame and the
resulting bitrate are measured from the encoded payload, over a rolling minute,
and carried in the health frame and on the local console. On a metered link the
figure that matters is the one from the hardware that is actually fitted, and
that is a number nobody has until the box is on the hill — so the box states it.

On-demand publishing is the right end state and is not built: see
CONTRACT-QUESTIONS.md item 13. Until the platform can ask, this publishes
continuously at a low rate, and `video_enabled` in the site configuration is the
one lever the platform has — `config.set` turns it off, which is the honest
manual version of what should be automatic.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from .camera import complete_jpeg

log = logging.getLogger("gsu.video")

#: Frame-rate bounds. The upper one is not a capability claim — it is a guard
#: against a configuration typo turning a metered link into a fire hose.
MIN_FPS = 0.05
MAX_FPS = 10.0

#: The slowest an `available: false` frame is sent, and the fastest. Bounded on
#: both sides: often enough that the console knows the station is alive, rarely
#: enough that saying "no camera" is never a bandwidth decision.
UNAVAILABLE_MAX_HZ = 1.0

#: How long the measured bitrate looks back.
WINDOW_SECONDS = 60.0

#: How long to wait before retrying a topic the broker refused. Long, because
#: the fix is a change on the platform, and short enough that nobody has to
#: restart a station on a hillside once it lands.
REFUSAL_RETRY_SECONDS = 300.0


class VideoPublisher:
    """Captures frames and publishes them, on its own schedule.

    Takes the agent rather than a pile of callbacks because everything it needs
    — the camera, the site configuration, the transport, the health register —
    is something the agent already owns and may replace underneath it: a camera
    is rebuilt when a device is rediscovered, and the transport is rebuilt on
    every re-attach. Reading them through the agent each cycle is what makes
    those replacements invisible here.
    """

    def __init__(self, agent) -> None:
        self.agent = agent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._window: deque[tuple[float, int]] = deque()
        self.published = 0
        self.dropped = 0
        self.captured = 0
        self.last_bytes = 0
        self.last_captured_at = None
        self.last_reason = ""
        self._refused_until = 0.0
        self._last_unavailable = 0.0

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gsu-video", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    # --- the loop -------------------------------------------------------

    def _run(self) -> None:
        log.info(
            "Video publisher started: %s at %.2f fps to %s.",
            "enabled" if self.agent.site.video_enabled else "disabled by configuration",
            1 / self.interval, self.topic or "(not enrolled)",
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.cycle()
            except Exception:  # noqa: BLE001 - a dead thread is a dead camera
                log.exception("Video cycle failed; continuing.")
            delay = self.interval - (time.monotonic() - started)
            self._stop.wait(max(0.0, delay))

    @property
    def interval(self) -> float:
        fps = min(MAX_FPS, max(MIN_FPS, float(self.agent.site.video_fps or 2.0)))
        return 1.0 / fps

    def cycle(self) -> bool:
        """One capture-and-publish. True if a frame went out.

        Public so that a test — and `gsu bench` — can drive it a frame at a time
        without a thread.
        """
        topic = self.topic
        if topic is None:
            return False
        if time.monotonic() < self._refused_until:
            return False

        camera = getattr(self.agent, "camera", None)
        if camera is None or not self.agent.site.video_enabled:
            return self._publish_unavailable(topic)
        if self._stream_has_the_camera(camera):
            # One sensor, one user. While `rpicam-vid` holds the CSI camera a
            # snapshot capture fails with a device-busy, which would be reported
            # as a broken camera when in fact it is a working one being used for
            # something better. Say which.
            self.last_reason = (
                "the camera is in use by the live stream; snapshots resume when "
                "it stops"
            )
            return self._publish_unavailable(topic)

        frame = camera.capture()
        if frame is None or not complete_jpeg(frame.jpeg):
            # Checked here as well as in the driver, deliberately. Every driver
            # is supposed to refuse a partial frame and the two in this build
            # do — but this is the last point before it goes on the wire, and
            # "half a picture of a site" is the one failure the contract calls
            # out by name. A future driver that forgets cannot cause it.
            if frame is not None:
                self.last_reason = (
                    f"the camera returned {len(frame.jpeg)} bytes that are not a "
                    "complete JPEG; the frame was dropped rather than published"
                )
                log.warning("%s", self.last_reason)
            else:
                self.last_reason = (
                    getattr(camera, "unavailable_reason", "")
                    or "the camera returned no frame"
                )
            return self._publish_unavailable(topic)

        self.captured += 1
        self.last_captured_at = frame.captured_at
        self.last_reason = ""
        payload = frame.to_payload()
        # Measured from the encoded payload rather than from the JPEG: base64
        # and the JSON around it are real bytes on a metered link, and quoting
        # the JPEG size alone would understate the cost by a third.
        size = len(payload["jpeg"]) + 160
        self.last_bytes = size
        if self._send(topic, payload):
            self.published += 1
            self._window.append((time.monotonic(), size))
            self._trim()
            return True
        self.dropped += 1
        return False

    def _stream_has_the_camera(self, camera) -> bool:
        """Whether the live encoder is holding the hardware.

        Only for a real camera: the synthetic source is a drawing routine and
        two of them can run at once, which matters because it is the
        configuration the platform tests against — losing snapshots there would
        be an artefact of the test rig rather than of the design.
        """
        stream = getattr(self.agent, "stream", None)
        # "starting" counts as held: the encoder is about to open the sensor
        # and a snapshot dispatched in that window would win the race and kill
        # the stream at birth. On a Pi 2B a snapshot subprocess runs for the
        # best part of a second, so at 2 fps that window is most of the time.
        if stream is None or stream.state not in ("streaming", "starting"):
            return False
        describe = getattr(camera, "describe", None)
        return not (describe and describe().simulated)

    # --- publishing -----------------------------------------------------

    def _publish_unavailable(self, topic: str) -> bool:
        """Say there is no picture, on a cadence, with a reason.

        Rate-limited separately from the frame rate: the statement is worth 90
        bytes a second at most, and a station that goes quiet instead is
        indistinguishable from one that has died.
        """
        now = time.monotonic()
        if now - self._last_unavailable < max(self.interval, 1.0 / UNAVAILABLE_MAX_HZ):
            return False
        self._last_unavailable = now
        self._send(topic, self.unavailable_payload())
        return False

    def unavailable_payload(self) -> dict:
        """`available: false`, and why, in an operator's words.

        The driver's own reason wins when it has one — "rpicam-jpeg failed: no
        cameras available" tells somebody what to do, where "camera not
        detected" only tells them something is wrong. The inventory's reason is
        the fallback, and it is the right one when nothing is fitted at all.
        """
        camera = getattr(self.agent, "camera", None)
        if camera is not None and not self.agent.site.video_enabled:
            reason = (
                f"video is switched off in this station's configuration "
                f"(version {self.agent.site.version})"
            )
        else:
            reason = self.last_reason or (
                getattr(camera, "unavailable_reason", "") if camera is not None else ""
            )
        payload = self.agent.unavailable_payload("video")
        if reason:
            payload["unavailable_reason"] = reason[:200]
        return payload

    def _send(self, topic: str, payload: dict) -> bool:
        if self.agent._publish(topic, payload):
            return True
        self._check_refused(topic)
        return False

    def _check_refused(self, topic: str) -> None:
        """Tell a refused channel from a link that is merely down.

        They look identical from a failed publish and are completely different
        faults: one is weather, the other is a station publishing into a channel
        its broker principal was never granted. The second cannot fix itself and
        needs a named change on the platform, so it is said in those terms.
        """
        transport = self.agent.transport
        detail = (getattr(transport, "refusals", None) or {}).get(topic)
        if not detail:
            return
        self._refused_until = time.monotonic() + REFUSAL_RETRY_SECONDS
        self.agent.health.raise_condition(
            "video.topic_refused", "warning",
            f"The broker refused {topic} ({detail}). This station's principal is "
            "granted telemetry, audio and commands only, so video cannot be "
            "published until the video channel is added to the broker ACL and "
            "the enrolment response carries a video topic. Retrying every "
            f"{REFUSAL_RETRY_SECONDS / 60:.0f} minutes; nothing else is affected.",
        )
        log.error(
            "Video is refused by the broker on %s: %s. See CONTRACT-QUESTIONS.md "
            "item 12 — the platform has to grant the channel.", topic, detail,
        )

    @property
    def topic(self) -> str | None:
        enrolment = self.agent.enrolment
        return enrolment.broker.resolve_video_topic() if enrolment else None

    # --- what it cost ---------------------------------------------------

    def _trim(self) -> None:
        cutoff = time.monotonic() - WINDOW_SECONDS
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def stats(self) -> dict:
        """What video is actually costing, measured rather than intended.

        Carried in the health frame and rendered on the setup page, because "how
        much of the link is the camera using" is the first question anyone asks
        about a metered site and the last one a log file answers.
        """
        self._trim()
        total = sum(size for _, size in self._window)
        span = WINDOW_SECONDS
        if len(self._window) > 1:
            span = max(1.0, self._window[-1][0] - self._window[0][0])
        return {
            "enabled": bool(self.agent.site.video_enabled),
            "fps_configured": round(1 / self.interval, 2),
            "fps_measured": round(len(self._window) / span, 2) if self._window else 0.0,
            "frames_published": self.published,
            "frames_dropped": self.dropped,
            "bytes_per_frame": self.last_bytes,
            # Bits per second on the wire, over the last minute of frames that
            # actually left the box.
            "bitrate_bps": round(total * 8 / span) if self._window else 0,
            "captured_at": self.last_captured_at.isoformat() if self.last_captured_at else None,
            "refused": time.monotonic() < self._refused_until,
            "reason": self.last_reason,
        }
