"""A synthetic source that produces **real H.264**, with no camera and no codec.

The platform has to be able to build and test the live-stream path before anyone
has a Pi with a camera on it, and a fake that emits made-up bytes would prove
nothing: the interesting failures on that side are all in decoding. So this
emits a conformant Annex B stream that any decoder will play.

It does that without a discrete cosine transform, without motion estimation and
without entropy-coded residuals, by using two macroblock types the standard
already provides for exactly this sort of corner:

    I_PCM     a macroblock carrying raw samples. No transform, no prediction,
              no CAVLC — 384 bytes and the decoder reproduces them exactly.
    P_Skip    a macroblock that copies the one before it, for a few bits.

An IDR of I_PCM macroblocks followed by P frames that skip everything except the
few macroblocks the clock is drawn in is a legal, decodable, moving H.264
stream, and it costs almost nothing to produce on a slow CPU.

**Two limits keep the keyframe decodable, and both are about size.** Every
picture is cut into slices of about 64 kB (`MBS_PER_SLICE`), and the picture
itself is shrunk until half a megabyte of raw samples covers it
(`MAX_KEYFRAME_BYTES`) — so a station configured for 1080p is sent a 768x432
test card, at the same shape, and told so. Neither is a nod to packet size:
uncompressed macroblocks make a 1080p keyframe 3.1 MB, nothing decodes that,
and the failure is silent and looks exactly like a broken camera. The comments
on those two names have the measurements.

**Its bitrate means nothing.** I_PCM is uncompressed, so a synthetic frame is
tens of times larger than what the Pi's hardware encoder produces for the same
picture. Use it to prove the pipe works; use HARDWARE.md §9 for what the link
has to carry. The numbers are not related and treating one as the other is the
mistake this paragraph exists to prevent.

Verified by decoding the output with ffmpeg 7.0.2 while it was written: the
stream decodes without error and the pixels come back as drawn.
"""

from __future__ import annotations

import logging
import threading
import time

from .. import clock
from .h264 import AccessUnit, StreamSettings
from .synthetic import SyntheticCamera

log = logging.getLogger("gsu.h264")

MB = 16
#: Bytes of a 4:2:0 I_PCM macroblock: 256 luma, 64 Cb, 64 Cr.
PCM_BYTES = 384

#: How much of one picture goes into a slice. I_PCM is 384 uncompressed bytes a
#: macroblock and nothing else in this stream has any size worth counting, so
#: this is a byte budget with a macroblock count derived from it.
#:
#: A picture used to be one slice, which at 1080p is an IDR of 8160 macroblocks
#: — 3.1 MB in a single NAL. **Browsers would not decode it.** Chromium on the
#: setup page dropped every keyframe and decoded only the P frames, so the
#: console showed the handful of macroblocks the clock and the sweep are drawn
#: in, moving on a black field, and nothing else: no colour bars, no mosaic, no
#: station name. Which looks like a camera fault and is a slice size. The same
#: picture at 320x240 — a 115 kB IDR — decoded perfectly, which is what made it
#: look like a resolution problem rather than the one number below.
#:
#: 64 kB is comfortably inside what a decoder will hold for one slice, and the
#: split costs one ~5-byte slice header per 64 kB.
#:
#: **This is the first of two limits, not the whole fix.** Measured in Chromium
#: at 1080p: with one slice per picture no part of a keyframe decodes at all;
#: with 64 kB slices the tail of one does and the rest of the picture stays
#: blank. What makes a keyframe whole is `MAX_KEYFRAME_BYTES` below, which
#: bounds the picture rather than the slice.
SLICE_BYTES = 64 * 1024
MBS_PER_SLICE = max(1, SLICE_BYTES // PCM_BYTES)

#: The largest picture this source will encode, as a keyframe byte budget.
#:
#: I_PCM is uncompressed — 384 bytes a macroblock, twelve bits a pixel — so a
#: keyframe here is the whole picture in raw samples. At 1080p that is 3.1 MB,
#: and **no browser decodes it.** Measured in Chromium against this station's
#: own setup page: 1080p comes back with the top two thirds of the picture
#: missing and one frame decoded every two seconds, 1280x720 (1.4 MB) fails
#: the same way, while 960x540 (780 kB) and 640x480 (450 kB) are perfect at
#: full rate. The cliff is about a megabyte a keyframe, and slicing does not
#: move it — the limit is on the picture, not the slice.
#:
#: Half a megabyte leaves room under that for decoders less willing than the
#: one it was measured on. **A real encoder has no such limit**, because a real
#: encoder compresses and this one cannot; nothing here is a statement about
#: what the camera or the link can do. HARDWARE.md §9 has those numbers.
MAX_KEYFRAME_BYTES = 512 * 1024
MAX_MACROBLOCKS = MAX_KEYFRAME_BYTES // PCM_BYTES


def encodable(width: int, height: int) -> tuple[int, int]:
    """The requested picture, shrunk until its keyframe will decode.

    The shape is kept, so a 16:9 site gets a 16:9 test card, and both sides come
    back whole macroblocks — this is a limit on how much raw sample data one
    picture can be, so it is answered in macroblocks rather than in pixels.
    """
    mb_width = max(1, (width + MB - 1) // MB)
    mb_height = max(1, (height + MB - 1) // MB)
    if mb_width * mb_height <= MAX_MACROBLOCKS:
        return width, height
    # Fit the height first and derive the width from it, rather than scaling
    # both and rounding each: rounding two sides independently is what turns
    # 16:9 into 1.81, and the test card is the one picture on the station whose
    # shape being wrong would be read as the console stretching it.
    scale = (MAX_MACROBLOCKS / (mb_width * mb_height)) ** 0.5
    rows = max(1, int(mb_height * scale))
    while True:
        columns = max(1, round(rows * width / height))
        if columns * rows <= MAX_MACROBLOCKS or rows == 1:
            return columns * MB, rows * MB
        rows -= 1


class BitWriter:
    """RBSP bits, and the Exp-Golomb the syntax is written in."""

    def __init__(self) -> None:
        self.out = bytearray()
        self._acc = 0
        self._bits = 0

    def u(self, value: int, length: int) -> None:
        self._acc = (self._acc << length) | (value & ((1 << length) - 1))
        self._bits += length
        while self._bits >= 8:
            self._bits -= 8
            self.out.append((self._acc >> self._bits) & 0xFF)
        self._acc &= (1 << self._bits) - 1

    def ue(self, value: int) -> None:
        """Unsigned Exp-Golomb: the whole syntax is written in it."""
        value += 1
        length = value.bit_length()
        self.u(0, length - 1)
        self.u(value, length)

    def se(self, value: int) -> None:
        self.ue(2 * value - 1 if value > 0 else -2 * value)

    def byte_align_zero(self) -> None:
        """`pcm_alignment_zero_bit`s. Zero, not the usual one-then-zeros."""
        if self._bits:
            self.u(0, 8 - self._bits)

    def bytes_(self, data: bytes) -> None:
        if self._bits:  # pragma: no cover - callers align first
            raise ValueError("byte payload written at a non-byte boundary")
        self.out += data

    def trailing(self) -> bytes:
        """`rbsp_trailing_bits`: a one, then zeros to the byte."""
        self.u(1, 1)
        if self._bits:
            self.u(0, 8 - self._bits)
        return bytes(self.out)


def rbsp_to_ebsp(data: bytes) -> bytes:
    """Insert emulation prevention bytes.

    Two zero bytes followed by 00, 01, 02 or 03 must have an 0x03 put between
    them, or the decoder reads a start code inside a macroblock and the picture
    ends where the data does not. I_PCM payloads are full of zero runs — a black
    region is thousands of them — so this is not a theoretical case here, it is
    the common one.
    """
    out = bytearray()
    start = 0
    index = 0
    length = len(data)
    while index < length:
        pair = data.find(b"\x00\x00", index)
        if pair < 0:
            break
        after = pair + 2
        if after < length and data[after] <= 0x03:
            out += data[start:after]
            out.append(0x03)
            start = after
            # The inserted byte breaks the zero run, so counting restarts here.
            index = after
        else:
            index = after + 1
    out += data[start:]
    return bytes(out)


def nal(unit_type: int, ref_idc: int, rbsp: bytes) -> bytes:
    """One NAL unit with a four-byte start code."""
    header = ((ref_idc & 3) << 5) | unit_type
    return b"\x00\x00\x00\x01" + bytes((header,)) + rbsp_to_ebsp(rbsp)


def rgb_to_yuv_limited(red: int, green: int, blue: int) -> tuple[int, int, int]:
    """BT.601, **studio range**, which is what H.264 means by default.

    The snapshot path uses full-range JFIF because that is what JPEG means. Using
    the same conversion for both would wash out every H.264 frame by a fixed
    amount — the sort of error that looks like a camera setting and is a line of
    arithmetic.
    """
    y = (299 * red + 587 * green + 114 * blue) // 1000
    cb = 128 + (-169 * red - 331 * green + 500 * blue) // 1000
    cr = 128 + (500 * red - 419 * green - 81 * blue) // 1000
    return (
        max(16, min(235, 16 + (219 * y) // 255)),
        max(16, min(240, 128 + (224 * (cb - 128)) // 255)),
        max(16, min(240, 128 + (224 * (cr - 128)) // 255)),
    )


def sps(width: int, height: int, sps_id: int = 0) -> bytes:
    """Sequence parameter set for a progressive 4:2:0 stream of this size."""
    mb_width = (width + MB - 1) // MB
    mb_height = (height + MB - 1) // MB
    writer = BitWriter()
    writer.u(66, 8)                     # profile_idc: baseline
    writer.u(0, 8)                      # constraint flags, none claimed
    writer.u(51, 8)                     # level_idc 5.1 — I_PCM is far over any
                                        # sane level's bitrate, and a strict
                                        # decoder would be right to say so
    writer.ue(sps_id)
    writer.ue(0)                        # log2_max_frame_num_minus4 → 4 bits
    writer.ue(2)                        # pic_order_cnt_type 2: decode order is
                                        # display order, so no POC in the slice
    writer.ue(1)                        # max_num_ref_frames
    writer.u(0, 1)                      # gaps_in_frame_num_value_allowed_flag
    writer.ue(mb_width - 1)
    writer.ue(mb_height - 1)
    writer.u(1, 1)                      # frame_mbs_only_flag
    writer.u(1, 1)                      # direct_8x8_inference_flag
    crop_bottom = (mb_height * MB - height) // 2   # chroma units, 4:2:0
    crop_right = (mb_width * MB - width) // 2
    if crop_bottom or crop_right:
        writer.u(1, 1)                  # frame_cropping_flag
        writer.ue(0)                    # left
        writer.ue(crop_right)
        writer.ue(0)                    # top
        writer.ue(crop_bottom)
    else:
        writer.u(0, 1)
    writer.u(0, 1)                      # vui_parameters_present_flag
    return nal(7, 3, writer.trailing())


def pps(pps_id: int = 0, sps_id: int = 0) -> bytes:
    writer = BitWriter()
    writer.ue(pps_id)
    writer.ue(sps_id)
    writer.u(0, 1)                      # entropy_coding_mode_flag: CAVLC
    writer.u(0, 1)                      # bottom_field_pic_order_in_frame_present
    writer.ue(0)                        # num_slice_groups_minus1
    writer.ue(0)                        # num_ref_idx_l0_default_active_minus1
    writer.ue(0)                        # num_ref_idx_l1_default_active_minus1
    writer.u(0, 1)                      # weighted_pred_flag
    writer.u(0, 2)                      # weighted_bipred_idc
    writer.se(0)                        # pic_init_qp_minus26
    writer.se(0)                        # pic_init_qs_minus26
    writer.se(0)                        # chroma_qp_index_offset
    writer.u(0, 1)                      # deblocking_filter_control_present_flag
    writer.u(0, 1)                      # constrained_intra_pred_flag
    writer.u(0, 1)                      # redundant_pic_cnt_present_flag
    return nal(8, 3, writer.trailing())


class Picture:
    """A test card in macroblocks, ready to be written as raw samples.

    Built from the same `Canvas` the snapshot test card uses, so the two sources
    show the same picture and a console can be checked against either.
    """

    def __init__(self, width: int, height: int, station_name: str = "") -> None:
        self.width = width
        self.height = height
        self.mb_width = (width + MB - 1) // MB
        self.mb_height = (height + MB - 1) // MB
        # The drawing canvas covers whole macroblocks; the SPS crops the frame
        # back to the declared size, which is how 1080 (67.5 macroblocks) works
        # at all.
        self._camera = SyntheticCamera(
            resolution=(self.mb_width * MB, self.mb_height * MB),
            station_name=station_name,
        )
        self.macroblocks: list[bytes] = [b""] * (self.mb_width * self.mb_height)
        self._previous_tiles: list | None = None

    def draw(self, at, frames: int) -> list[int]:
        """Render one frame. Returns the macroblocks that changed.

        Changes are found by comparing *tiles*, not encoded macroblocks, and
        only the macroblocks that changed are rebuilt. At 1080p that is the
        difference between eight thousand macroblock builds a frame and about
        thirty — which is what makes a synthetic 1080p source possible in Python
        at all.
        """
        self._camera.frames = frames
        canvas = self._camera.render(at)
        columns = canvas.columns
        tiles = canvas.tiles
        previous = self._previous_tiles
        changed: list[int] = []
        for mby in range(self.mb_height):
            row0 = (mby * 2) * columns
            row1 = row0 + columns
            for mbx in range(self.mb_width):
                left = mbx * 2
                quad = (tiles[row0 + left], tiles[row0 + left + 1],
                        tiles[row1 + left], tiles[row1 + left + 1])
                index = mby * self.mb_width + mbx
                if previous is not None and (
                    previous[row0 + left] == quad[0]
                    and previous[row0 + left + 1] == quad[1]
                    and previous[row1 + left] == quad[2]
                    and previous[row1 + left + 1] == quad[3]
                ):
                    continue
                self.macroblocks[index] = _pcm(quad)
                changed.append(index)
        self._previous_tiles = tiles
        return changed


#: Encoded macroblocks by the four tile colours that produce them. A test card
#: has a few dozen distinct macroblocks and thousands of copies of them.
_PCM_CACHE: dict[tuple, bytes] = {}


def _pcm(tiles) -> bytes:
    """One macroblock's raw samples: 256 luma, then 64 Cb, then 64 Cr.

    `tiles` is the four 8x8 tiles covering the macroblock, in raster order, and
    each is one flat colour — which is why this is four conversions rather than
    384, and why the result is worth caching by colour.
    """
    cached = _PCM_CACHE.get(tiles)
    if cached is not None:
        return cached
    yuv = [rgb_to_yuv_limited(*tile) for tile in tiles]
    out = bytearray(PCM_BYTES)
    for row in range(16):
        top = 0 if row < 8 else 2
        left, right = yuv[top][0], yuv[top + 1][0]
        base = row * 16
        out[base:base + 8] = bytes((left,)) * 8
        out[base + 8:base + 16] = bytes((right,)) * 8
    for plane, offset in ((1, 256), (2, 320)):
        for row in range(8):
            top = 0 if row < 4 else 2
            base = offset + row * 8
            out[base:base + 4] = bytes((yuv[top][plane],)) * 4
            out[base + 4:base + 8] = bytes((yuv[top + 1][plane],)) * 4
    block = bytes(out)
    if len(_PCM_CACHE) < 4096:
        _PCM_CACHE[tiles] = block
    return block


def slice_header(writer: BitWriter, first_mb: int, frame_num: int, *,
                 idr: bool) -> None:
    """The header both slice types share, written once.

    `first_mb_in_slice` is what makes several slices a picture rather than
    several pictures: each one states where in the macroblock raster it begins,
    and between them they have to tile the frame exactly.
    """
    writer.ue(first_mb)
    writer.ue(7 if idr else 5)          # slice_type: I or P, all slices
    writer.ue(0)                        # pic_parameter_set_id
    writer.u(frame_num & 0xF, 4)
    if idr:
        writer.ue(0)                    # idr_pic_id
        writer.u(0, 1)                  # no_output_of_prior_pics_flag
        writer.u(0, 1)                  # long_term_reference_flag
    else:
        writer.u(0, 1)                  # num_ref_idx_active_override_flag
        writer.u(0, 1)                  # ref_pic_list_modification_flag_l0
        writer.u(0, 1)                  # adaptive_ref_pic_marking_mode_flag
    writer.se(0)                        # slice_qp_delta


def idr_slice(picture: Picture, frame_num: int = 0) -> bytes:
    """An IDR made entirely of I_PCM macroblocks, in slices of `MBS_PER_SLICE`.

    One NAL per slice, concatenated: the access unit is the whole picture and
    every consumer downstream already handles a frame that is several NALs,
    because a real encoder's keyframe is one too.
    """
    out = b""
    total = len(picture.macroblocks)
    for start in range(0, total, MBS_PER_SLICE):
        writer = BitWriter()
        slice_header(writer, start, frame_num, idr=True)
        for block in picture.macroblocks[start:start + MBS_PER_SLICE]:
            writer.ue(25)               # mb_type I_PCM, in an I slice
            writer.byte_align_zero()
            writer.bytes_(block)
        out += nal(5, 3, writer.trailing())
    return out


def p_slice(picture: Picture, changed: list[int], frame_num: int) -> bytes:
    """A P frame that skips everything it can.

    Every macroblock that did not change is `P_Skip` — a few bits that mean
    "copy the previous frame" — and every one that did is I_PCM. On a test card
    where only a clock is moving that is a handful of macroblocks a frame, so
    this is normally one small slice.

    It is split on the same budget as the IDR all the same. A P frame is only
    small because little moved, which is a property of the picture rather than
    of the format — change the card enough and a frame of mostly-coded
    macroblocks is the same 3 MB slice that stopped keyframes decoding.
    """
    total = picture.mb_width * picture.mb_height
    out = b""
    start = 0                           # first macroblock of this slice
    written = 0                         # coded macroblocks dealt with so far
    while start < total:
        batch = changed[written:written + MBS_PER_SLICE]
        written += len(batch)
        # To the end of the picture, unless coded macroblocks are left over —
        # then this slice stops where the next one has to begin.
        end = total if written >= len(changed) else changed[written]
        writer = BitWriter()
        slice_header(writer, start, frame_num, idr=False)
        previous = start - 1
        for index in batch:
            writer.ue(index - previous - 1)  # mb_skip_run to this macroblock
            writer.ue(30)                    # mb_type I_PCM in a P slice (25+5)
            writer.byte_align_zero()
            writer.bytes_(picture.macroblocks[index])
            previous = index
        trailing = end - previous - 1
        if trailing > 0:
            writer.ue(trailing)
        out += nal(1, 2, writer.trailing())
        start = end
    return out


class SyntheticH264Source:
    """The same interface as `RpicamVidSource`, with no camera behind it.

    Deliberately the same shape: the station code above it must not be able to
    tell which one it is talking to, because the thing being tested on the
    platform side is the pipe, and a test that exercises a different path from
    the real one is worth very little.
    """

    def __init__(self, settings: StreamSettings | None = None,
                 station_name: str = "") -> None:
        self.settings = settings or StreamSettings()
        self.tool = "synthetic"
        self.reason = ""
        #: What is actually encoded, which is not always what was asked for.
        #: Read by `gsu/stream.py` so that the log, the stored event and the
        #: telemetry all name the picture being sent rather than the policy it
        #: was derived from.
        self.width, self.height = encodable(
            self.settings.width, self.settings.height)
        self.clamped = (
            (self.width, self.height)
            != (self.settings.width, self.settings.height)
        )
        if self.clamped:
            log.info(
                "The synthetic camera is encoding %dx%d, not the %dx%d this "
                "station is configured for: its macroblocks are uncompressed, "
                "so a keyframe at the configured size is %.1f MB and decoders "
                "drop it. This is the fake encoder's limit and says nothing "
                "about the camera or the link.",
                self.width, self.height,
                self.settings.width, self.settings.height,
                (((self.settings.width + MB - 1) // MB)
                 * ((self.settings.height + MB - 1) // MB)
                 * PCM_BYTES / (1024 * 1024)),
            )
        self._picture = Picture(self.width, self.height, station_name)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_unit = None
        self.started_at: float | None = None
        self.frames = 0
        self.bytes_out = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, on_unit) -> bool:
        if self.running:
            return True
        self._stop.clear()
        self._on_unit = on_unit
        self.started_at = time.monotonic()
        self.frames = 0
        self.bytes_out = 0
        self._thread = threading.Thread(
            target=self._run, name="gsu-h264-synthetic", daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)

    def _run(self) -> None:
        interval = 1.0 / max(1, self.settings.fps)
        next_frame = time.monotonic()
        while not self._stop.is_set():
            unit = self.frame()
            if self._on_unit is not None:
                self._on_unit(unit)
            next_frame += interval
            self._stop.wait(max(0.0, next_frame - time.monotonic()))

    def frame(self) -> AccessUnit:
        """One access unit. Public so a test can pull frames without a thread."""
        at = clock.now()
        # Counted from the last IDR, not from the start of the stream. That is
        # what `frame_num` means: an IDR resets it to zero and every reference
        # frame after it adds one, and with
        # `gaps_in_frame_num_value_allowed_flag` clear in the SPS a decoder is
        # entitled to reject a picture whose frame_num skipped. Using the
        # absolute frame counter here made the first group of pictures correct
        # by accident and every one after it wrong — the frame after the second
        # IDR jumped from 0 to 13 — so a browser decoded one keyframe per two
        # seconds and dropped the fifty-nine P frames between them.
        since_idr = self.frames % max(1, self.settings.intra_period)
        keyframe = since_idr == 0
        changed = self._picture.draw(at, self.frames)
        if keyframe:
            data = sps(self.width, self.height) + pps() + idr_slice(self._picture)
        else:
            data = p_slice(self._picture, changed, since_idr)
        self.frames += 1
        self.bytes_out += len(data)
        return AccessUnit(
            data=data, captured_at=at, keyframe=keyframe,
            parameter_sets=sps(self.width, self.height) + pps(),
        )

    def stats(self) -> dict:
        elapsed = max(0.001, time.monotonic() - (self.started_at or time.monotonic()))
        return {
            "running": self.running,
            "tool": "synthetic",
            "frames": self.frames,
            "bytes": self.bytes_out,
            "fps_measured": round(self.frames / elapsed, 1),
            "bitrate_bps": round(self.bytes_out * 8 / elapsed),
            "width": self.width,
            "height": self.height,
            "reason": (
                f"synthetic H.264 — uncompressed macroblocks, so its bitrate "
                f"is not a camera's. Encoding {self.width}x{self.height} "
                f"rather than the configured {self.settings.width}x"
                f"{self.settings.height}: a keyframe of raw samples at that "
                f"size is too large to decode."
                if self.clamped else
                "synthetic H.264 — uncompressed macroblocks, so its bitrate "
                "is not a camera's"
            ),
        }
