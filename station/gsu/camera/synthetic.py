"""A camera that is obviously not a camera.

This is the only way any of the video path gets tested before hardware exists,
and it is what an operator sees if the real camera does not come up first time.
Both of those argue for the same thing: **it must be impossible to mistake for a
photograph.** So it is a test card — colour bars, a mosaic background, and the
capture time written across the middle in letters four blocks high — and it says
SYNTHETIC on its face rather than only in a device inventory that nobody has
open.

The clock drawn on the frame is the same instant that goes into `captured_at`.
That is deliberate and worth keeping: it makes the contract's one subtle rule
checkable by eye. If a console ever renders a frame whose drawn clock disagrees
with the age it displays, the bug is on the platform side, and there is no
instrumentation to set up to see it.

It draws at whole-block resolution (`jpeg.py` explains why), which costs a
fraction of a millisecond a frame and produces a frame several times smaller
than a real camera's. HARDWARE.md §8 has both numbers; the one to plan bandwidth
with is the real camera's.
"""

from __future__ import annotations

import logging

from .. import clock
from ..sensors import Device
from . import Frame, complete_jpeg, font, jpeg, parse_resolution

log = logging.getLogger("gsu.camera")

TILE = 8

#: Colour bars, left to right. The order is the familiar one so that a wrong
#: colour channel is obvious at a glance rather than merely odd.
BARS = (
    (200, 200, 200), (200, 200, 0), (0, 200, 200), (0, 200, 0),
    (200, 0, 200), (200, 0, 0), (0, 0, 200),
)

AMBER = (255, 176, 0)
WHITE = (235, 235, 235)
GREY = (110, 118, 130)
SWEEP = (0, 220, 120)


class Canvas:
    """A grid of flat 8x8 tiles. Every drawing operation is tile-aligned,
    because the encoder cannot represent anything finer."""

    def __init__(self, columns: int, rows: int, background: tuple[int, int, int]) -> None:
        self.columns = columns
        self.rows = rows
        self.tiles = [background] * (columns * rows)

    def set(self, x: int, y: int, colour: tuple[int, int, int]) -> None:
        if 0 <= x < self.columns and 0 <= y < self.rows:
            self.tiles[y * self.columns + x] = colour

    def fill(self, x: int, y: int, width: int, height: int,
             colour: tuple[int, int, int]) -> None:
        for row in range(y, min(y + height, self.rows)):
            if row < 0:
                continue
            start = max(0, x)
            end = min(x + width, self.columns)
            if end > start:
                self.tiles[row * self.columns + start:row * self.columns + end] = (
                    [colour] * (end - start)
                )

    def text(self, x: int, y: int, text: str, colour: tuple[int, int, int]) -> None:
        for index, char in enumerate(text):
            origin = x + index * font.ADVANCE
            for row, pixels in enumerate(font.glyph(char)):
                for column, lit in enumerate(pixels):
                    if lit:
                        self.set(origin + column, y + row, colour)

    def centred(self, y: int, text: str, colour: tuple[int, int, int]) -> None:
        text = font.fits(text, self.columns - 2)
        self.text(max(1, (self.columns - font.text_width(text)) // 2), y, text, colour)


class SyntheticCamera:
    """A test card at the configured resolution and frame size.

    Reports `simulated=True` in the device inventory, like every other simulated
    adapter here, so the platform is never told a simulation is a sensor and the
    console's DEMO badge lights for the right reason.
    """

    def __init__(
        self,
        resolution: object = "640x480",
        quality: int = 75,
        station_name: str = "",
    ) -> None:
        self.width, self.height = parse_resolution(resolution)
        self.quality = max(1, min(100, int(quality or 75)))
        self.station_name = (station_name or "").upper()
        self.columns = (self.width + TILE - 1) // TILE
        self.rows = (self.height + TILE - 1) // TILE
        self.frames = 0
        self.last_bytes = 0
        self._reason = ""

    # --- the interface --------------------------------------------------

    @property
    def status(self) -> str:
        return "streaming" if self.frames else "silent"

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    def capture(self) -> Frame | None:
        # Stamped before the frame is drawn, and drawn onto the frame from the
        # same value: `captured_at` is the moment of capture, and here that is
        # also what the picture says.
        at = clock.now()
        data = jpeg.encode_tiles(
            self.render(at).tiles, self.columns, self.rows,
            self.width, self.height, self.quality,
        )
        if not complete_jpeg(data):  # pragma: no cover - defensive
            # Unreachable unless the encoder is broken, and that is exactly when
            # it must not publish: the completeness rule is enforced on every
            # path, not only on the ones that seem likely to fail.
            self._reason = "the synthetic encoder produced an incomplete frame"
            log.error("%s", self._reason)
            return None
        self.frames += 1
        self.last_bytes = len(data)
        self._reason = ""
        return Frame(jpeg=data, width=self.width, height=self.height, captured_at=at)

    def raw_sample(self) -> list[str]:
        if not self.frames:
            return []
        return [
            f"frame {self.frames}: {self.last_bytes / 1024:.1f} kB, "
            f"{self.width}x{self.height} test card"
        ]

    def describe(self) -> Device:
        return Device(
            id="camera",
            kind="camera",
            present=True,
            detail=(
                f"synthetic test card, {self.width}x{self.height}, quality "
                f"{self.quality}, {self.last_bytes / 1024:.1f} kB/frame"
                if self.frames else
                f"synthetic test card, {self.width}x{self.height}, "
                f"quality {self.quality}"
            ),
            simulated=True,
        )

    def close(self) -> None:
        return None

    # --- the picture ----------------------------------------------------

    def render(self, at) -> Canvas:
        canvas = Canvas(self.columns, self.rows, (12, 14, 22))
        self._background(canvas)
        # A quarter of the height, not a twelfth. The bars are the part of this
        # card that proves colour survives the encoder, and at a twelfth they
        # were a stripe you had to look for.
        bars = max(3, self.rows // 4)
        width = max(1, self.columns // len(BARS))
        for index, colour in enumerate(BARS):
            last = index == len(BARS) - 1
            canvas.fill(index * width, 0,
                        self.columns - index * width if last else width, bars, colour)

        # Short lines on purpose: one font pixel is one 8x8 block, so a 640-wide
        # frame is thirteen characters across. The trailing Z is how the picture
        # says UTC in the space available.
        lines = [
            ("SYNTHETIC", AMBER),
            (at.strftime("%H:%M:%SZ"), WHITE),
            (at.strftime("%Y-%m-%d"), GREY),
        ]
        if self.station_name:
            # A truncated name reads as a fault in the picture, so a name too
            # long for the frame is shortened to its first word rather than cut
            # mid-letter. "Kaikoura Ridge" is one character over at 640 wide.
            name = self.station_name
            if font.text_width(name) > canvas.columns - 2:
                name = name.split(" ")[0]
            lines.append((name, GREY))
        lines.append((f"FRAME {self.frames:06d}", GREY))

        # Lay the lines out from just under the bars, dropping any that do not
        # fit rather than overflowing: this has to work at 320x240 as well.
        y = bars + 2
        for text, colour in lines:
            if y + font.HEIGHT > self.rows - 3:
                break
            canvas.centred(y, text, colour)
            y += font.HEIGHT + 1

        self._sweep(canvas)
        self._border(canvas)
        return canvas

    def _background(self, canvas: Canvas) -> None:
        """A coarse mosaic, so a frozen frame and a black one are different
        things to look at.

        This was a vertical gradient from RGB 10 to 36 — near-black top to
        bottom. With the colour bars only a twelfth of the height, some ninety
        percent of the test card was a dark smudge, and the one thing a test
        card exists to prove is that the picture path works at all. A dark
        frame and a dead camera looked the same, which is exactly backwards.

        Muted rather than saturated: the bars above are the colour reference
        and this must not compete with them, but it does have to be plainly
        *lit* so a glance separates "the camera is fine" from "the sensor is
        giving me nothing".
        """
        palette = (
            (54, 60, 78), (68, 74, 96), (44, 52, 70), (78, 84, 104),
            (60, 68, 88), (50, 58, 76),
        )
        block = max(2, canvas.rows // 10)
        for row in range(0, canvas.rows, block):
            for column in range(0, canvas.columns, block):
                shade = palette[((row // block) + (column // block)) % len(palette)]
                canvas.fill(column, row,
                            min(block, canvas.columns - column),
                            min(block, canvas.rows - row), shade)

    def _sweep(self, canvas: Canvas) -> None:
        """A block that moves one column per frame.

        The point of it is a failure that is otherwise invisible: a stream that
        has stopped updating looks exactly like a still scene, and a site with
        nothing happening in it is the normal case. If the block is not moving,
        the frames are not arriving.
        """
        row = canvas.rows - 3
        canvas.fill(0, row, canvas.columns, 1, (30, 34, 44))
        span = max(2, canvas.columns // 16)
        travel = max(1, canvas.columns - span)
        canvas.fill(self.frames % travel, row, span, 1, SWEEP)

    def _border(self, canvas: Canvas) -> None:
        """Edges and a floor. Not decoration: they mark where the frame ends, so
        a decoder that has cropped or padded the picture shows it."""
        canvas.fill(0, canvas.rows - 1, canvas.columns, 1, (60, 66, 78))
        for row in range(canvas.rows):
            canvas.set(0, row, (60, 66, 78))
            canvas.set(canvas.columns - 1, row, (60, 66, 78))
