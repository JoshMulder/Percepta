"""Where H.264 goes, once the platform has said.

**Not implemented, deliberately, and this file is the record of why.** Like
`mqtt.py` beside it, this is a documented stub rather than a plausible-looking
untested client — a wire format guessed at from this side would be discovered to
be wrong by somebody debugging a black video panel over a satellite link.

What is already decided and is not this station's to choose
(`server/docs/03-realtime-isolation.md` §7, `00-topology.md` rule 8):

- **Nothing reaches a viewer from a station.** The platform terminates the
  stream and re-originates it. No peer-to-peer, no direct WebRTC.
- **Not through the broker.** Redis pub/sub carries telemetry and commands, and
  several Mbit/s of video through it would compete with the traffic it must not
  delay. The stream gets its own connection.
- **Outbound from the station.** Starlink is CGNAT: nothing can reach inward.
- **TLS, authenticated with the credential this station already holds.**

What this side would like, stated so the platform can design around it:

- **Annex B, as it comes out of the encoder.** `rpicam-vid` produces a byte
  stream of NAL units with start codes, and the Pi should hand that on
  untouched. Fragmented MP4 means muxing on a 900 MHz core that is already
  running the station, and it buys the station nothing.
- **A length-prefixed frame per access unit** is the cheapest thing to write and
  the easiest to resynchronise: four bytes of length, one byte of flags
  (keyframe), eight bytes of capture timestamp, then the access unit. The
  station already knows all three.
- **Parameter sets repeated at every keyframe** (`--inline`), so a viewer that
  attaches mid-stream decodes on the next IDR rather than never.
- **Back-pressure that the station can see.** When the link cannot carry the
  stream the station must drop frames rather than buffer them, and it needs to
  know it is happening to report it. A socket that simply blocks would turn a
  bandwidth problem into a stalled encoder.

Until then `NullUplink` exists so the encoder path can be run and measured, and
`FileUplink` so somebody with a Pi can prove the hardware encoder works before
any of the above exists.
"""

from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod

log = logging.getLogger("gsu.stream")

#: How much a diagnostic capture may write before it stops itself. A stream is
#: megabytes a second and an SD card is not large.
FILE_CAP_BYTES = 64 * 1024 * 1024


class StreamUplink(ABC):
    """One live video connection to the platform."""

    #: Shown on the console and in telemetry, so "where is this going" is
    #: answerable without reading configuration.
    name = "uplink"

    @abstractmethod
    def open(self) -> bool: ...

    @abstractmethod
    def send(self, unit) -> bool:
        """One access unit. False means it was dropped, which is allowed and
        must be counted — never queued."""

    @abstractmethod
    def close(self) -> None: ...

    def describe(self) -> str:
        return self.name


class NullUplink(StreamUplink):
    """Counts and discards. The honest state of this station's stream path.

    It exists so that the encoder, the on-demand logic and the measurements can
    all be exercised and reported, and it says what it is everywhere it appears
    — a station reporting `streaming` into this is reporting that it is encoding,
    not that anyone can see anything.
    """

    name = "none (no stream uplink implemented yet)"

    def __init__(self) -> None:
        self.units = 0
        self.bytes = 0

    def open(self) -> bool:
        log.warning(
            "Starting the encoder with no uplink: frames are being counted and "
            "discarded. The platform has not specified the stream wire format "
            "yet (CONTRACT-QUESTIONS.md item 14)."
        )
        return True

    def send(self, unit) -> bool:
        self.units += 1
        self.bytes += len(unit.data)
        return True

    def close(self) -> None:
        return None


class FileUplink(StreamUplink):
    """Writes the stream to a file, for one purpose only.

    `GSU_STREAM_SINK=/dev/shm/gsu.h264` and `python -m gsu stream` are how the
    first person with a Pi finds out whether the hardware H.264 encoder actually
    works, without needing a platform, a network or a console: start it, stop
    it, copy the file off, play it. That question is the largest hardware
    unknown in this build and it should not need the rest of the system to
    answer.

    Capped, because an unattended box must not fill its own card.
    """

    def __init__(self, path: str, cap: int = FILE_CAP_BYTES) -> None:
        self.path = path
        self.cap = cap
        self.name = f"file:{path}"
        self.bytes = 0
        self.units = 0
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
            log.error("Cannot write the stream to %s: %s", self.path, exc)
            return False
        self.bytes = 0
        self.units = 0
        self.capped = False
        log.info("Writing the H.264 stream to %s (cap %d MB).",
                 self.path, self.cap // (1024 * 1024))
        return True

    def send(self, unit) -> bool:
        with self._lock:
            if self._handle is None or self.capped:
                return False
            if self.bytes + len(unit.data) > self.cap:
                self.capped = True
                log.warning(
                    "Stopped writing %s at %.1f MB: the cap is there so a "
                    "diagnostic capture cannot fill the card.",
                    self.path, self.bytes / 1e6,
                )
                return False
            try:
                self._handle.write(unit.data)
            except OSError as exc:  # pragma: no cover - disk full, etc.
                log.error("Writing the stream failed: %s", exc)
                return False
            self.bytes += len(unit.data)
            self.units += 1
            return True

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass


def build_uplink(sink: str | None) -> StreamUplink:
    """Pick an uplink from configuration. One line to change when the real one
    exists, which is the point of it being a function."""
    if sink:
        return FileUplink(sink)
    return NullUplink()
