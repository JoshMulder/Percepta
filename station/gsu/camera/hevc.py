"""HEVC (H.265): the same Annex B pipe, told what a NAL header looks like.

A real camera arrived and it speaks HEVC Main at 4K. The station's job does not
change — it still never encodes and never decodes for the live stream, it
remuxes what the camera already produced — but two things it *reads* are
different enough to be wrong silently, and both were:

**The NAL header is two bytes, not one.** H.264 puts the type in the low five
bits of one byte; HEVC uses `forbidden_zero(1) | type(6) | layer_id(6) |
temporal_id_plus1(3)` across two. Read with H.264's rule, an HEVC IDR (type 19
or 20) reads as type 6 or 8 — SEI or PPS — so an entire stream looks like
parameter sets with no pictures in it. That is not a hypothetical: `-c copy`
poured this camera's H.265 into a container labelled H.264, ffmpeg exited zero,
8 Mbit/s flowed, and the reader found one access unit in 109 seconds with no
error anywhere.

**There is a third parameter set.** HEVC has a video parameter set above the
sequence and picture ones, and it has to reach a decoder like the other two. A
viewer that attaches mid-stream and is handed only SPS and PPS gets a black
element and no error.

This file is the HEVC half of two things and nothing else:

    the grammar   `HEVC`, a `NalRules` for `AnnexBReader` — one reader, two rule
                  sets, because the framing (start codes, trailing zeros, the
                  buffering) is identical and is the part with the subtle bugs
                  in it.
    the SPS       enough of the sequence parameter set to fill in an `hvcC` box
                  and an RFC 6381 codec string. Both fail *silently* when wrong:
                  Media Source Extensions accepts the source buffer, decodes
                  nothing, and shows a black video element with no error on any
                  console — which is indistinguishable from a dead camera.

Boxes are built in `gsu/media/fmp4.py`; this file only reads bitstreams. The
split is deliberate — one file knows MP4, one file knows HEVC.

**There is no HEVC encoder here and there should not be.** The station remuxes
a camera that encodes for itself. Asking a Pi to encode HEVC is asking it to do
the one thing this whole design exists to avoid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .h264 import NalRules

log = logging.getLogger("gsu.hevc")

# --- NAL unit types -------------------------------------------------------
#
# Everything below 32 is a slice (a "VCL" NAL); everything from 32 up is not.
# That single rule is cleaner than H.264's scattered numbering and is what the
# rules below are built on.

NAL_TRAIL_N = 0
NAL_TRAIL_R = 1
#: The intra random access pictures, 16 to 21 inclusive: BLA_W_LP, BLA_W_RADL,
#: BLA_N_LP, IDR_W_RADL, IDR_N_LP and CRA_NUT. All six are places a decoder can
#: start, so all six are sync samples. Missing CRA is an easy and expensive
#: mistake — x265 emits one rather than an IDR at most of its keyframes, so a
#: stream whose only sync sample is the very first frame is what you get, and
#: after one dropped fragment the picture never comes back.
NAL_IRAP_FIRST = 16
NAL_IRAP_LAST = 21
NAL_VPS = 32
NAL_SPS = 33
NAL_PPS = 34
NAL_AUD = 35
NAL_EOS = 36
NAL_EOB = 37
NAL_FD = 38
NAL_PREFIX_SEI = 39
NAL_SUFFIX_SEI = 40

#: The first NAL unit type that is not a slice.
FIRST_NON_VCL = 32


def nal_type(nal: bytes) -> int:
    """The type of one NAL unit, given without its start code."""
    return (nal[0] >> 1) & 0x3F if nal else 0


#: The HEVC grammar, for `AnnexBReader` and `Fmp4Muxer`.
#:
#: `starters` is the conservative half of the spec's list. ISO/IEC 23008-2
#: §7.4.2.4.4 also lets several reserved and unspecified types begin an access
#: unit, and they are left out on purpose: a NAL wrongly treated as a picture
#: start splits one frame into two, while a NAL wrongly treated as ordinary is
#: merely carried along inside the frame it arrived in. Of those two ways to be
#: wrong about a type nobody has ever sent, the second is the one that still
#: plays.
#:
#: The one that is not optional is the SEI split: **prefix SEI (39) begins an
#: access unit and suffix SEI (40) does not.** Treating both as starters cuts a
#: frame in half every time a camera appends one.
HEVC = NalRules(
    name="hevc",
    type_shift=1,
    type_mask=0x3F,
    header_bytes=2,
    parameter_sets=(NAL_VPS, NAL_SPS, NAL_PPS),
    starters=frozenset({NAL_VPS, NAL_SPS, NAL_PPS, NAL_AUD, NAL_PREFIX_SEI}),
    slices=frozenset(range(FIRST_NON_VCL)),
    keyframes=frozenset(range(NAL_IRAP_FIRST, NAL_IRAP_LAST + 1)),
    aud=NAL_AUD,
    vps=NAL_VPS,
    sps=NAL_SPS,
    pps=NAL_PPS,
)


# --- reading a bitstream --------------------------------------------------


class BitstreamError(ValueError):
    """The bytes ran out, or said something impossible. Never raised upward."""


def unescape(data: bytes) -> bytes:
    """Remove emulation prevention bytes: `00 00 03` → `00 00`.

    The exact inverse of `h264_synthetic.rbsp_to_ebsp`, and not optional here:
    a real SPS is full of them. The 4K camera's is
    `… 01 60 00 00 03 00 90 00 00 03 …`, and parsing that without stripping the
    0x03s reads the profile compatibility flags four bytes out of alignment —
    which produces a codec string that is wrong in a way no decoder reports.
    """
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class Bits:
    """Big-endian bit reader over an RBSP, with the Exp-Golomb it is written in.

    Held as one integer rather than a byte cursor: an SPS is a few dozen bytes,
    it is read once per encoder session, and shifting a big integer keeps the
    bit twiddling out of the way of the parsing it exists to serve.
    """

    def __init__(self, data: bytes) -> None:
        self._whole = int.from_bytes(data, "big") if data else 0
        self._end = len(data) * 8
        self._bit = 0

    def u(self, count: int) -> int:
        """`count` bits, unsigned."""
        if count <= 0:
            return 0
        if self._bit + count > self._end:
            raise BitstreamError(
                f"the parameter set ended {self._bit + count - self._end} bits early"
            )
        shift = self._end - self._bit - count
        self._bit += count
        return (self._whole >> shift) & ((1 << count) - 1)

    def ue(self) -> int:
        """Unsigned Exp-Golomb, which most of the syntax is written in."""
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                # Not a length: a run this long means the read is misaligned,
                # and continuing would produce a plausible-looking number.
                raise BitstreamError("a run of zero bits too long to be a value")
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.u(zeros)


# --- profile, tier and level ----------------------------------------------


@dataclass(frozen=True)
class ProfileTierLevel:
    """The twelve bytes that decide whether a browser will play this at all.

    They appear twice, byte for byte: in the `hvcC` configuration box and in the
    RFC 6381 codec string handed to Media Source Extensions. Both are read
    before a single frame is decoded, and both fail the same way when wrong —
    no exception, no console error, a black video element.
    """

    profile_space: int
    tier_flag: int
    profile_idc: int
    #: 32 flags, as stored: flag[0] is the most significant bit.
    compatibility_flags: int
    #: The six bytes of general_constraint_indicator_flags.
    constraint_flags: bytes
    level_idc: int

    def codec_string(self, sample_entry: str = "hvc1") -> str:
        """The RFC 6381 name, e.g. `hvc1.1.6.L120.90`.

        ISO/IEC 14496-15 Annex E.3, which is five rules and no room to guess:

          1. the profile space as nothing, or `A`/`B`/`C`;
          2. `general_profile_idc`, decimal;
          3. the compatibility flags **in reverse bit order**, hex, no leading
             zeros — Main's `0x60000000` therefore prints as `6`, not `60000000`;
          4. `L` for the main tier or `H` for high, then `general_level_idc`
             decimal, which is the level times thirty (`L120` is level 4.0);
          5. the six constraint bytes in hex, dot-separated, with trailing zero
             bytes left off — progressive plus frame-only is the single byte
             `90`.
        """
        space = "" if not self.profile_space else chr(ord("A") + self.profile_space - 1)
        reversed_flags = int(
            f"{self.compatibility_flags:032b}"[::-1], 2
        )
        parts = [
            sample_entry,
            f"{space}{self.profile_idc}",
            f"{reversed_flags:x}",
            f"{'H' if self.tier_flag else 'L'}{self.level_idc}",
        ]
        constraints = self.constraint_flags.rstrip(b"\x00")
        parts += [f"{byte:02x}" for byte in constraints]
        return ".".join(parts)


def read_profile_tier_level(bits: Bits, max_sub_layers_minus1: int) -> ProfileTierLevel:
    """`profile_tier_level()` with `profilePresentFlag` set, per §7.3.3.

    The sub-layer half is skipped rather than kept — nothing here needs it — but
    it has to be *read*, because everything the SPS says after it (the picture
    size, the chroma format, the bit depth) is at whatever bit offset this
    leaves behind.
    """
    profile_space = bits.u(2)
    tier_flag = bits.u(1)
    profile_idc = bits.u(5)
    compatibility_flags = bits.u(32)
    constraint_flags = bits.u(48).to_bytes(6, "big")
    level_idc = bits.u(8)

    present = [(bits.u(1), bits.u(1)) for _ in range(max_sub_layers_minus1)]
    if max_sub_layers_minus1 > 0:
        # reserved_zero_2bits, padding the flags out to eight sub-layers.
        for _ in range(max_sub_layers_minus1, 8):
            bits.u(2)
    for profile_present, level_present in present:
        if profile_present:
            bits.u(88)
        if level_present:
            bits.u(8)

    return ProfileTierLevel(
        profile_space=profile_space,
        tier_flag=tier_flag,
        profile_idc=profile_idc,
        compatibility_flags=compatibility_flags,
        constraint_flags=constraint_flags,
        level_idc=level_idc,
    )


# --- the sequence parameter set -------------------------------------------

#: Table 6-1: how far apart chroma samples are, which is what the conformance
#: window's offsets are counted in. Monochrome and 4:4:4 are 1:1.
_SUB_SAMPLING = {0: (1, 1), 1: (2, 2), 2: (2, 1), 3: (1, 1)}


@dataclass(frozen=True)
class SequenceParameterSet:
    """As much of the SPS as an `hvcC` box and a codec string need.

    Parsing stops at `bit_depth_chroma_minus8`, which is the last field either
    of them wants. Everything after it — the picture order count, the reference
    picture sets, the VUI — is the decoder's business and is not read, because
    a parser that reads fields nobody uses is a parser with more ways to be
    wrong about a stream it would otherwise have handled.
    """

    profile_tier_level: ProfileTierLevel
    max_sub_layers: int
    temporal_id_nesting: int
    chroma_format_idc: int
    #: The coded size, before the conformance window is applied.
    coded_width: int
    coded_height: int
    #: The size a viewer should see, after the conformance window is taken off.
    #: Unlike H.264 — where 1080 is not a whole number of 16-line macroblocks,
    #: so every 1080p stream is coded as 1088 and cropped — HEVC's smallest
    #: coding block is 8x8 and 1080p needs no window at all. It is the odd sizes
    #: that do, and a camera set to something like 1918x1078 codes 1920x1080 and
    #: crops. Reading the coded size instead puts the wrong shape in the sample
    #: entry, which is a stretched picture rather than a missing one — the one
    #: failure in this file somebody would actually see.
    width: int
    height: int
    bit_depth_luma: int
    bit_depth_chroma: int

    def codec_string(self, sample_entry: str = "hvc1") -> str:
        return self.profile_tier_level.codec_string(sample_entry)


def parse_sps(nal: bytes) -> SequenceParameterSet | None:
    """One SPS NAL (header included, start code not) → its fields, or None.

    None means "this did not parse", and every caller treats that as a reason to
    stop rather than a reason to guess. A guessed `hvcC` is the failure this
    whole file exists to prevent: it produces a video element that stays black
    and reports nothing, on every console, forever.
    """
    if len(nal) < 3 or nal_type(nal) != NAL_SPS:
        return None
    try:
        return _parse_sps(Bits(unescape(nal[2:])))
    except BitstreamError as exc:
        log.error("An HEVC sequence parameter set would not parse: %s", exc)
        return None


def _parse_sps(bits: Bits) -> SequenceParameterSet:
    bits.u(4)                                       # sps_video_parameter_set_id
    max_sub_layers_minus1 = bits.u(3)
    temporal_id_nesting = bits.u(1)
    ptl = read_profile_tier_level(bits, max_sub_layers_minus1)
    bits.ue()                                       # sps_seq_parameter_set_id
    chroma_format_idc = bits.ue()
    separate_colour_plane = bits.u(1) if chroma_format_idc == 3 else 0
    coded_width = bits.ue()
    coded_height = bits.ue()

    left = right = top = bottom = 0
    if bits.u(1):                                   # conformance_window_flag
        left, right, top, bottom = bits.ue(), bits.ue(), bits.ue(), bits.ue()
    bit_depth_luma = bits.ue() + 8
    bit_depth_chroma = bits.ue() + 8

    # With separate colour planes the three are coded as monochrome, so the
    # window is counted in luma samples whatever the chroma format says.
    sub_width, sub_height = _SUB_SAMPLING.get(
        0 if separate_colour_plane else chroma_format_idc, (1, 1)
    )
    return SequenceParameterSet(
        profile_tier_level=ptl,
        max_sub_layers=max_sub_layers_minus1 + 1,
        temporal_id_nesting=temporal_id_nesting,
        chroma_format_idc=chroma_format_idc,
        coded_width=coded_width,
        coded_height=coded_height,
        width=max(0, coded_width - sub_width * (left + right)),
        height=max(0, coded_height - sub_height * (top + bottom)),
        bit_depth_luma=bit_depth_luma,
        bit_depth_chroma=bit_depth_chroma,
    )


def codec_string(sps_nal: bytes, sample_entry: str = "hvc1") -> str:
    """The RFC 6381 string for this stream, or `""` if the SPS will not parse.

    Empty rather than a plausible default, deliberately. The H.264 side of this
    falls back to baseline 3.1 and gets away with it because almost everything
    decodes almost all H.264; HEVC playback is hardware-dependent and a wrong
    string is refused or, worse, accepted and never decoded. An empty string
    stops the stream with a reason somebody can read.
    """
    parsed = parse_sps(sps_nal)
    return parsed.codec_string(sample_entry) if parsed else ""
