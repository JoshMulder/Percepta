"""The Raspberry Pi camera on the CSI ribbon, under libcamera.

**This driver no longer opens libcamera inside the station's own process, and
that is the whole fix rather than a detail of it.**

The history is worth keeping, because the shape of the bug outlived three
correct-looking fixes. The driver used to have two backends: `picamera2`, a
Python API holding one long-lived camera object, and `rpicam-jpeg`, a
subprocess per frame. picamera2 existed for speed — it was the only way to make
a 2 fps snapshot channel affordable on a Pi 2B. It also put a `libcamera`
`CameraManager` inside this process, and that is the one thing that can produce
the error that took the station off the air:

    Camera in Acquired state trying acquire()

That message is emitted when *a single process* acquires the same camera twice.
It is unrecoverable for the life of that process, because the leaked
acquisition belongs to a manager singleton with no Python object left to close
it. The leak had a specific, reachable path: `Picamera2()` acquires the sensor
in its constructor, and if the `configure()` or `start()` that followed raised,
the half-built object was dropped on the floor with the acquisition still in
it — `self._camera` was never assigned, so neither `close()` nor `retire()` had
anything to close. Every later open then failed the same way, for ever, and the
driver's handler read that permanent failure as "the camera is merely busy" and
retried it every five seconds until somebody rebooted the box.

The snapshot channel that justified picamera2 has been removed
(`gsu/video.py`). What is left is one frame at a time, on demand, only while
somebody has the setup page open — and at that rate a subprocess is entirely
affordable. So there is one backend, it is `rpicam-jpeg`, and this process
contains no libcamera at all. The error above is now unreachable by
construction rather than defended against.

**Ownership is explicit.** Every capture holds the sensor lease
(`camera/ownership.py`) for exactly as long as the subprocess runs, and takes
it by name so that a log line and the setup page can say who has the camera.
The live stream holds the same lease for the length of a session. Two readers
of one sensor cannot overlap, and the one that loses says who won rather than
reporting a broken camera.

Three rules the rest of the station still relies on:

**Nothing blocks the sensing loop.** Every camera operation happens on the
preview thread or the stream's. The constructor does only cheap checks — no
subprocess — because it runs inside a tick.

**A frame is complete or it is absent.** The file is read only after the
process has exited cleanly, and the result goes through `complete_jpeg()`. A
capture that timed out, half-wrote, or returned a truncated buffer produces
`None` and a sentence, never a partial picture.

**`captured_at` is the shutter, not the send.** Taken immediately after the
capture call returns and carried on the frame from there.

A camera that is not answering backs off rather than being retried at the frame
rate: on a box nobody is standing next to, a failing subprocess twice a second
for a week is its own fault.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from .. import clock
from ..sensors import Device
from . import Frame, complete_jpeg, parse_resolution
from .ownership import SensorLease

log = logging.getLogger("gsu.camera")

#: Command-line capture tools, best first. Bookworm renamed `libcamera-*` to
#: `rpicam-*` and ships both names for now; an older image has only the former.
CLI_TOOLS = ("rpicam-jpeg", "rpicam-still", "libcamera-jpeg", "libcamera-still")

#: How long a still capture may take before it is abandoned. Generous, because
#: a cold camera on a Pi 2B is not quick, and bounded, because an unattended box
#: cannot afford a subprocess that never returns.
CAPTURE_TIMEOUT_S = 8.0

#: Preview/auto-exposure settle time given to the command-line tool. The default
#: is five seconds, which would be one frame every five seconds; this is the
#: shortest value that still lets exposure converge at all.
CLI_SETTLE_MS = 300

#: How long to leave a failed camera alone before trying again.
RETRY_SECONDS = 10.0

#: Failures in a row before the slot is reported as failed rather than silent.
FAILURES_BEFORE_FAILED = 3

#: How long a capture waits for the sensor before giving up and naming the
#: holder. Deliberately short: the preview must never queue behind a live
#: stream and then fire the instant it ends, which is the race that used to
#: kill the next stream at birth. Long enough only to absorb the overlap
#: between two captures that were dispatched close together.
LEASE_WAIT_S = 0.5


class PiCsiCamera:
    """A CSI camera, captured one still at a time, through a subprocess."""

    #: One ribbon, one owner at a time. Read by `camera.sensor_exclusive()`,
    #: which is what the preview and the stream both consult before assuming
    #: they may open anything.
    owns_sensor = True

    def __init__(
        self,
        resolution: object = "640x480",
        quality: int = 75,
        rotation: int = 0,
        camera_num: int = 0,
        sensor_lease: SensorLease | None = None,
    ) -> None:
        self.width, self.height = parse_resolution(resolution)
        self.quality = max(1, min(100, int(quality or 75)))
        self.rotation = 180 if int(rotation or 0) == 180 else 0
        self.camera_num = int(camera_num or 0)

        # Cheap checks only: this constructor runs inside a sensing tick.
        self._tool = next((tool for tool in CLI_TOOLS if shutil.which(tool)), None)
        self._backend = "rpicam" if self._tool else "none"
        #: Which capture path and why. One path now, so this says what it is
        #: and what it costs rather than which of two was picked — but it is
        #: still reported rather than inferred, because "the camera is slow"
        #: and "the camera is being shared" are different conversations.
        self.backend_reason = self._explain_backend()
        self._reason = "" if self._backend != "none" else self.backend_reason
        #: The arbiter, shared with the live stream and with whatever driver
        #: replaces this one. Owned by the agent so that it outlives any single
        #: driver instance — see camera/ownership.py. A private one when
        #: constructed standalone (tests, `gsu camera`), so the code path is
        #: the same either way and there is no "no lease" branch to get wrong.
        self.sensor_lease = sensor_lease or SensorLease("camera")
        #: Guards the driver's own bookkeeping only. The *sensor* is guarded by
        #: the lease; this is here so two threads cannot corrupt the failure
        #: counters and the retire flag while doing so.
        self._state_lock = threading.Lock()
        self._retired = False
        self._failures = 0
        self._next_attempt = 0.0
        self.frames = 0
        self.last_bytes = 0
        self.last_capture_ms = 0.0
        # Scratch file for the capture. /dev/shm keeps frames off the SD card,
        # which is the part of a Pi that wears out. The pid is in the name so
        # that a stray CLI process cannot overwrite the service's frame.
        self._scratch = os.path.join(
            "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir(),
            f"gsu-camera-{os.getpid()}.jpg",
        )

    def _explain_backend(self) -> str:
        """One sentence naming the capture path and what it costs."""
        if self._backend == "rpicam":
            return (
                f"{self._tool}, one subprocess per frame. The camera is opened "
                "only for the moment a frame is taken and is never held between "
                "them, which is what lets the live stream have it without a "
                "fight."
            )
        return (
            "no CSI camera support on this box: no rpicam-jpeg was found. "
            "`apt install rpicam-apps`."
        )

    # --- the interface --------------------------------------------------

    @property
    def backend(self) -> str:
        """`rpicam` or `none`. Public because the setup page renders it beside
        `backend_reason`."""
        return self._backend

    @property
    def status(self) -> str:
        if self._backend == "none":
            return "absent"
        if self._failures >= FAILURES_BEFORE_FAILED:
            return "failed"
        return "streaming" if self.frames else "silent"

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    def capture(self) -> Frame | None:
        """One frame, if this driver may have the sensor right now.

        The lease is taken for the length of the subprocess and released
        whatever happens. A refusal is not a failure and is deliberately not
        counted as one: the camera is working, somebody else is using it, and
        counting that as a fault is what drove the first station's rediscovery
        into rebuilding a healthy camera in a loop.
        """
        with self._state_lock:
            if self._retired:
                # Replaced during rediscovery. The successor owns the sensor,
                # and a capture that raced the replacement must not open it.
                self._reason = "this camera driver was replaced; not reopening"
                return None
            if self._backend == "none":
                return None
            if time.monotonic() < self._next_attempt:
                # Backing off. The reason from the last failure is still the
                # truth, and the station keeps reporting it rather than going
                # quiet.
                return None

        token = self.sensor_lease.acquire("the camera preview", LEASE_WAIT_S)
        if token is None:
            self._reason = (
                f"the camera is in use by "
                f"{self.sensor_lease.holder or 'something else'}"
            )
            return None
        try:
            return self._capture_held()
        finally:
            self.sensor_lease.release(token)

    def _capture_held(self) -> Frame | None:
        """One capture, with the sensor already owned by this call."""
        started = time.monotonic()
        data = self._capture_cli()
        at = clock.now()
        if not complete_jpeg(data):
            if data is not None and not self._reason:
                self._reason = (
                    f"the camera returned {len(data)} bytes that are not a "
                    "complete JPEG; the frame was dropped rather than published"
                )
            return self._failed(self._reason or "the camera returned no frame")

        with self._state_lock:
            self._failures = 0
            self._reason = ""
            self.frames += 1
            self.last_bytes = len(data)
            self.last_capture_ms = (time.monotonic() - started) * 1000
        return Frame(jpeg=data, width=self.width, height=self.height, captured_at=at)

    def raw_sample(self) -> list[str]:
        """Capture stats for the setup page's datastream field: what a frame
        costs is the camera's data stream, there being no raw bytes to show."""
        if self._retired or not self.frames:
            return []
        return [
            f"frame {self.frames}: {self.last_bytes / 1024:.1f} kB, "
            f"{self.last_capture_ms:.0f} ms via {self._backend} "
            f"(sensor {self.sensor_lease.describe()})"
        ]

    def describe(self) -> Device:
        if self._backend == "none":
            detail = self._reason
        else:
            detail = (
                f"Pi CSI camera via {self._tool}, {self.width}x{self.height}, "
                f"quality {self.quality}"
            )
            if self.frames:
                detail += (
                    f", {self.last_bytes / 1024:.1f} kB/frame, "
                    f"{self.last_capture_ms:.0f} ms/capture"
                )
            elif self._reason:
                detail += f" — {self._reason}"
        return Device(
            id="camera",
            kind="camera",
            present=self.status in ("streaming", "silent"),
            detail=detail[:200],
            simulated=False,
        )

    def close(self) -> None:
        """Nothing is held between captures, so this has nothing to release.

        Kept because the `Camera` protocol has it and the agent's shutdown
        calls it. It is deliberately **not** a relinquish any more: there is no
        long-lived handle to relinquish, which is precisely why the
        relinquish-versus-terminal distinction that the previous two fixes
        turned on no longer has anything to be ambiguous about. A caller that
        wants this driver to stop using the sensor for good calls `retire()`.
        """
        return None

    def retire(self) -> None:
        """Never capture again. For the driver being replaced.

        Still terminal, and still the thing rediscovery calls — but it now
        closes a much smaller hole. There is no lazily-reopened handle to leak,
        so a retired instance cannot reacquire the sensor behind its
        successor's back; the flag exists to stop a capture that was already
        dispatched from spending a lease the successor is waiting for.
        """
        with self._state_lock:
            self._retired = True

    # --- the backend ------------------------------------------------------

    def _capture_cli(self) -> bytes | None:
        """One JPEG through `rpicam-jpeg`, via a file rather than a pipe.

        A file, because a partial pipe read and a complete one are hard to tell
        apart under a timeout, and this is the path that must not produce half a
        picture. The file is read only after the process has exited cleanly.
        """
        if not self._tool:
            return None
        command = [
            self._tool,
            "--nopreview",
            "--timeout", str(CLI_SETTLE_MS),
            "--width", str(self.width),
            "--height", str(self.height),
            "--quality", str(self.quality),
            "--output", self._scratch,
        ]
        if self._tool.endswith("still"):
            command += ["--encoding", "jpg"]
        if self.rotation == 180:
            command += ["--rotation", "180"]
        if self.camera_num:
            command += ["--camera", str(self.camera_num)]
        try:
            os.unlink(self._scratch)
        except OSError:
            pass
        try:
            done = subprocess.run(
                command, capture_output=True, timeout=CAPTURE_TIMEOUT_S, check=False,
            )
        except subprocess.TimeoutExpired:
            self._reason = (
                f"{self._tool} did not return within {CAPTURE_TIMEOUT_S:.0f}s; "
                "the frame was abandoned"
            )
            return None
        except OSError as exc:
            self._reason = f"{self._tool} could not be run: {exc}"[:200]
            return None
        if done.returncode != 0:
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()
            # libcamera's own message is far more useful than anything this
            # code could invent — "no cameras available" is the whole diagnosis.
            self._reason = f"{self._tool} failed: {detail.splitlines()[-1]}"[:200] \
                if detail else f"{self._tool} exited {done.returncode}"
            return None
        try:
            with open(self._scratch, "rb") as handle:
                return handle.read()
        except OSError as exc:
            self._reason = f"{self._tool} wrote nothing readable: {exc}"[:200]
            return None

    # --- failure ---------------------------------------------------------

    def _failed(self, reason: str) -> None:
        with self._state_lock:
            self._failures += 1
            self._reason = reason[:200]
            self._next_attempt = time.monotonic() + RETRY_SECONDS
            failures = self._failures
        if failures == FAILURES_BEFORE_FAILED:
            log.error(
                "The camera has failed %d captures in a row: %s. Retrying every "
                "%.0fs; the preview reports it as unavailable meanwhile.",
                failures, self._reason, RETRY_SECONDS,
            )
        return None
