"""The Raspberry Pi camera on the CSI ribbon, under libcamera.

**Now run against hardware.** This driver met its first real camera — an ov5647
on a Pi 2B (Raspbian 13, armhf) — in July 2026, and the picamera2 path carries
the live station's 2 fps snapshot channel today. HARDWARE.md §7 is the register
of what is measured and what still is not. It remains written to fail in ways
somebody can diagnose from a log line rather than to look convincing, which is
how every fault the hardware surfaced was found.

Bookworm dropped the old `raspistill`/MMAL stack for libcamera, so there are two
ways in and this driver takes whichever is there:

    picamera2      the Python API, if it imports. One long-lived camera object,
                   configured once, grabbing a JPEG per frame.
    rpicam-jpeg    a subprocess per frame, writing to a file we then read. Slower
                   by a long way - a process start, camera open and AE settle
                   every frame - but it depends on nothing but `rpicam-apps`,
                   which a stock Bookworm or Trixie image already has.

Which of the two a box gets is not left to chance and is not silent. `picamera2`
is a Debian package (`python3-picamera2`) bound to the system's libcamera build
and cannot be pip-installed, so a virtual environment made without
`--system-site-packages` cannot import it however well it is installed - and the
station then runs the slow path, which looks exactly like a slow camera.
`backend_reason` says which path and why, and it is carried into the device
inventory, the setup page and the health frame.

Three rules the rest of the station relies on, all of them enforced here:

**Nothing blocks the sensing loop.** Every camera operation happens on the video
thread. The constructor does only cheap checks - no import of picamera2, no
subprocess - because it runs inside a tick.

**A frame is complete or it is absent.** The subprocess path reads the file only
after the process has exited cleanly, and both paths go through
`complete_jpeg()`. A capture that timed out, half-wrote, or returned a truncated
buffer produces `None` and a sentence, never a partial picture.

**`captured_at` is the shutter, not the send.** Taken immediately after the
capture call returns and carried on the frame from there. It is the closest this
software can get to the exposure without the camera telling it - which picamera2
can, in request metadata, and which is worth revisiting on hardware.

A camera that is not answering backs off rather than being retried at the frame
rate: on a box nobody is standing next to, a failing subprocess twice a second
for a week is its own fault.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .. import clock
from ..sensors import Device
from . import Frame, complete_jpeg, parse_resolution

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


class PiCsiCamera:
    """A CSI camera, captured one still at a time."""

    def __init__(
        self,
        resolution: object = "640x480",
        quality: int = 75,
        rotation: int = 0,
        camera_num: int = 0,
    ) -> None:
        self.width, self.height = parse_resolution(resolution)
        self.quality = max(1, min(100, int(quality or 75)))
        self.rotation = 180 if int(rotation or 0) == 180 else 0
        self.camera_num = int(camera_num or 0)

        # Cheap checks only: this constructor runs inside a sensing tick.
        self._picamera2_installed = importlib.util.find_spec("picamera2") is not None
        self._tool = next((tool for tool in CLI_TOOLS if shutil.which(tool)), None)
        self._backend = "picamera2" if self._picamera2_installed else (
            "cli" if self._tool else "none"
        )
        #: Why this backend and not the faster one. Reported, never inferred:
        #: the subprocess path is several times slower per frame, and "the
        #: camera is slow" and "the fast path was unreachable for a packaging
        #: reason" are the same symptom with completely different fixes.
        self.backend_reason = self._explain_backend()
        self._camera = None            # a Picamera2, once one has been opened
        self._reason = "" if self._backend != "none" else self.backend_reason
        # One lock over open, capture and close. The publisher thread captures,
        # the commands thread closes when a stream starts, and unserialized
        # they double-opened picamera2 - the second __init__ found the sensor
        # held BY ITS OWN SIBLING, died half-built, and leaked the acquisition
        # for the life of the process. The original "no locking, runs inside a
        # tick" comment was written when there was one consumer; there are two.
        self._io_lock = threading.Lock()
        self._failures = 0
        self._next_attempt = 0.0
        self.frames = 0
        self.last_bytes = 0
        self.last_capture_ms = 0.0
        # Scratch file for the command-line path. /dev/shm keeps a frame twice a
        # second off the SD card, which is the part of a Pi that wears out.
        self._scratch = os.path.join(
            "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir(),
            f"gsu-camera-{os.getpid()}.jpg",
        )

    def _explain_backend(self) -> str:
        """One sentence naming the capture path and why it is that one.

        The case worth catching is specific and silent: `picamera2` is a Debian
        package on Raspberry Pi OS, so a virtual environment created **without**
        `--system-site-packages` cannot import it however well it is installed.
        The station then falls back to a subprocess per frame, which works — and
        looks exactly like a slow camera rather than like a packaging choice.
        """
        if self._backend == "picamera2":
            return "picamera2 is importable; using it (one process, frames in memory)"
        if self._backend == "cli":
            if self._venv_hides_system_packages():
                return (
                    f"picamera2 is not importable from this virtual environment "
                    f"({sys.prefix}), which was created without "
                    "--system-site-packages — picamera2 is a system package and "
                    f"cannot be pip-installed. Using {self._tool}, a subprocess "
                    "per frame. Recreate the venv with --system-site-packages to "
                    "use the faster path."
                )
            return (
                f"picamera2 is not installed; using {self._tool}, which is a "
                "subprocess per frame. `apt install python3-picamera2` for the "
                "faster path."
            )
        return (
            "no CSI camera support on this box: picamera2 is not importable and "
            "no rpicam-jpeg was found. Install python3-picamera2 or rpicam-apps."
        )

    @staticmethod
    def _venv_hides_system_packages() -> bool:
        """Whether this interpreter is a venv that cannot see system packages."""
        if sys.prefix == sys.base_prefix:
            return False
        try:
            with open(os.path.join(sys.prefix, "pyvenv.cfg")) as handle:
                for line in handle:
                    name, _, value = line.partition("=")
                    if name.strip() == "include-system-site-packages":
                        return value.strip().lower() != "true"
        except OSError:
            pass
        return True

    # --- the interface --------------------------------------------------

    @property
    def backend(self) -> str:
        """`picamera2`, `cli` or `none`. Public because the setup page renders
        it beside `backend_reason` — the pair is how "why is the camera slow"
        gets answered without an SSH session."""
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
        with self._io_lock:
            return self._capture_locked()

    def _capture_locked(self) -> Frame | None:
        if self._backend == "none":
            return None
        if time.monotonic() < self._next_attempt:
            # Backing off. The reason from the last failure is still the truth,
            # and the station keeps publishing it rather than going quiet.
            return None

        started = time.monotonic()
        data = None
        if self._backend == "picamera2":
            data = self._capture_picamera2()
            if data is None and self._backend == "cli":
                # picamera2 disqualified itself mid-capture; try the fallback in
                # the same tick rather than losing a frame to the switch.
                data = self._capture_cli()
        else:
            data = self._capture_cli()

        at = clock.now()
        if not complete_jpeg(data):
            if data is not None and not self._reason:
                self._reason = (
                    f"the camera returned {len(data)} bytes that are not a "
                    "complete JPEG; the frame was dropped rather than published"
                )
            return self._failed(self._reason or "the camera returned no frame")

        self._failures = 0
        self._reason = ""
        self.frames += 1
        self.last_bytes = len(data)
        self.last_capture_ms = (time.monotonic() - started) * 1000
        return Frame(jpeg=data, width=self.width, height=self.height, captured_at=at)

    def describe(self) -> Device:
        if self._backend == "none":
            detail = self._reason
        else:
            detail = (
                f"Pi CSI camera via {self._backend}, {self.width}x{self.height}, "
                f"quality {self.quality}"
            )
            if self.frames:
                detail += (
                    f", {self.last_bytes / 1024:.1f} kB/frame, "
                    f"{self.last_capture_ms:.0f} ms/capture"
                )
            elif self._reason:
                detail += f" — {self._reason}"
            if self._backend == "cli":
                # Said on every line that describes this camera, not once at
                # start-up in a log nobody keeps: the slow path is a standing
                # condition, and the reason for it is what somebody acts on.
                detail += f" [{self.backend_reason}]"
        return Device(
            id="camera",
            kind="camera",
            present=self.status in ("streaming", "silent"),
            detail=detail[:200],
            simulated=False,
        )

    def close(self) -> None:
        with self._io_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        camera, self._camera = self._camera, None
        if camera is not None:
            for method in ("stop", "close"):
                try:
                    getattr(camera, method)()
                except Exception:  # noqa: BLE001 - shutting down, say so and go on
                    # Loud, and remembered. A close that failed can leave
                    # picamera2's camera manager holding the sensor in Running
                    # state, and the next Picamera2() then dies half-built and
                    # LEAKS the acquisition - after which nothing in this
                    # process, snapshots or encoder, can ever take the camera
                    # again. That wedge took the first real station off the
                    # air until a restart. Once a close has failed, picamera2
                    # is not trusted again this run; the cli path costs a
                    # subprocess per frame and cannot poison the manager.
                    log.warning("Camera %s() failed during shutdown; not "
                                "trusting picamera2 again this run.",
                                method, exc_info=True)
                    self._close_failed = True
        try:
            os.unlink(self._scratch)
        except OSError:
            pass

    # --- backends -------------------------------------------------------

    #: Set when a picamera2 close() ever fails. From then on the cli path is
    #: used unconditionally - see close() for the wedge this prevents.
    _close_failed = False

    #: Monotonic time before which picamera2 must not be reopened. A failed
    #: open is retried, but not at the snapshot rate: hammering a busy sensor
    #: at 2 Hz is how the fatal half-open happens in the first place.
    _reopen_after = 0.0

    def _capture_picamera2(self) -> bytes | None:
        """One JPEG through the Python API.

        The camera object is opened once and kept: opening it costs the best
        part of a second, and doing that per frame would put the frame rate out
        of reach before anything else did.
        """
        if self._close_failed:
            return self._capture_cli()
        if self._camera is None and time.monotonic() < self._reopen_after:
            return None
        try:
            if self._camera is None:
                self._camera = self._open_picamera2()
            import io

            buffer = io.BytesIO()
            self._camera.capture_file(buffer, format="jpeg")
            return buffer.getvalue()
        except Exception as exc:  # noqa: BLE001 - reported, never raised upward
            text = str(exc).lower()
            if self._camera is None and (
                "running state" in text or "busy" in text
                or "did not complete" in text or "allocator" in text
            ):
                # The sensor is merely held - by the encoder winding down, or
                # by a subprocess finishing. That is weather, not a fault:
                # falling back to cli here would silently abandon the fast
                # path forever over a two-second contention. Wait it out and
                # try again, but not at the snapshot rate.
                self._reopen_after = time.monotonic() + 5.0
                log.info("picamera2 could not open (camera busy); retrying "
                         "in 5s without changing backend.")
                return None
            self._drop_picamera2(exc)
            return None

    def _open_picamera2(self):
        from picamera2 import Picamera2  # noqa: PLC0415 - deliberately lazy

        camera = Picamera2(self.camera_num)
        options = {"quality": self.quality}
        config = {"main": {"size": (self.width, self.height)}}
        if self.rotation == 180:
            try:
                from libcamera import Transform  # noqa: PLC0415

                config["transform"] = Transform(hflip=1, vflip=1)
            except Exception:  # noqa: BLE001
                log.warning(
                    "libcamera.Transform is unavailable; the camera will not be "
                    "rotated. Mount the camera the right way up or use the "
                    "command-line path."
                )
        camera.configure(camera.create_video_configuration(**config))
        camera.options.update(options)
        camera.start()
        log.info(
            "Pi camera open via picamera2 at %dx%d, quality %d.",
            self.width, self.height, self.quality,
        )
        return camera

    def _drop_picamera2(self, exc: Exception) -> None:
        """Give up on picamera2 and use the command-line tool instead.

        The likeliest cause on a stock image is a missing dependency of
        `capture_file` rather than a missing camera, and the two need different
        answers from whoever reads this. Both are stated.
        """
        camera, self._camera = self._camera, None
        if camera is not None:
            try:
                camera.close()
            except Exception:  # noqa: BLE001
                pass
        if self._tool:
            self._backend = "cli"
            self._reason = f"picamera2 failed ({exc}); using {self._tool} instead"
            log.warning(
                "picamera2 could not capture (%s). Falling back to %s, which is "
                "slower per frame. If the camera itself is missing, %s will fail "
                "too and say so.", exc, self._tool, self._tool,
            )
        else:
            self._reason = (
                f"picamera2 failed ({exc}) and no rpicam-jpeg is installed to "
                "fall back to"
            )[:200]

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
        self._failures += 1
        self._reason = reason[:200]
        self._next_attempt = time.monotonic() + RETRY_SECONDS
        if self._failures == FAILURES_BEFORE_FAILED:
            log.error(
                "The camera has failed %d captures in a row: %s. Retrying every "
                "%.0fs; video is being published as unavailable meanwhile.",
                self._failures, self._reason, RETRY_SECONDS,
            )
        return None
