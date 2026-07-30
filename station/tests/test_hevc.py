"""HEVC: the NAL grammar, the `hvcC` box, and the codec string.

Everything this file tests fails **silently** when it is wrong. A misread NAL
header produces a stream that looks like parameter sets with no pictures; a
malformed `hvcC` or a wrong codec string produces a video element that stays
black and reports nothing on any console. None of it raises, none of it logs,
and all of it looks exactly like a dead camera from the far end. So the
assertions here are against bytes that came out of something else, not against
what this code thinks it should have produced.

Three sources of truth, in descending order of how much they prove:

  ffmpeg's own bytes   The `hvcC` payloads and codec strings below were taken
                       from ffmpeg muxing the same parameter sets to fMP4 and
                       to DASH. `hvc1.1.6.L120.90` is what ffmpeg writes for
                       1080p Main level 4.0, and it is what this station must
                       write for the same stream.
  a real x265 stream   `fixtures/hevc_x265_128x96.h265`, ten pictures of a test
                       card, produced by libx265 and committed as it came out.
                       Reproduce it with:

                           ffmpeg -f lavfi -i testsrc=size=128x96:rate=25:duration=0.4 \\
                                  -pix_fmt yuv420p -c:v libx265 \\
                                  -x265-params keyint=3:min-keyint=3:bframes=0:qp=38:info=0 \\
                                  -f hevc hevc_x265_128x96.h265

  constructed NALs     For the cases a normal encoder will not produce on
                       demand — a suffix SEI, the whole IRAP range — where the
                       point is the header rules and the payload is irrelevant
                       because nothing here ever decodes one.

The end of the file re-encodes and re-decodes through real ffmpeg when there is
one on the box. That test is skipped in a sandbox and runs on the Pi, which is
where it matters.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gsu.camera.h264 import H264, AnnexBReader, split_annexb
from gsu.camera.hevc import (
    HEVC,
    NAL_PPS,
    NAL_SPS,
    NAL_VPS,
    codec_string,
    nal_type,
    parse_sps,
    unescape,
)
from gsu.media.fmp4 import Fmp4Muxer, hvcc

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STREAM = FIXTURES / "hevc_x265_128x96.h265"


def _hex(text: str) -> bytes:
    return bytes.fromhex(text)


#: Real parameter sets from three libx265 streams, with the `hvcC` payload and
#: the RFC 6381 string ffmpeg produced for each. The station has to agree with
#: both, byte for byte and character for character.
REFERENCE = {
    "128x96 Main": {
        "vps": _hex("40010c01ffff01600000030090000003000003001e928090"),
        "sps": _hex("42010101600000030090000003000003001ea01020616592a493"
                    "2bc05a020000030002000003003210"),
        "pps": _hex("4401c171a112"),
        "hvcc": _hex(
            "0101600000009000000000001ef000fcfdf8f800000f03a00001001840010c"
            "01ffff01600000030090000003000003001e928090a10001002942010101600000"
            "030090000003000003001ea01020616592a4932bc05a020000030002000003003210"
            "a2000100064401c171a112"
        ),
        "codec": "hvc1.1.6.L30.90",
        "size": (128, 96),
        "depth": 8,
    },
    "1920x1080 Main": {
        "vps": _hex("40010c01ffff016000000300900000030000030078959809"),
        "sps": _hex("420101016000000300900000030000030078a003c08010e59656"
                    "6924caf0168080000003008000000c84"),
        "pps": _hex("4401c172b46240"),
        "hvcc": _hex(
            "01016000000090000000000078f000fcfdf8f800000f03a00001001840010c01"
            "ffff016000000300900000030000030078959809a10001002a420101016000000300"
            "900000030000030078a003c08010e596566924caf0168080000003008000000c84a2"
            "000100074401c172b46240"
        ),
        # The one the brief names, and the one a 1080p camera has to produce.
        "codec": "hvc1.1.6.L120.90",
        "size": (1920, 1080),
        "depth": 8,
    },
    "640x480 Main10": {
        "vps": _hex("40010c01ffff02200000030090000003000003005a959809"),
        "sps": _hex("42010102200000030090000003000003005aa0050201e1365959a4"
                    "932bc05a020000030002000003003210"),
        "pps": _hex("4401c172b46240"),
        "hvcc": _hex(
            "0102200000009000000000005af000fcfdfafa00000f03a00001001840010c01"
            "ffff02200000030090000003000003005a959809a10001002b42010102200000030"
            "090000003000003005aa0050201e1365959a4932bc05a020000030002000003003210"
            "a2000100074401c172b46240"
        ),
        # Main10 is profile 2, and its compatibility flags reverse to 4 rather
        # than 6. A Main10 camera announced as Main plays on nothing.
        "codec": "hvc1.2.4.L90.90",
        "size": (640, 480),
        "depth": 10,
    },
}


def hevc_nal(kind: int, payload: bytes = b"\x80\x00") -> bytes:
    """One constructed NAL: two header bytes, then whatever.

    Nothing in the station decodes a payload, so for the header rules the
    payload only has to carry the first-slice bit in its top bit.
    """
    return bytes(((kind << 1) & 0x7E, 1)) + payload


def annexb(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)


class NalHeaderTests(unittest.TestCase):
    """Two bytes, not one — the whole reason a real camera streamed nothing."""

    def test_an_hevc_idr_reads_as_a_parameter_set_under_h264s_rule(self):
        # The failure, stated as a test so it cannot come back. IDR_W_RADL is
        # type 19 and IDR_N_LP is 20; read with H.264's `b & 0x1f` they are 6
        # and 8 — SEI and PPS. An entire stream of pictures therefore looks
        # like parameter sets with no pictures in it, which is why ffmpeg
        # exited zero, 8 Mbit/s flowed, and the reader found one access unit in
        # 109 seconds with nothing in any log to say why.
        for kind, misread in ((19, 6), (20, 8)):
            nal = hevc_nal(kind)
            self.assertEqual(HEVC.nal_type(nal), kind)
            self.assertEqual(H264.nal_type(nal), misread)

    def test_the_first_slice_bit_is_read_past_the_longer_header(self):
        # HEVC's first_slice_segment_in_pic_flag is the top bit of the first
        # payload byte, which is byte 2. Reading byte 1 — where H.264's is —
        # reads the layer and temporal id instead, and those are nearly always
        # non-zero, so every slice would look like the start of a picture and
        # every frame would be cut into as many pieces as it has NALs.
        self.assertTrue(HEVC.starts_picture(hevc_nal(1, b"\x80\x00")))
        self.assertFalse(HEVC.starts_picture(hevc_nal(1, b"\x00\x00")))

    def test_every_intra_random_access_type_is_a_keyframe(self):
        # 16-21: BLA_W_LP, BLA_W_RADL, BLA_N_LP, IDR_W_RADL, IDR_N_LP, CRA_NUT.
        # Missing CRA is the expensive one — x265 emits a CRA rather than an
        # IDR at most keyframes, so a stream whose only sync sample is its
        # first frame is what you get, and the picture never returns after a
        # single dropped fragment.
        for kind in range(16, 22):
            self.assertIn(kind, HEVC.keyframes, kind)
        for kind in (0, 1, 9, 15):
            self.assertNotIn(kind, HEVC.keyframes, kind)

    def test_a_prefix_sei_opens_a_picture_and_a_suffix_sei_does_not(self):
        # 39 and 40 are one apart and mean opposite things. Treating both as
        # openers cuts a frame in half every time a camera appends one.
        self.assertTrue(HEVC.starts_picture(hevc_nal(39)))
        self.assertFalse(HEVC.starts_picture(hevc_nal(40, b"\x00\x00")))


class SequenceParameterSetTests(unittest.TestCase):
    """Read out of real bytes, and checked against what ffmpeg made of them."""

    def test_the_codec_string_is_what_ffmpeg_writes_for_the_same_stream(self):
        for label, case in REFERENCE.items():
            with self.subTest(label):
                self.assertEqual(codec_string(case["sps"]), case["codec"])

    def test_the_dimensions_come_from_the_conformance_window(self):
        for label, case in REFERENCE.items():
            with self.subTest(label):
                parsed = parse_sps(case["sps"])
                self.assertIsNotNone(parsed)
                self.assertEqual((parsed.width, parsed.height), case["size"])
                self.assertEqual(parsed.bit_depth_luma, case["depth"])

    def test_an_odd_size_is_coded_larger_and_cropped_back(self):
        # Worth being exact about, because the H.264 intuition is wrong here.
        # H.264 codes 1080p as 1088 lines and crops, since 1080 is not a whole
        # number of 16-line macroblocks. HEVC's smallest coding block is 8x8, so
        # 1080p needs no window at all — every size in REFERENCE is a clean
        # multiple and none of them exercise this path. This one does: a camera
        # set to 1918x1078 codes 1920x1080 and crops two pixels off each edge.
        parsed = parse_sps(_hex(
            "420101016000000300900000030000030078a003c08010e75596566924caf016"
            "8080000003008000000c84"
        ))
        self.assertEqual((parsed.coded_width, parsed.coded_height), (1920, 1080))
        self.assertEqual((parsed.width, parsed.height), (1918, 1078))

    def test_emulation_prevention_is_undone_exactly(self):
        # The SPSs above are full of 00 00 03. Parsing one without stripping
        # them reads the profile compatibility flags out of alignment and
        # produces a codec string that is wrong in a way nothing reports. The
        # reference is the encoder-side function the synthetic H.264 source
        # already uses, so the two are inverses or one of them is broken.
        from gsu.camera.h264_synthetic import rbsp_to_ebsp

        import random

        rng = random.Random(23)
        cases = [b"", b"\x00\x00\x00", b"\x00\x00\x01", b"\x00\x00\x02",
                 b"\x00\x00\x03", b"\x00" * 16, bytes(range(256))]
        cases += [
            bytes(rng.choice((0, 0, 0, 1, 2, 3, 255, rng.randrange(256)))
                  for _ in range(rng.randrange(48)))
            for _ in range(200)
        ]
        for case in cases:
            self.assertEqual(unescape(rbsp_to_ebsp(case)), case, case)

    def test_a_truncated_parameter_set_is_refused_rather_than_guessed(self):
        # The whole point. A plausible-looking default here is a black picture
        # nobody can explain; an empty codec string stops the stream with a
        # sentence somebody can read.
        sps = REFERENCE["1920x1080 Main"]["sps"]
        for length in (2, 4, 8, 12):
            with self.subTest(length):
                self.assertIsNone(parse_sps(sps[:length]))
                self.assertEqual(codec_string(sps[:length]), "")

    def test_something_that_is_not_a_parameter_set_is_refused(self):
        self.assertIsNone(parse_sps(hevc_nal(1)))
        self.assertIsNone(parse_sps(b""))


class ConfigurationBoxTests(unittest.TestCase):
    """`hvcC`, against ffmpeg's own, byte for byte."""

    def test_the_box_is_byte_identical_to_the_one_ffmpeg_writes(self):
        # The substantial claim in this whole change. Every reserved bit, the
        # array ordering, the counts, the profile/tier/level copied out of the
        # SPS — if any of it is wrong this assertion fails, and if none of it
        # is wrong the box is the one a browser already knows how to read.
        for label, case in REFERENCE.items():
            with self.subTest(label):
                parsed = parse_sps(case["sps"])
                built = hvcc(case["vps"], case["sps"], case["pps"], parsed)
                self.assertEqual(built[:4], (len(case["hvcc"]) + 8).to_bytes(4, "big"))
                self.assertEqual(built[4:8], b"hvcC")
                self.assertEqual(built[8:].hex(), case["hvcc"].hex())

    def test_it_carries_three_arrays_and_one_of_them_is_the_vps(self):
        case = REFERENCE["1920x1080 Main"]
        payload = hvcc(case["vps"], case["sps"], case["pps"],
                       parse_sps(case["sps"]))[8:]
        self.assertEqual(payload[22], 3, "numOfArrays")
        # 0x80 is array_completeness, which is what `hvc1` promises and what
        # the muxer keeps by stripping parameter sets out of the samples.
        self.assertEqual(payload[23], 0x80 | NAL_VPS)
        self.assertIn(case["vps"], payload)
        self.assertIn(case["sps"], payload)
        self.assertIn(case["pps"], payload)


class ReaderTests(unittest.TestCase):
    """The access-unit reader over a real x265 stream."""

    @classmethod
    def setUpClass(cls):
        cls.stream = STREAM.read_bytes()

    def units(self, step: int) -> list:
        reader = AnnexBReader(HEVC)
        units = []
        for index in range(0, len(self.stream), step):
            units += reader.feed(self.stream[index:index + step])
        return units + reader.flush(), reader

    def test_the_real_stream_becomes_its_pictures(self):
        units, _ = self.units(4096)
        self.assertEqual(len(units), 10, "ten coded pictures in the fixture")
        # An IDR_N_LP and then a CRA at every keyint. Four of ten, and the
        # three CRAs are the ones an IDR-only rule would miss.
        self.assertEqual(sum(unit.keyframe for unit in units), 4)
        self.assertTrue(units[0].keyframe)

    def test_it_reassembles_byte_for_byte_at_any_chunk_size(self):
        # A pipe delivers whatever it delivers. The trailing-zero handling and
        # the split-across-a-start-code case are the parts that break, and they
        # break into a stream that still mostly plays, which is the worst kind.
        for step in (1, 3, 7, 64, 997, 65536):
            with self.subTest(step):
                units, _ = self.units(step)
                self.assertEqual(len(units), 10)
                rebuilt = b"".join(unit.data for unit in units)
                self.assertEqual(
                    split_annexb(rebuilt), split_annexb(self.stream),
                )

    def test_a_late_viewer_is_given_the_video_parameter_set_too(self):
        # H.264 has no VPS, so this is the one parameter set that could be
        # forgotten without any H.264 test noticing. A viewer handed SPS and
        # PPS but no VPS gets a black element and no error.
        _, reader = self.units(997)
        kinds = [nal_type(nal) for nal in split_annexb(reader.parameter_sets)]
        self.assertIn(NAL_VPS, kinds)
        self.assertIn(NAL_SPS, kinds)
        self.assertIn(NAL_PPS, kinds)

    def test_parameter_sets_with_no_picture_behind_them_are_not_a_picture(self):
        # The rule the H.264 reader's docstring is about, which HEVC inherits
        # and which has one more parameter set to get wrong. x265 re-emits all
        # three before every keyframe; if each trio became an access unit, a
        # viewer would receive four empty units in this fixture alone.
        reader = AnnexBReader(HEVC)
        units = reader.feed(annexb(hevc_nal(NAL_VPS), hevc_nal(NAL_SPS),
                                   hevc_nal(NAL_PPS)))
        self.assertEqual(units, [])
        units = reader.feed(annexb(hevc_nal(20, b"\x80\x00"),
                                   hevc_nal(NAL_VPS), hevc_nal(NAL_SPS),
                                   hevc_nal(NAL_PPS), hevc_nal(20, b"\x80\x00")))
        self.assertEqual(len(units), 1)
        self.assertTrue(units[0].keyframe)

    def test_a_suffix_sei_stays_with_the_picture_it_followed(self):
        reader = AnnexBReader(HEVC)
        reader.feed(annexb(hevc_nal(1, b"\x80\x00"), hevc_nal(40, b"\x00\x00")))
        # The second slice is what ends the first picture — one access unit of
        # latency is inherent, and the flush releases the second one.
        units = reader.feed(annexb(hevc_nal(1, b"\x80\x00")))
        units += reader.flush()
        self.assertEqual(len(units), 2)
        kinds = [nal_type(nal) for nal in split_annexb(units[0].data)]
        self.assertEqual(kinds, [1, 40], "the suffix SEI belongs to the frame before it")


class HevcMuxerTests(unittest.TestCase):
    """The fMP4 side: `hvc1`, and refusing to guess."""

    def stream_units(self) -> list:
        reader = AnnexBReader(HEVC)
        units = reader.feed(STREAM.read_bytes())
        return units + reader.flush()

    def muxer(self, width=128, height=96) -> Fmp4Muxer:
        return Fmp4Muxer(width, height, fps=25, rules=HEVC)

    def test_the_init_segment_is_hvc1_all_the_way_down(self):
        muxer = self.muxer()
        for unit in self.stream_units():
            muxer.feed(unit)
        segment = muxer.init_segment()
        self.assertIsNotNone(segment)
        self.assertIn(b"hvc1", segment)
        self.assertIn(b"hvcC", segment)
        self.assertNotIn(b"avc1", segment, "an HEVC track announced as AVC")
        self.assertNotIn(b"avcC", segment)
        self.assertEqual(muxer.codec(), REFERENCE["128x96 Main"]["codec"])

    def test_the_samples_carry_no_parameter_sets(self):
        # What `hvc1` (as against `hev1`) promises, and what
        # `array_completeness` in the box asserts. Safari refuses `hev1`.
        muxer = self.muxer()
        seen = 0
        for unit in self.stream_units():
            fragment, _, _ = muxer.feed(unit)
            if fragment is None:
                continue
            seen += 1
            for kind in (NAL_VPS, NAL_SPS, NAL_PPS):
                self.assertNotIn(
                    b"\x00\x00\x00\x01" + bytes(((kind << 1) & 0x7E, 1)), fragment,
                )
        self.assertEqual(seen, 10)

    def test_it_is_not_ready_until_the_video_parameter_set_arrives(self):
        case = REFERENCE["128x96 Main"]
        muxer = self.muxer()
        muxer.feed(_unit(annexb(case["sps"], case["pps"], hevc_nal(20, b"\x80\x00"))))
        self.assertFalse(muxer.ready, "an HEVC init segment without a VPS")
        self.assertIsNone(muxer.init_segment())
        muxer.feed(_unit(annexb(case["vps"], hevc_nal(20, b"\x80\x00"))))
        self.assertTrue(muxer.ready)

    def test_an_unreadable_parameter_set_stops_the_stream_and_says_so(self):
        # The alternative is a guessed codec string, which MSE accepts and then
        # decodes nothing from: no error, no picture, and a station reporting
        # that it is streaming perfectly well.
        case = REFERENCE["128x96 Main"]
        muxer = self.muxer()
        muxer.feed(_unit(annexb(case["vps"], case["sps"][:6], case["pps"],
                                hevc_nal(20, b"\x80\x00"))))
        self.assertFalse(muxer.ready)
        self.assertIsNone(muxer.init_segment())
        self.assertEqual(muxer.codec(), "")
        self.assertIn("could not be read", muxer.reason)

    def test_the_sample_entry_takes_its_size_from_the_stream(self):
        # A network camera's resolution is the camera's, not this station's.
        # The first real one is 4K while the site policy asks for 1080p, and
        # the picture in the container has to be the picture in the stream.
        case = REFERENCE["1920x1080 Main"]
        muxer = Fmp4Muxer(640, 480, fps=25, rules=HEVC)
        muxer.feed(_unit(annexb(case["vps"], case["sps"], case["pps"],
                                hevc_nal(20, b"\x80\x00"))))
        segment = muxer.init_segment()
        # rindex, not index: `hvc1` is also a compatible brand in the ftyp at
        # the very front of the segment, and that one has no dimensions after it.
        entry = segment[segment.rindex(b"hvc1"):]
        self.assertEqual(int.from_bytes(entry[28:30], "big"), 1920)
        self.assertEqual(int.from_bytes(entry[30:32], "big"), 1080)

    def test_h264_is_still_avc1(self):
        # The rules default, and the thing that must not have moved.
        from gsu.camera.h264_synthetic import SyntheticH264Source
        from gsu.camera.h264 import StreamSettings

        source = SyntheticH264Source(StreamSettings(width=320, height=240, fps=10,
                                                    intra_period=5))
        muxer = Fmp4Muxer(320, 240, fps=10)
        for _ in range(3):
            muxer.feed(source.frame())
        segment = muxer.init_segment()
        self.assertIn(b"avc1", segment)
        self.assertIn(b"avcC", segment)
        self.assertNotIn(b"hvcC", segment)
        self.assertTrue(muxer.codec().startswith("avc1."))
        self.assertIsNone(muxer.vps)


def _unit(data: bytes):
    """An access unit carrying exactly these bytes, for the muxer's `feed`."""
    from gsu.camera.h264 import AccessUnit

    return AccessUnit(data=data, captured_at=None)


class RtspCodecTests(unittest.TestCase):
    """The refusal, narrowed rather than removed."""

    def source(self, codec: str):
        from gsu.camera.rtsp import RtspRemuxSource

        return RtspRemuxSource(url="rtsp://camera/stream", codec=codec)

    def test_hevc_gets_the_hevc_muxer_and_the_hevc_grammar(self):
        # These two have to move together. `-f h264` around an H.265 stream is
        # a container that lies, and it is what produced 8 Mbit/s of nothing.
        source = self.source("hevc")
        command = source.command()
        self.assertIn("hevc", command)
        self.assertNotIn("h264", command)
        self.assertEqual(command[command.index("-f") + 1], "hevc")
        self.assertIs(source.nal_rules, HEVC)
        self.assertIn("HEVC", source.kind)

    def test_h264_is_untouched(self):
        source = self.source("h264")
        self.assertEqual(source.command()[source.command().index("-f") + 1], "h264")
        self.assertIs(source.nal_rules, H264)

    def test_it_still_never_transcodes(self):
        for codec in ("h264", "hevc"):
            command = " ".join(self.source(codec).command())
            self.assertIn("-c:v copy", command)
            for encoder in ("libx264", "libx265", "h264_v4l2m2m", "hevc_v4l2m2m"):
                self.assertNotIn(encoder, command,
                                 "a transcode flag on a box that cannot transcode")

    def test_a_codec_that_is_neither_is_refused_by_name(self):
        from gsu.camera.rtsp import RtspCamera

        camera = RtspCamera(address="192.0.2.10")
        camera._codec = "mjpeg"
        camera._ffmpeg = "/usr/bin/ffmpeg"
        from gsu.camera.h264 import StreamSettings

        self.assertIsNone(camera.stream_source(StreamSettings()))
        self.assertIn("MJPEG", camera.unavailable_reason)
        self.assertIn("H.265", camera.unavailable_reason,
                      "the reason should name what is now accepted")

    def test_the_pump_reads_hevc_with_hevc_rules(self):
        """The real pump thread over the real fixture, `cat` standing in for
        ffmpeg — the same trick `test_rtsp.py` uses for the H.264 path.

        This is the only test that covers the wiring rather than the parts: the
        source has to hand its grammar to the reader `ProcessEncoder` builds,
        and it builds three of them (construction, start, and respawn after a
        lost camera). Miss any one and an HEVC camera silently gets the H.264
        reader back.
        """
        from unittest import mock

        with mock.patch("shutil.which", lambda name: "/usr/bin/ffmpeg"):
            source = self.source("hevc")
        source.command = lambda: ["cat", str(STREAM)]
        source.tool = "cat"

        units = []
        self.assertTrue(source.start(units.append), source.reason)
        source._thread.join(timeout=10)
        self.addCleanup(source.stop)

        self.assertEqual(len(units), 10)
        self.assertEqual(sum(unit.keyframe for unit in units), 4)

        muxer = Fmp4Muxer(1920, 1080, fps=25, rules=source.nal_rules)
        for unit in units:
            muxer.feed(unit)
        self.assertTrue(muxer.ready)
        self.assertEqual(muxer.codec(), REFERENCE["128x96 Main"]["codec"])

    def test_hevc_is_no_longer_refused(self):
        from gsu.camera.h264 import StreamSettings
        from gsu.camera.rtsp import RtspCamera

        camera = RtspCamera(address="192.0.2.10")
        camera._codec = "hevc"
        camera._ffmpeg = "/usr/bin/ffmpeg"
        source = camera.stream_source(StreamSettings())
        self.assertIsNotNone(source, camera.unavailable_reason)
        self.assertIs(source.nal_rules, HEVC)


def _can_encode_hevc() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        done = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False
    return "libx265" in (done.stdout or "")


@unittest.skipUnless(_can_encode_hevc(), "needs an ffmpeg built with libx265")
class RoundTripTests(unittest.TestCase):
    """Encode real HEVC, mux it here, and make a decoder play it back.

    Skipped where there is no ffmpeg — which is the sandbox this was written in
    — and runs on the Pi, where ffmpeg is an apt dependency the installer
    already lists. It is the only test here that proves the *whole* claim: not
    that the boxes match a reference, but that something which has never seen
    this code can decode what it produced.
    """

    def encode(self, size: str, pix_fmt: str) -> bytes:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.h265"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration=0.8",
                 "-pix_fmt", pix_fmt, "-c:v", "libx265",
                 "-x265-params", "keyint=5:min-keyint=5:bframes=0:info=0:log-level=none",
                 "-f", "hevc", str(path), "-y"],
                check=True, capture_output=True, timeout=120,
            )
            return path.read_bytes()

    def remux(self, raw: bytes, width: int, height: int) -> bytes:
        reader = AnnexBReader(HEVC)
        units = []
        for index in range(0, len(raw), 997):
            units += reader.feed(raw[index:index + 997])
        units += reader.flush()
        muxer = Fmp4Muxer(width, height, fps=25, rules=HEVC)
        out = bytearray()
        for unit in units:
            fragment, _, _ = muxer.feed(unit)
            if not out:
                segment = muxer.init_segment()
                if segment is None:
                    continue
                out += segment
            if fragment is not None:
                out += fragment
        self.assertTrue(out, "nothing was muxed")
        return bytes(out)

    def probe(self, data: bytes) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.mp4"
            path.write_bytes(data)
            done = subprocess.run(
                ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                 "-show_entries",
                 "stream=codec_name,profile,width,height,pix_fmt,nb_read_frames",
                 "-of", "default=nw=1", str(path)],
                capture_output=True, text=True, timeout=120,
            )
        return dict(
            line.split("=", 1) for line in done.stdout.strip().splitlines() if "=" in line
        )

    def test_a_decoder_plays_what_this_station_produced(self):
        for size, pix_fmt, profile in (("320x240", "yuv420p", "Main"),
                                       ("1920x1080", "yuv420p", "Main"),
                                       ("640x480", "yuv420p10le", "Main 10")):
            with self.subTest(size, pix_fmt=pix_fmt):
                width, height = (int(part) for part in size.split("x"))
                raw = self.encode(size, pix_fmt)
                # Deliberately the wrong configured size: the container must
                # take the picture's shape from the stream, as it does from a
                # network camera whose resolution the station does not set.
                facts = self.probe(self.remux(raw, 320, 240))
                self.assertEqual(facts.get("codec_name"), "hevc")
                self.assertEqual(facts.get("profile"), profile)
                self.assertEqual(int(facts.get("width", 0)), width)
                self.assertEqual(int(facts.get("height", 0)), height)
                self.assertEqual(int(facts.get("nb_read_frames", 0)), 20,
                                 "every frame the encoder produced came back")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
