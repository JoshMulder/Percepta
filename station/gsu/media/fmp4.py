"""Fragmented MP4, built here, from H.264 or HEVC access units.

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

**Two codecs, one container.** A network camera that encodes HEVC for itself is
remuxed the same way, into the same boxes, with two things swapped: the sample
entry is `hvc1` with an `hvcC` under it instead of `avc1` with an `avcC`, and
the codec string is `hvc1.…` instead of `avc1.…`. Both are read before a single
frame is decoded and both fail the same way when wrong — Media Source
Extensions accepts the source buffer, decodes nothing, and shows black with no
error. Which is the same failure signature as a dead camera, an unopened
uplink, and a wrong NAL header, so it is worth being exact about.
"""

from __future__ import annotations

import logging

from ..camera.h264 import H264, H264Sps, NalRules, split_annexb
from ..camera.h264 import parse_sps as parse_h264_sps
from ..camera.hevc import HEVC, SequenceParameterSet, parse_sps

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


def hvcc(vps: bytes, sps: bytes, pps: bytes, parsed: SequenceParameterSet) -> bytes:
    """The HEVC decoder configuration record — ISO/IEC 14496-15 §8.3.3.1.

    Genuinely a different shape from `avcC`, not a renamed one. Three things
    about it are worth stating because getting any of them wrong produces a
    video element that stays black and reports nothing at all:

    **The profile, tier and level are copied out, not just carried.** Twelve
    bytes of them sit in front, and they have to agree with what is inside the
    SPS *and* with the codec string sent alongside. They are read straight from
    the parsed SPS here so there is one source for all three.

    **The parameter sets are arrays with counts**, not the fixed one-SPS-one-PPS
    of `avcC` — one array per NAL type, each with its own count. That is what
    makes room for the VPS, which H.264 has no equivalent of.

    **`array_completeness` is set.** It means "every parameter set of this type
    is in here", which is a promise the muxer keeps by stripping them out of the
    samples — and it is what `hvc1` means, as opposed to `hev1`.

    The reserved bits are ones, not zeros. A decoder that validates them refuses
    a record full of zeros, and one that does not is reading a field that means
    something else.
    """
    ptl = parsed.profile_tier_level
    arrays = b""
    count = 0
    for kind, nal in ((HEVC.vps, vps), (HEVC.sps, sps), (HEVC.pps, pps)):
        if not nal:
            continue
        count += 1
        arrays += bytes((0x80 | kind,)) + _u16(1) + _u16(len(nal)) + nal
    return box(
        b"hvcC",
        bytes((1,)),                           # configurationVersion
        bytes(((ptl.profile_space << 6) | (ptl.tier_flag << 5) | ptl.profile_idc,)),
        _u32(ptl.compatibility_flags),
        ptl.constraint_flags,                  # 48 bits
        bytes((ptl.level_idc,)),
        # 1111 + min_spatial_segmentation_idc. Zero means "not stated": it comes
        # from the VUI, which is not parsed, and a wrong non-zero value here
        # tells a decoder it may parallelise in a way the stream does not allow.
        b"\xf0\x00",
        b"\xfc",                               # 111111 + parallelismType = 0
        bytes((0xFC | (parsed.chroma_format_idc & 3),)),
        bytes((0xF8 | ((parsed.bit_depth_luma - 8) & 7),)),
        bytes((0xF8 | ((parsed.bit_depth_chroma - 8) & 7),)),
        _u16(0),                               # avgFrameRate: unstated
        # constantFrameRate = 0 (unknown), then the temporal layer count and
        # nesting flag out of the SPS, then lengthSizeMinusOne = 3 so every
        # sample is 4-byte-length-prefixed, exactly as on the H.264 path.
        bytes(((parsed.max_sub_layers & 7) << 3
               | (parsed.temporal_id_nesting & 1) << 2 | 3,)),
        bytes((count,)),
        arrays,
    )


def visual_sample_entry(sps: bytes, pps: bytes, width: int, height: int) -> bytes:
    return _sample_entry(b"avc1", width, height, avcc(sps, pps))


def hevc_sample_entry(vps: bytes, sps: bytes, pps: bytes,
                      parsed: SequenceParameterSet,
                      width: int, height: int) -> bytes:
    """`hvc1`, not `hev1`.

    The two differ only in whether parameter sets may also appear in the
    samples. This muxer strips them out — the same as it does for H.264 — so
    they exist in exactly one place and `hvc1` is the accurate name. It is also
    the stricter one, and the only one Safari will play.
    """
    return _sample_entry(b"hvc1", width, height, hvcc(vps, sps, pps, parsed))


def _sample_entry(kind: bytes, width: int, height: int, config: bytes) -> bytes:
    """A `VisualSampleEntry`, which is identical for both codecs but the name
    and the configuration box hanging off the end of it."""
    return box(
        kind,
        b"\x00" * 6, _u16(1),                  # reserved, data_reference_index
        b"\x00" * 16,                          # pre_defined and reserved
        _u16(width), _u16(height),
        _u32(0x00480000), _u32(0x00480000),    # 72 dpi, as everything writes
        _u32(0), _u16(1),                      # reserved, frame_count
        b"\x00" * 32,                          # compressorname
        _u16(0x0018), b"\xff\xff",             # depth, pre_defined = -1
        config,
    )


def init_segment(sps: bytes, pps: bytes, width: int, height: int,
                 timescale: int = TIMESCALE, *, vps: bytes | None = None,
                 parsed: SequenceParameterSet | None = None,
                 rules: NalRules = H264) -> bytes:
    """`ftyp` + `moov`: everything a decoder needs before the first frame.

    The platform keeps this and gives it to every later viewer, because a viewer
    handed only the next fragment sees nothing at all — and that is
    indistinguishable from a dead camera.

    `rules` picks the codec. Everything below the sample entry — the movie
    header, the track, the timescale, `mvex` — is the same either way, and the
    one place the two diverge is the four-character code and the configuration
    box under it.
    """
    hevc = rules is HEVC
    if hevc and parsed is None:                # pragma: no cover - callers check
        raise ValueError("an HEVC init segment needs a parsed sequence parameter set")
    brand = b"hvc1" if hevc else b"avc1"
    sample_entry = (
        hevc_sample_entry(vps or b"", sps, pps, parsed, width, height) if hevc
        else visual_sample_entry(sps, pps, width, height)
    )
    ftyp = box(b"ftyp", b"isom", _u32(0x200), b"isom", b"iso2", brand, b"mp41", b"iso6")

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
        full_box(b"stsd", 0, 0, _u32(1), sample_entry),
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


def to_length_prefixed(nals: list[bytes]) -> bytes:
    """Annex B NALs → the length-prefixed form a sample carries.

    Four-byte lengths, because both configuration records above say
    `lengthSizeMinusOne = 3`.
    """
    return b"".join(_u32(len(nal)) + nal for nal in nals)


class Fmp4Muxer:
    """Access units in, an init segment and fragments out.

    Holds three pieces of state and nothing else: the parameter sets it has
    seen, the fragment sequence number, and the decode time. The last one is why
    a dropped fragment still has to be *counted* — see `advance`.

    `rules` says which codec is arriving. H.264 by default, so nothing that
    predates HEVC has to say so; `HEVC` from `gsu/camera/hevc.py` for a camera
    that streams H.265, which the station remuxes and never transcodes.

    **Known limitation, and NOT the cause of any stutter observed so far: no
    composition time offsets.** Every sample is written with presentation time
    equal to decode time and a fixed duration, because Annex B carries no
    timestamps — `-c copy` into the raw `h264`/`hevc` muxer discards the RTP
    ones, and recovering them would mean parsing slice headers for the picture
    order count and the whole reference-picture-set machinery behind it. On a
    stream with B-frames the pictures still come out in the right order (a
    decoder reorders from the POC in the bitstream, not from the container) but
    the timeline is flat, and a strict downstream muxer drops the last picture.

    This was the standing first suspect for the live view stuttering on the
    HEVC camera, and it was **measured and ruled out**: the bench camera at
    1920x1080 reports `has_b_frames=0` and delivered 21 I and 328 P frames over
    five seconds with no B-frame at all. The stutter was the muxer's clock
    being built from a stale configured frame rate instead of the stream's own
    — see `StreamSession.start`. So this remains a real gap and remains
    unreachable from the hardware in front of us: rpicam-vid and the synthetic
    source emit no B-frames by construction, and this camera emits none by
    configuration. A different camera, or this one on a different profile,
    would reach it. Worth building when something actually sends a B-frame, and
    worth not building before then — a fix with no failing case to prove it is
    how the flat timeline got written this way in the first place.
    """

    def __init__(self, width: int, height: int, fps: float = 30.0,
                 timescale: int = TIMESCALE, rules: NalRules = H264) -> None:
        self.width = width
        self.height = height
        self.rules = rules
        self.timescale = timescale
        self.sample_duration = max(1, round(timescale / max(1.0, float(fps))))
        self.vps: bytes | None = None
        self.sps: bytes | None = None
        self.pps: bytes | None = None
        #: The HEVC SPS, read. `hvcC` and the codec string are both built out of
        #: fields inside it, so it is parsed once when it arrives rather than
        #: twice on every session.
        self.parsed: SequenceParameterSet | None = None
        #: The H.264 SPS, read, for the picture size and the codec string.
        #: Added late and deliberately: this muxer took the H.264 sample entry's
        #: dimensions from the size the *station* was configured for, which on a
        #: 4K camera under a 1080p site policy writes 1920x1080 into a container
        #: full of 3840x2160 pictures. HEVC was fixed to read its SPS and H.264
        #: was flagged rather than fixed; this is the fix.
        self.h264: H264Sps | None = None
        #: Why there is no init segment, when there is a parameter set but it
        #: could not be used. Surfaced by `gsu/stream.py` — the alternative is a
        #: stream that reports healthy and shows black.
        self.reason = ""
        self.sequence = 0
        self.decode_time = 0
        self.samples = 0

    # --- parameter sets --------------------------------------------------

    @property
    def ready(self) -> bool:
        if self.sps is None or self.pps is None:
            return False
        if self.rules.vps is not None and self.vps is None:
            return False
        return self.rules is not HEVC or self.parsed is not None

    @property
    def picture_width(self) -> int:
        """The width of the pictures actually arriving, from their own sequence
        parameter set — falling back to the configured size only before one has
        been seen. Both codecs, one rule: the container describes the stream,
        never the station's intentions about it."""
        for parsed in (self.parsed, self.h264):
            if parsed is not None and parsed.width:
                return parsed.width
        return self.width

    @property
    def picture_height(self) -> int:
        for parsed in (self.parsed, self.h264):
            if parsed is not None and parsed.height:
                return parsed.height
        return self.height

    def init_segment(self) -> bytes | None:
        if not self.ready:
            return None
        return init_segment(
            self.sps, self.pps, self.picture_width, self.picture_height,
            self.timescale, vps=self.vps, parsed=self.parsed, rules=self.rules,
        )

    def codec(self) -> str:
        if self.rules is HEVC:
            return self.parsed.codec_string() if self.parsed else ""
        # From the parsed SPS when there is one, so the codec string and the
        # dimensions cannot come from two different readings of the same bytes.
        # `codec_string()` remains the fallback and remains correct — profile,
        # constraints and level are at fixed offsets — but it is the answer
        # only until the SPS has been read properly.
        if self.h264 is not None:
            return self.h264.codec_string()
        return codec_string(self.sps or b"")

    def _remember(self, kind: int, nal: bytes) -> bool:
        """Store one parameter set. True when it differs from the one held.

        A changed SPS is a changed decoder configuration, and for HEVC it is
        also a re-parse: the profile, tier and level in `hvcC` and in the codec
        string both come from in here, and carrying the old ones forward is how
        a resolution change turns into a black picture rather than a new one.
        """
        rules = self.rules
        slot = {rules.vps: "vps", rules.sps: "sps", rules.pps: "pps"}[kind]
        if getattr(self, slot) == nal:
            return False
        had = getattr(self, slot) is not None
        setattr(self, slot, nal)
        if slot == "sps" and rules is HEVC:
            self.parsed = parse_sps(nal)
            self.reason = "" if self.parsed else (
                "the camera's HEVC sequence parameter set could not be read, so "
                "there is no way to tell a browser what to decode. The stream is "
                "stopped rather than sent as a picture nothing can play."
            )
        elif slot == "sps":
            # H.264. Unlike the HEVC case this is not fatal when it fails: the
            # codec string can still be read from three bytes at fixed offsets,
            # and the dimensions fall back to the configured size — which is
            # what every H.264 stream did until now. So it is a degradation
            # that is *said*, rather than a refusal.
            self.h264 = parse_h264_sps(nal)
            if self.h264 is None:
                log.warning(
                    "The H.264 sequence parameter set could not be read; the "
                    "container will carry this station's configured size "
                    "(%dx%d) rather than the stream's own.",
                    self.width, self.height,
                )
        return had

    # --- frames ----------------------------------------------------------

    def feed(self, unit, nals: list[bytes] | None = None,
             duration: int | None = None
             ) -> tuple[bytes | None, bool, bool]:
        """One access unit → `(fragment, keyframe, parameters_changed)`.

        `nals` lets a caller that has already split the access unit hand the
        result in rather than have it done twice. That is not a micro-
        optimisation: `split_annexb` is a Python loop over every byte of the
        frame, and on a 4K stream at 25 fps a second pass is tens of
        milliseconds per frame on a Pi 2B — enough to matter on the one board
        this has to run on. `gsu/stream.py` splits once and passes it here.

        `duration` is this sample's presentation time in timescale ticks, when
        the caller knows it — the wall-clock gap since the previous frame. A
        camera that slows down in low light (long night exposures) sends frames
        further apart, and Annex B carries no timestamp to say so; without this
        every frame gets `sample_duration` and the timeline runs at the daylight
        rate against a stream arriving slower, which shows as the "race two
        seconds, stall, race two more" stutter after dark. `None` keeps the
        fixed rate — the first frame, dropped frames, and every test source that
        has no wall clock to offer.

        `parameters_changed` is true when this frame carried a sequence
        parameter set different from the one in the current init segment — a
        restarted encoder, or one told to change resolution. The caller has to
        declare a new session before sending the fragment, because parameters
        that no longer match decode as corruption rather than as an error.

        Returns `None` for the fragment when there is nothing sendable yet:
        before the first parameter sets have arrived there is no decoder
        configuration, and a fragment sent then is a fragment nobody can play.
        """
        rules = self.rules
        changed = False
        payload: list[bytes] = []
        keyframe = False
        for nal in (split_annexb(unit.data) if nals is None else nals):
            kind = rules.nal_type(nal)
            if kind in rules.parameter_sets:
                # Stripped from the sample, not merely skipped: they live in the
                # configuration record instead, which is what `avc1` and `hvc1`
                # both promise. Leaving them in the sample as well is what
                # `hev1` means, and Safari will not play that.
                changed = self._remember(kind, nal) or changed
                continue
            if kind == rules.aud:
                # Access unit delimiters carry no picture and MSE has no use for
                # them; the boundary they mark is already the fragment boundary.
                continue
            if kind in rules.keyframes:
                keyframe = True
            payload.append(nal)

        if not payload or not self.ready:
            return None, keyframe, changed
        return (self.fragment(to_length_prefixed(payload), keyframe, duration),
                keyframe, changed)

    def fragment(self, sample: bytes, keyframe: bool,
                 duration: int | None = None) -> bytes:
        """`moof` + `mdat` for one sample, at the current decode time.

        `duration` is this sample's length in timescale ticks; `None` uses the
        fixed `sample_duration`. See `feed`.
        """
        self.sequence += 1
        ticks = self.sample_duration if duration is None else max(1, int(duration))
        moof = self._moof(len(sample), keyframe, ticks)
        self.advance(duration=ticks)
        return moof + box(b"mdat", sample)

    def advance(self, frames: int = 1, duration: int | None = None) -> None:
        """Move the clock on, whether or not the frame was sent.

        Called for dropped fragments too. Decode times must keep pace with the
        wall clock or a player treats a gap as a stall and then as a jump: the
        gap is real and the timeline has to say so, rather than pretending the
        frames that were dropped never existed.

        `duration` is the per-frame step in timescale ticks; `None` uses the
        fixed `sample_duration`, which is what a drop (no wall-clock gap of its
        own) still wants.
        """
        step = self.sample_duration if duration is None else max(1, int(duration))
        self.decode_time += step * frames
        self.samples += frames

    def _moof(self, sample_size: int, keyframe: bool,
              duration: int | None = None) -> bytes:
        ticks = self.sample_duration if duration is None else max(1, int(duration))
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
                _u32(ticks), _u32(sample_size),
            )

        provisional = box(b"moof", mfhd, box(b"traf", tfhd, tfdt, trun(0)))
        return box(b"moof", mfhd, box(b"traf", tfhd, tfdt, trun(len(provisional) + 8)))
