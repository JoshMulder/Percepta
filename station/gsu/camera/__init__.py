"""Cameras, and the one rule that matters more than any of them.

`contract/schemas/video.schema.json` is Motion JPEG: one complete, independent
frame per message. Two consequences run through every file in this package.

**A frame is complete or it is absent.** A truncated JPEG is dropped and never
published — `complete_jpeg()` is the gate, and every driver goes through it. Half
a picture of a site is worse than no picture, because it invites an operator to
believe they have seen something. Nothing here ever publishes what it could not
finish reading.

**`captured_at` is when the shutter went, not when the frame was sent.** It is
stamped in the driver, at capture, and carried on the frame object from there —
never filled in by the publisher, which runs later and, on a link that buffers,
possibly much later. The age of the picture is the thing an operator most needs
and most easily assumes.

Two implementations behind one interface, exactly as `sensors/` does it:

    picsi.py       the real CSI camera, through `rpicam-jpeg` — a subprocess
                   per frame, and no libcamera inside this process. There used
                   to be a second, faster backend holding a camera object open
                   between frames; it was the only thing that could wedge the
                   sensor for the life of a run, and the 2 fps channel that
                   justified it is gone. See HARDWARE.md §7
    synthetic.py   a generated test card, which says SYNTHETIC on its face

and `jpeg.py`, a small baseline JPEG writer that exists **only** for the
synthetic source. Real cameras produce their own JPEG in hardware and never go
near it.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from ..sensors import Device

#: Start of image / end of image. A JPEG that does not begin and end with these
#: is a partial read, whatever its length.
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

#: Below this, it cannot be a real frame however well-formed it looks. Chosen
#: well under the smallest plausible frame rather than near it: the check is for
#: truncation, not for quality.
MIN_JPEG_BYTES = 128


def complete_jpeg(data: bytes | None) -> bool:
    """Whether this is a whole JPEG, rather than as much of one as arrived.

    Trailing NULs are tolerated because some capture paths pad to a block
    boundary; a missing `EOI` is not, because that is precisely what a truncated
    read looks like. This is deliberately cheap — it runs on every frame — and
    deliberately not a decode: the station has no decoder and does not need one
    to know it was cut off.
    """
    if not data or len(data) < MIN_JPEG_BYTES:
        return False
    return data.startswith(SOI) and data.rstrip(b"\x00").endswith(EOI)


def iso(at: datetime) -> str:
    """ISO 8601 in UTC with a `Z`, as the schema's example writes it."""
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Frame:
    """One complete JPEG, and when it was taken.

    Immutable on purpose: the timestamp travels with the pixels from the driver
    that captured them, and there is no way for anything downstream to restamp
    it with its own idea of now.
    """

    jpeg: bytes
    width: int
    height: int
    captured_at: datetime

    @property
    def bytes_on_wire(self) -> int:
        """Roughly what this frame costs to publish.

        base64 is 4 bytes out per 3 in, plus the JSON around it. Approximate,
        and only used for reporting — the number the station actually reports as
        its bitrate is measured from the encoded payload, not from this.
        """
        return (len(self.jpeg) + 2) // 3 * 4 + 160

    def to_payload(self) -> dict:
        return {
            "kind": "video",
            "format": "mjpeg",
            "jpeg": base64.b64encode(self.jpeg).decode("ascii"),
            "width": self.width,
            "height": self.height,
            "captured_at": iso(self.captured_at),
        }


@runtime_checkable
class Camera(Protocol):
    def capture(self) -> Frame | None:
        """One complete frame, or None with a reason in `unavailable_reason`.

        Never a partial frame, never a black one standing in for a failure, and
        never an exception into the publishing loop: a camera that has stopped
        answering is a `None` and a sentence, which is what the station puts on
        the wire as `available: false`.
        """

    @property
    def unavailable_reason(self) -> str:
        """Why the last capture produced nothing. Short, and for a person."""

    def describe(self) -> Device: ...

    def close(self) -> None: ...


def jpeg_dimensions(data: bytes | None) -> tuple[int, int] | None:
    """Width and height out of a JPEG's own start-of-frame marker.

    The RTSP driver needs this: the frame size is whatever the camera is
    configured to send, not a parameter this station holds, and the video
    schema requires real dimensions on every frame. Walks the marker segments
    to the SOF (C0-C3, C5-C7, C9-CB, CD-CF — everything but DHT/DAC/RST),
    which carries height then width, big-endian, after the precision byte.
    Returns None rather than guessing when the structure is not there.
    """
    if not data or len(data) < 4 or not data.startswith(SOI):
        return None
    index = 2
    length = len(data)
    while index + 4 <= length:
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2               # markers with no payload
            continue
        segment = int.from_bytes(data[index + 2:index + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if index + 9 > length:
                return None
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return (width, height) if width and height else None
        index += 2 + segment
    return None


def sensor_exclusive(camera) -> bool:
    """Whether snapshots and the live encoder contend for one physical sensor.

    True for the CSI camera: one ribbon, one owner at a time, so the snapshot
    path has to relinquish before the encoder starts and hold off while it
    runs. False for anything that declares `owns_sensor = False` — a network
    camera does its own capture and encode, and both of this station's paths
    are just readers — and false for the synthetic source, which is a drawing
    routine two of which can run at once. The stream and snapshot paths both
    ask this one function so they cannot drift apart on the answer.
    """
    if camera is None:
        return False
    if not getattr(camera, "owns_sensor", True):
        return False
    describe = getattr(camera, "describe", None)
    return not (describe and describe().simulated)


def parse_resolution(value: object, default: tuple[int, int] = (640, 480)) -> tuple[int, int]:
    """`"640x480"` → `(640, 480)`, tolerantly.

    A resolution typed into a setup form is a place where a typo must produce a
    working camera at the default size rather than a station with no video and
    a traceback in a log nobody is reading.
    """
    if isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            return max(8, int(value[0])), max(8, int(value[1]))
        except (TypeError, ValueError):
            return default
    text = str(value or "").lower().replace(" ", "")
    for separator in ("x", "*", ","):
        if separator in text:
            head, _, tail = text.partition(separator)
            try:
                return max(8, int(head)), max(8, int(tail))
            except ValueError:
                return default
    return default
