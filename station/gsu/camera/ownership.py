"""Who is allowed to open the camera. One question, one answer, one place.

Three fixes were shipped for the same wedge before this file existed — a lock
inside the driver, `retire()` in the agent, a relinquish in the stream — and
the Pi 2B still came up with `Camera in Acquired state trying acquire()`, a
38-second stream delivering zero frames, and no recovery short of a reboot.
Each fix was correct about the case it named. None of them could be correct
about the cases nobody had named yet, because *there was no statement of who
owns the sensor* — only a growing set of places that tried to be polite about
it.

This is that statement, and it is enforced rather than described.

**The rule.** A physical sensor has exactly one owner at a time. Ownership is
taken by name, granted as a token, and released only by the token that was
granted. Nothing opens the sensor without holding the token, and no code path
may assume it still holds one.

**Why a token and not a flag.** The zombie hold that took the first station off
the air was a driver instance nobody referenced any more, reopening the sensor
after its replacement had been built. Under a boolean, that instance's
`release()` frees its successor's hold and the two then run concurrently — the
bug wearing the fix as a disguise. A token cannot do that: `release()` compares
identity, and a stale holder's release is refused and logged. The successor's
grip is not something a predecessor can loosen.

**Why the process, not the driver.** libcamera's `Camera` object is per
process: `Camera in Acquired state trying acquire()` is emitted when *one
process* acquires twice, and it is unrecoverable for the life of that process
because the leaked acquisition belongs to a `CameraManager` singleton with no
owner left to close it. So the arbiter has to outlive any individual driver,
which is why the agent holds it and hands the same object to every camera it
builds. A rediscovery that replaces the driver does not reset who owns what.

**What this does not cover, said out loud.** A second *process* — `gsu camera`
on the CLI while the service runs — is not arbitrated here and cannot be: this
lease is in-process state. That case is the station's file lock's job
(`Agent._take_lock`), and the CLI paths that touch the sensor now take it.

**A camera with no local sensor has nothing to own.** An RTSP camera encodes
and serves on the far end; this station's preview and stream paths are both
readers of a network stream and never contend. `camera.sensor_exclusive()`
decides, and a driver that owns no sensor simply never asks for the lease.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("gsu.camera.ownership")

#: How long a would-be owner waits by default before giving up and saying who
#: has it. Long enough to outlast one `rpicam-jpeg` on a cold Pi 2B, short
#: enough that a caller is never wedged behind a holder that has itself wedged.
DEFAULT_WAIT_S = 10.0


class SensorBusy(RuntimeError):
    """The sensor could not be taken, and this says who has it.

    Carries the holder's name rather than a generic failure, because "the
    camera is busy" and "the camera is broken" are the two diagnoses this whole
    file exists to keep apart, and on a box nobody is standing next to the
    difference has to be in the message.
    """

    def __init__(self, holder: str, wanted_by: str, waited: float) -> None:
        super().__init__(
            f"the camera is held by {holder}; {wanted_by} waited {waited:.1f}s "
            f"and did not get it"
        )
        self.holder = holder
        self.wanted_by = wanted_by


class SensorLease:
    """Exclusive ownership of one physical sensor, for one process.

    Not a `threading.Lock`, though it is one underneath, and the differences
    are the point:

    * **It has a name in it.** Telemetry and the setup page can say *who* is
      holding the camera, which is the answer to the only question anybody
      asks when the picture stops.
    * **It cannot be released by the wrong caller.** See the module docstring.
    * **Refusing is normal.** `acquire()` returns `None` rather than blocking
      for ever. A preview that cannot have the sensor because the live stream
      is running is not an error, it is Tuesday, and it must not be able to
      queue up behind the stream and fire the instant it ends.
    """

    def __init__(self, name: str = "camera") -> None:
        self.name = name
        self._condition = threading.Condition()
        self._holder: str | None = None
        self._token: str | None = None
        self._since = 0.0
        self._counter = itertools.count(1)
        #: Counted so the health frame can show contention rather than leaving
        #: somebody to infer it from a gap in the frames.
        self.grants = 0
        self.refusals = 0

    # --- taking and giving back ------------------------------------------

    def acquire(self, holder: str, wait: float = 0.0) -> str | None:
        """Take the sensor for `holder`. A token, or None if somebody has it.

        `wait` is a ceiling, not a promise: an rpicam subprocess that is one
        second from finishing is worth waiting for, and one that has hung is
        not worth waiting for at all.
        """
        deadline = time.monotonic() + max(0.0, wait)
        with self._condition:
            while self._token is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.refusals += 1
                    return None
                self._condition.wait(remaining)
            token = f"{holder}#{next(self._counter)}"
            self._holder = holder
            self._token = token
            self._since = time.monotonic()
            self.grants += 1
            return token

    def release(self, token: str | None) -> bool:
        """Give the sensor back. Only the token that was granted may do it.

        A refused release is logged at warning and is not an exception: the
        caller is by definition confused about what it owns, and raising into
        a shutdown path would turn a stale reference into a dead thread. The
        log line is what somebody reads; the refusal is what protects the
        current owner.
        """
        if token is None:
            return False
        with self._condition:
            if token != self._token:
                if self._token is not None:
                    log.warning(
                        "A stale holder (%s) tried to release the %s sensor, "
                        "which is held by %s. Refused — this is exactly the "
                        "release that used to let two readers run at once.",
                        token, self.name, self._holder,
                    )
                return False
            self._holder = None
            self._token = None
            self._since = 0.0
            self._condition.notify_all()
            return True

    @contextmanager
    def held_by(self, holder: str, wait: float = DEFAULT_WAIT_S):
        """`with lease.held_by("the preview"):` — taken, and always given back.

        Raises `SensorBusy` rather than yielding None, so there is no shape of
        this block that runs without the sensor. A caller that would rather
        skip than fail uses `acquire()` and checks.
        """
        started = time.monotonic()
        token = self.acquire(holder, wait)
        if token is None:
            raise SensorBusy(self.holder or "something else", holder,
                             time.monotonic() - started)
        try:
            yield token
        finally:
            self.release(token)

    # --- what it is doing -------------------------------------------------

    @property
    def holder(self) -> str | None:
        """The current owner's name, or None. Read without the lock on
        purpose: this is for a log line and a status page, and a name that is
        one instant stale is worth more than a reader that can block."""
        return self._holder

    @property
    def free(self) -> bool:
        return self._token is None

    def held_for(self) -> float:
        since = self._since
        return max(0.0, time.monotonic() - since) if since else 0.0

    def describe(self) -> str:
        holder = self._holder
        if holder is None:
            return "free"
        return f"held by {holder} for {self.held_for():.1f}s"

    def state(self) -> dict:
        """For the health frame and the setup page."""
        return {
            "holder": self._holder,
            "held_for_s": round(self.held_for(), 1) if self._holder else 0.0,
            "grants": self.grants,
            "refusals": self.refusals,
        }
