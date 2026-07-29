"""Fragmented MP4, built here, from H.264 access units.

The platform wants fMP4 over a WebSocket rather than the Annex B byte stream the
encoder produces, and the reasoning is the platform's to make: fMP4 keeps the
relay a byte pipe with no parsing and no re-muxing, so a second viewer costs a
socket rather than a codec, and a browser plays it through Media Source
Extensions with no player library.

So the station muxes. Three reasons it happens here rather than being asked of
`rpicam-vid --codec libav`:

**One container for three sources.** The hardware encoder, the software encoder
and the synthetic source all produce Annex B, and all three become byte-
identical fMP4 through this file. A bug in the container is one bug, in one
place, and the synthetic path proves the real one.

**No dependency on which muxer flags a given rpicam-apps build supports.** A
normal MP4 puts its index at the end and is unplayable as a stream; getting
`frag_keyframe+empty_moov+default_base_moof` wrong produces a file that looks
fine when copied off and shows nothing when streamed. That is not a thing to
discover on a remote box.

**It costs almost nothing.** Per frame this writes about 120 bytes of boxes and
copies the sample. At 30 fps that is 4 kB/s of overhead and no re-encoding.

Structure, which is the whole format:

    init segment   ftyp + moov   sent once per encoder session
    fragment       moof + mdat   one per frame, low latency by construction

One frame per fragment rather than one per keyframe: a fragment cannot be shown
until it is complete, so batching two seconds of frames into one would add two
seconds of latency to a live view for a few hundred bytes.
"""

from __future__ import annotations

import logging

from ..camera.h264 import (
    NAL_AUD,
    NAL_IDR,
    NAL_PPS,
    NAL_SPS,
    nal_type,
    split_annexb,
)

log = logging.getLogger("gsu.media")

#: 90 kHz, the usual video timescale. Whole numbers for 25, 30 and 50 fps.
TIMESCALE = 90000

#: Unity transformation matrix, as every MP4 carries it.
_MATRIX = b"".join(
    value.to_bytes(4, "big") for value in (
        0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000,
    )
)

#: trun sample flags. A keyframe depends on nothing and is a sync sample; every
#: other frame depends on what came before and is not one. A player that is
#: handed these wrong will either refuse to start or start on a frame it cannot
#: decode, and the second looks like a broken camera.
SYNC_SAMPLE = 0x02000000
NON_SYNC_SAMPLE = 0x01010000


def box(kind: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return (len(body) + 8).to_bytes(4, "big") + kind + body


def full_box(kind: bytes, version: int, flags: int, *payload: bytes) -> bytes:
    return box(kind, bytes((version,)) + flags.to_bytes(3, "big"), *payload)


def _u32(value: int) -> bytes:
    return int(value).to_bytes(4, "big")


def _u16(value: int) -> bytes:
    return int(value).to_bytes(2, "big")


def codec_string(sps: bytes) -> str:
    """The `avc1.PPCCLL` string Media Source Extensions needs up front.

    Profile, constraint flags and level, straight out of the first bytes of the
    sequence parameter set — no bit parsing, because they are the only three
    fields at fixed positions. Guessing this is how a browser fails silently:
    it accepts the source buffer and then decodes nothing, which from the far
    end is indistinguishable from a camera that is not sending.
    """
    if len(sps) < 4:
        return "avc1.42001f"     # baseline 3.1: a defensible last resort
    return f"avc1.{sps[1]:02x}{sps[2]:02x}{sps[3]:02x}"


def avcc(sps: bytes, pps: bytes) -> bytes:
    """The AVC decoder configuration record: the parameter sets, out of band."""
    return box(
        b"avcC",
        bytes((1, sps[1] if len(sps) > 1 else 0x42,
               sps[2] if len(sps) > 2 else 0,
               sps[3] if len(sps) > 3 else 0x1F)),
        # 0xFF: six reserved bits set, then lengthSizeMinusOne = 3, so every
        # sample in this file is a sequence of 4-byte-length-prefixed NALs.
        b"\xff",
        b"\xe1", _u16(len(sps)), sps,          # 0xE1: three reserved bits, 1 SPS
        b"\x01", _u16(len(pps)), pps,
    )


def visual_sample_entry(sps: bytes, pps: bytes, width: int, height: int) -> bytes:
    return box(
        b"avc1",
        b"\x00" * 6, _u16(1),                  # reserved, data_reference_index
        b"\x00" * 16,                          # pre_defined and reserved
        _u16(width), _u16(height),
        _u32(0x00480000), _u32(0x00480000),    # 72 dpi, as everything writes
        _u32(0), _u16(1),                      # reserved, frame_count
        b"\x00" * 32,                          # compressorname
        _u16(0x0018), b"\xff\xff",             # depth, pre_defined = -1
        avcc(sps, pps),
    )


def init_segment(sps: bytes, pps: bytes, width: int, height: int,
                 timescale: int = TIMESCALE) -> bytes:
    """`ftyp` + `moov`: everything a decoder needs before the first frame.

    The platform keeps this and gives it to every later viewer, because a viewer
    handed only the next fragment sees nothing at all — and that is
    indistinguishable from a dead camera.
    """
    ftyp = box(b"ftyp", b"isom", _u32(0x200), b"isom", b"iso2", b"avc1", b"mp41", b"iso6")

    mvhd = full_box(
        b"mvhd", 0, 0,
        _u32(0), _u32(0), _u32(1000), _u32(0),
        _u32(0x00010000), _u16(0x0100), b"\x00" * 10,
        _MATRIX, b"\x00" * 24, _u32(2),
    )
    tkhd = full_box(
        b"tkhd", 0, 0x000007,                  # enabled, in movie, in preview
        _u32(0), _u32(0), _u32(1), _u32(0), _u32(0),
        b"\x00" * 8, _u16(0), _u16(0), _u16(0), _u16(0),
        _MATRIX,
        _u32(width << 16), _u32(height << 16),
    )
    mdhd = full_box(b"mdhd", 0, 0, _u32(0), _u32(0), _u32(timescale), _u32(0),
                    _u16(0x55C4), _u16(0))     # 'und'
    hdlr = full_box(b"hdlr", 0, 0, _u32(0), b"vide", b"\x00" * 12, b"VideoHandler\x00")
    vmhd = full_box(b"vmhd", 0, 1, _u16(0), b"\x00" * 6)
    dinf = box(b"dinf", box(b"dref", _u32(0) + _u32(1) + full_box(b"url ", 0, 1)))
    stbl = box(
        b"stbl",
        full_box(b"stsd", 0, 0, _u32(1), visual_sample_entry(sps, pps, width, height)),
        full_box(b"stts", 0, 0, _u32(0)),
        full_box(b"stsc", 0, 0, _u32(0)),
        full_box(b"stsz", 0, 0, _u32(0), _u32(0)),
        full_box(b"stco", 0, 0, _u32(0)),
    )
    minf = box(b"minf", vmhd, dinf, stbl)
    mdia = box(b"mdia", mdhd, hdlr, minf)
    trak = box(b"trak", tkhd, mdia)
    # mvex is what makes this a *fragmented* file: it tells a reader that the
    # samples are in fragments that follow rather than in an index at the end,
    # which is the difference between a stream and a file that only plays once
    # it has been downloaded whole.
    mvex = box(b"mvex", full_box(b"trex", 0, 0, _u32(1), _u32(1), _u32(0), _u32(0), _u32(0)))
    return ftyp + box(b"moov", mvhd, trak, mvex)


def to_avcc(nals: list[bytes]) -> bytes:
    """Annex B NALs → the length-prefixed form a sample carries."""
    return b"".join(_u32(len(nal)) + nal for nal in nals)


class Fmp4Muxer:
    """H.264 access units in, an init segment and fragments out.

    Holds three pieces of state and nothing else: the parameter sets it has
    seen, the fragment sequence number, and the decode time. The last one is why
    a dropped fragment still has to be *counted* — see `advance`.
    """

    def __init__(self, width: int, height: int, fps: float = 30.0,
                 timescale: int = TIMESCALE) -> None:
        self.width = width
        self.height = height
        self.timescale = timescale
        self.sample_duration = max(1, round(timescale / max(1.0, float(fps))))
        self.sps: bytes | None = None
        self.pps: bytes | None = None
        self.sequence = 0
        self.decode_time = 0
        self.samples = 0

    # --- parameter sets --------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.sps is not None and self.pps is not None

    def init_segment(self) -> bytes | None:
        if not self.ready:
            return None
        return init_segment(self.sps, self.pps, self.width, self.height, self.timescale)

    def codec(self) -> str:
        return codec_string(self.sps or b"")

    # --- frames ----------------------------------------------------------

    def feed(self, unit) -> tuple[bytes | None, bool, bool]:
        """One access unit → `(fragment, keyframe, parameters_changed)`.

        `parameters_changed` is true when this frame carried a sequence
        parameter set different from the one in the current init segment — a
        restarted encoder, or one told to change resolution. The caller has to
        declare a new session before sending the fragment, because parameters
        that no longer match decode as corruption rather than as an error.

        Returns `None` for the fragment when there is nothing sendable yet:
        before the first parameter sets have arrived there is no decoder
        configuration, and a fragment sent then is a fragment nobody can play.
        """
        changed = False
        payload: list[bytes] = []
        keyframe = False
        for nal in split_annexb(unit.data):
            kind = nal_type(nal)
            if kind == NAL_SPS:
                if self.sps != nal:
                    changed = self.sps is not None
                    self.sps = nal
                continue
            if kind == NAL_PPS:
                if self.pps != nal:
                    changed = changed or self.pps is not None
                    self.pps = nal
                continue
            if kind == NAL_AUD:
                # Access unit delimiters carry no picture and MSE has no use for
                # them; the boundary they mark is already the fragment boundary.
                continue
            if kind == NAL_IDR:
                keyframe = True
            payload.append(nal)

        if not payload or not self.ready:
            return None, keyframe, changed
        return self.fragment(to_avcc(payload), keyframe), keyframe, changed

    def fragment(self, sample: bytes, keyframe: bool) -> bytes:
        """`moof` + `mdat` for one sample, at the current decode time."""
        self.sequence += 1
        moof = self._moof(len(sample), keyframe)
        self.advance()
        return moof + box(b"mdat", sample)

    def advance(self, frames: int = 1) -> None:
        """Move the clock on, whether or not the frame was sent.

        Called for dropped fragments too. Decode times must keep pace with the
        wall clock or a player treats a gap as a stall and then as a jump: the
        gap is real and the timeline has to say so, rather than pretending the
        frames that were dropped never existed.
        """
        self.decode_time += self.sample_duration * frames
        self.samples += frames

    def _moof(self, sample_size: int, keyframe: bool) -> bytes:
        mfhd = full_box(b"mfhd", 0, 0, _u32(self.sequence))
        # default-base-is-moof: offsets are measured from the start of this
        # moof, which is the only interpretation that survives a stream where
        # nobody knows the absolute file position.
        tfhd = full_box(b"tfhd", 0, 0x020000, _u32(1))
        tfdt = full_box(b"tfdt", 1, 0, self.decode_time.to_bytes(8, "big"))
        flags = 0x000001 | 0x000004 | 0x000100 | 0x000200
        # The data offset is measured from the moof, so it depends on the size
        # of the box it sits in. Built once with a placeholder to learn that
        # size, then again with the real value.
        def trun(offset: int) -> bytes:
            return full_box(
                b"trun", 0, flags,
                _u32(1), offset.to_bytes(4, "big", signed=True),
                _u32(SYNC_SAMPLE if keyframe else NON_SYNC_SAMPLE),
                _u32(self.sample_duration), _u32(sample_size),
            )

        provisional = box(b"moof", mfhd, box(b"traf", tfhd, tfdt, trun(0)))
        return box(b"moof", mfhd, box(b"traf", tfhd, tfdt, trun(len(provisional) + 8)))
