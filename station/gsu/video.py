"""The setup page's camera preview — and no snapshot channel at all any more.

This file used to be `VideoPublisher`: a thread capturing a JPEG twice a second
for ever and publishing it to `gsu/{station_id}/video`. That channel is gone.
It is worth being precise about why, because it was removed as a *fix*, not as
a feature cut.

**Two readers of one sensor was the whole bug.** The CSI camera is a single
device with a single owner. The snapshot loop and the live encoder were both
trying to be that owner, and every attempt to make them share — a lock in the
driver, a relinquish before the stream starts, a terminal `retire()` on
rediscovery, a 2.5-second sleep to outlast an in-flight capture — was correct
about the case it named and silent about the next one. The station still wedged
with `Camera in Acquired state trying acquire()` and a stream delivering zero
frames. Removing one of the two readers does not narrow that class of bug; it
deletes it.

**And it was hiding the fault it was supposed to reveal.** In the owner's
words: *"lets just disable all snapshot functionality for now so i can tell
what is camera not working rather than actually just snapshots"*. A black
console meant either a dead camera or a snapshot path losing a race with the
live stream, and those look identical from the far end. With no snapshot path
there is one answer.

What is left is a **preview**, and the differences from a publisher are the
design:

**It publishes nothing.** No topic, no broker, no bytes on a metered link. The
platform has the media channel for live video; a second, worse copy of the same
picture at 2 fps was never worth what it cost.

**It captures only while somebody is looking.** The setup page polls
`/status.json` every 2.5 seconds, and that poll is the demand signal —
`preview_state()` marks the preview wanted for a few seconds, and the thread
captures only inside that window. A station with nobody on the setup page opens
its camera exactly never, which leaves the live stream as the sole consumer of
the sensor on an unattended box. That is the strongest statement this file can
make about ownership, and it is made by not running rather than by being
careful.

**It never fights for the sensor.** The capture goes through the driver, and
the driver takes the lease (`camera/ownership.py`). While the live stream holds
the sensor, the preview simply does not get it — the cached frame ages, the age
is stated, and `/frame.jpg` keeps serving the newest picture there is with an
honest `X-Frame-Age`. That is the contract the console renders, unchanged.

**A frame the preview could not take is not a camera fault.** Contention is
reported as contention, naming the holder. Only a capture that was attempted
and failed counts against the camera.

The class is still reached as `agent.video` because `gsu/console.py` reads it
under that name and is owned elsewhere; `agent.preview` is the name it should
have once that file is next touched.
"""

from __future__ import annotations

import logging
import threading
import time

from . import clock
from .camera import Frame, complete_jpeg

log = logging.getLogger("gsu.video")

#: How long one `/status.json` poll keeps the preview warm. Comfortably longer
#: than the console's 2.5-second poll, so a page left open never flickers
#: between wanted and not, and short enough that closing the laptop lid stops
#: the camera being opened within a few seconds.
DEMAND_WINDOW_S = 10.0

#: The fastest the preview will take a frame, however often it is asked. The
#: console polls at 2.5 s; this is the floor under that, and under anything
#: else that starts calling `preview_state()`.
MIN_INTERVAL_S = 2.0

#: How often the thread wakes to see whether a frame is wanted. Cheap — it is
#: a clock comparison — and it sets how quickly a preview appears after the
#: setup page is first opened.
POLL_S = 0.5


class CameraPreview:
    """The newest frame the station has, taken on demand and published nowhere.

    Takes the agent rather than a camera because the camera is something the
    agent owns and may replace underneath it: a driver is rebuilt when a device
    is rediscovered. Reading it through the agent each cycle is what makes that
    replacement invisible here.
    """

    def __init__(self, agent) -> None:
        self.agent = agent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Monotonic time until which somebody is considered to be watching.
        self._wanted_until = 0.0
        self._last_attempt = 0.0
        self.captured = 0
        self.refused = 0
        self.failed = 0
        #: The newest complete frame, for `/frame.jpg` (`console._send_frame`).
        #: Written by the preview thread, read by the console's — safe as a
        #: bare attribute because Frame is frozen and the swap is a single
        #: assignment. Never cleared on a failure: a stale picture with a
        #: stated age beats no picture, and the age says stale.
        self.last_frame: Frame | None = None
        self.last_reason = ""

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="gsu-preview", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    # --- the loop -------------------------------------------------------

    def _run(self) -> None:
        log.info(
            "Camera preview ready: no snapshot channel, frames taken only while "
            "the setup page is open."
        )
        while not self._stop.is_set():
            try:
                if self.wanted:
                    self.cycle()
            except Exception:  # noqa: BLE001 - a dead thread is a dead preview
                log.exception("Preview cycle failed; continuing.")
            self._stop.wait(POLL_S)

    @property
    def wanted(self) -> bool:
        """Whether anybody has asked for a picture recently enough to matter,
        and whether enough time has passed since the last attempt."""
        now = time.monotonic()
        return (
            now < self._wanted_until
            and now - self._last_attempt >= MIN_INTERVAL_S
        )

    def request(self) -> None:
        """Somebody is looking. Keeps the preview warm for `DEMAND_WINDOW_S`.

        Called from `preview_state()`, which runs on the console's HTTP thread
        — so it does no work beyond writing a float. The capture itself belongs
        to the preview thread, because an 8-second `rpicam-jpeg` timeout inside
        a status poll would hang the setup page it is meant to serve.
        """
        self._wanted_until = time.monotonic() + DEMAND_WINDOW_S

    def cycle(self) -> bool:
        """One capture attempt. True if a new frame was taken.

        Public so that a test — and the setup page's first render — can drive
        it once without a thread. Runs with no enrolment and no topic: nothing
        goes on the wire from here at all, which is the point.
        """
        self._last_attempt = time.monotonic()
        camera = getattr(self.agent, "camera", None)
        if camera is None:
            self.last_reason = (
                self.agent.inventory.reasons.get("camera") or "no camera fitted"
            )
            # **The last picture goes with the camera.**
            #
            # Keeping a frame while a camera is merely struggling is deliberate
            # — a picture with a stated age beats a blank box. That reasoning
            # stops when there is no camera at all: the frame is then a
            # photograph of a site being served as this station's current view,
            # by a station that has no view. Somebody who sets the slot to "not
            # fitted" and goes on being shown the test card reasonably concludes
            # the change did not take.
            self.last_frame = None
            return False
        if not self.agent.site.video_enabled:
            self.last_reason = (
                f"video is switched off in this station's configuration "
                f"(version {self.agent.site.version})"
            )
            return False

        frame = camera.capture()
        if frame is None:
            # Two very different situations with one shape, and the driver has
            # already told them apart: contention names its holder, a fault
            # names the fault. Neither is invented here.
            self.last_reason = (
                getattr(camera, "unavailable_reason", "")
                or "the camera returned no frame"
            )
            if "in use by" in self.last_reason:
                self.refused += 1
            else:
                self.failed += 1
            return False
        if not complete_jpeg(frame.jpeg):
            # Checked here as well as in the driver, deliberately. Every driver
            # is supposed to refuse a partial frame and the ones in this build
            # do — but this is the last point before a picture is shown to
            # somebody, and "half a picture of a site" is the one failure the
            # contract calls out by name. A future driver that forgets cannot
            # cause it.
            self.failed += 1
            self.last_reason = (
                f"the camera returned {len(frame.jpeg)} bytes that are not a "
                "complete JPEG; the frame was dropped rather than shown"
            )
            log.warning("%s", self.last_reason)
            return False

        self.captured += 1
        self.last_frame = frame
        self.last_reason = ""
        return True

    # --- what the console reads -----------------------------------------

    def frame_age_s(self) -> float | None:
        """How old the cached frame is, from its own `captured_at`.

        The one number the preview must not lie about: while the live stream
        holds the sensor this frame is deliberately not replaced, and the age
        is what says so.
        """
        frame = self.last_frame
        if frame is None:
            return None
        return max(0.0, (clock.now() - frame.captured_at).total_seconds())

    def preview_state(self) -> dict:
        """What the setup page needs to render the preview — and the demand
        signal that makes a frame exist at all.

        Local console only, deliberately not in the health frame: the health
        frame goes over a metered link and the platform has the media channel.
        """
        self.request()
        state: dict = {"has_frame": self.last_frame is not None}
        age = self.frame_age_s()
        if age is not None:
            state["frame_age_s"] = round(age, 1)
        if self.last_reason:
            state["reason"] = self.last_reason
        return state

    def stats(self) -> dict:
        """What the preview is doing. Costs nothing on the link by
        construction, so there is no bitrate here to report — that number was
        the snapshot channel's, and the snapshot channel is gone."""
        return {
            # Named so that a reader of the health frame cannot mistake this
            # for the old snapshot channel merely being idle.
            "snapshots": False,
            "snapshots_removed": (
                "the periodic JPEG channel was removed: two readers of one "
                "sensor was the cause of the camera wedge, and live video is "
                "the media channel's job"
            ),
            "preview_frames": self.captured,
            "preview_refused": self.refused,
            "preview_failed": self.failed,
            "watching": time.monotonic() < self._wanted_until,
            "captured_at": (
                self.last_frame.captured_at.isoformat() if self.last_frame else None
            ),
            "reason": self.last_reason,
        }
