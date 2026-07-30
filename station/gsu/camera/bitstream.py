"""Reading the bits a parameter set is written in. One copy, both codecs.

This was inside `hevc.py`, which was the right place while HEVC was the only
codec whose sequence parameter set the station read. It is not any more: the
H.264 sample entry used to take its dimensions from the station's *configured*
size, which on a 4K camera under a 1080p site policy writes 1920x1080 into the
container for a 3840x2160 stream — the previous agent fixed exactly that for
HEVC and flagged that H.264 still had it. Fixing it means parsing an H.264 SPS,
which means this reader has to be reachable from `h264.py` — and `hevc.py`
already imports `NalRules` from there, so leaving it where it was would have
made a cycle.

`hevc.py` re-exports all three names, so nothing that imported them from there
has to change.

Exp-Golomb and emulation prevention are the two things every codec in this
family writes its parameter sets with, and they are the two things that fail
*silently* when wrong: a misaligned read produces a plausible number, not an
error, and a plausible number here is a codec string a browser accepts and
cannot decode.
"""

from __future__ import annotations


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

    def se(self) -> int:
        """Signed Exp-Golomb — `ue` folded onto the integers around zero.

        Needed only by H.264: its scaling lists and picture order count syntax
        are written in it, and both sit *before* the picture dimensions in the
        SPS. They are skipped rather than used, but they have to be skipped
        exactly, because a single bit of drift here moves the width and height
        to somewhere else entirely.
        """
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)
