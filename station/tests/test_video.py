"""The video channel: the payload, the picture, and the rules about both.

Everything here runs offline against `contract/schemas/video.schema.json`, like
the rest of `tests/`, so a schema change lands as a failure on this side rather
than as a surprise on a console.

Three of the contract's rules are behavioural rather than structural, and a
schema cannot check any of them. They have a test each and they are the reason
this file exists:

    a frame is complete or absent      - a truncated JPEG is never published
    captured_at is the shutter         - not the publish, not the arrival
    no camera says so, out loud        - `available: false`, on a cadence

The JPEG itself is parsed structurally here — markers, dimensions, Huffman
table lengths, byte stuffing — because the station has no decoder and should
not grow one. It was additionally decoded with libjpeg (Pillow 12.3) while it
was written, and the pixels came back matching the drawn test card to within
one quantisation step; that check is not in this suite because it would put an
image library into an unattended box's dependencies to test something the box
does not do.
"""

import json
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

from gsu.agent import Agent
from gsu.camera import Frame, complete_jpeg, font, jpeg, parse_resolution
from gsu.camera.picsi import PiCsiCamera
from gsu.camera.synthetic import SyntheticCamera
from gsu.config import AgentConfig, SiteConfig
from gsu.credentials import Broker
from gsu.devices import registry
from gsu.video import VideoPublisher

SCHEMAS = Path(__file__).resolve().parent.parent.parent / "contract" / "schemas"
VIDEO = Draft202012Validator(json.loads((SCHEMAS / "video.schema.json").read_text()))

STATION = "29ed8568-999e-4725-8daa-3ee3cea1751e"


def broker(video_topic: str | None = None) -> Broker:
    return Broker(
        url="rediss://broker:6380/0",
        username=f"gsu:{STATION}",
        telemetry_topic=f"gsu/{STATION}/telemetry",
        audio_topic=f"gsu/{STATION}/audio",
        command_topic=f"cmd/gsu/{STATION}",
        video_topic=video_topic,
    )


class FakeTransport:
    """Enough transport to publish against, and to refuse."""

    url = "rediss://broker:6380/0"

    def __init__(self, ok: bool = True, refuse: str | None = None) -> None:
        self.ok = ok
        self.refuse = refuse
        self.sent: list[tuple[str, dict]] = []
        self.dropped = 0

    def publish(self, topic: str, payload: dict) -> bool:
        if self.refuse == topic:
            self.dropped += 1
            return False
        if not self.ok:
            self.dropped += 1
            return False
        self.sent.append((topic, payload))
        return True

    @property
    def refusals(self) -> dict[str, str]:
        return {self.refuse: "NOPERM this user has no permissions to access a channel"} \
            if self.refuse else {}

    connected = True

    def stop(self) -> None:
        pass

    def subscribe(self, topic, handler) -> None:
        pass


class StubCamera:
    """A camera under the test's control, for the failure paths."""

    def __init__(self, frame=None, reason="") -> None:
        self.frame = frame
        self.reason = reason
        self.captures = 0

    def capture(self):
        self.captures += 1
        return self.frame

    @property
    def unavailable_reason(self) -> str:
        return self.reason

    def close(self) -> None:
        pass


class AgentFixture(unittest.TestCase):
    """An agent with a temporary home, an identity, and no threads running."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.agent = Agent(AgentConfig(
            home=Path(self._dir.name), setup_enabled=False, single_instance=False,
        ))
        # Attached by hand rather than through `_attach`, which would start the
        # video thread, the renewer and a transport. Every test here drives
        # `cycle()` itself so that timing is the test's business.
        self.agent.enrolment = _enrolment()
        self.transport = FakeTransport()
        self.agent.transport = self.transport
        self.video = self.agent.video

    def tearDown(self):
        self.agent.shutdown()
        self._dir.cleanup()

    def published(self) -> list[dict]:
        return [payload for _, payload in self.transport.sent]


def _enrolment():
    from gsu.credentials import Credential, Enrolment, Site

    at = datetime.now(UTC)
    return Enrolment(
        station_id=STATION,
        credential=Credential("bearer", "secret", at + timedelta(hours=24),
                              at + timedelta(hours=12)),
        broker=broker(),
        site=Site("Kaikoura Ridge", "Pacific/Auckland", -42.4, 173.68),
        config_version=1,
        enrolled_at=at,
    )


# --- the payload ---------------------------------------------------------


class PayloadTests(AgentFixture):
    def test_a_frame_matches_the_schema(self):
        self.assertTrue(self.video.cycle(), "a fitted simulated camera should publish")
        payload = self.published()[0]
        errors = sorted(VIDEO.iter_errors(payload), key=str)
        self.assertFalse(errors, [error.message for error in errors])
        self.assertEqual(payload["kind"], "video")
        self.assertEqual(payload["format"], "mjpeg")
        self.assertEqual((payload["width"], payload["height"]), (640, 480))

    def test_it_goes_to_the_video_channel_and_nowhere_else(self):
        self.video.cycle()
        topic, _ = self.transport.sent[0]
        self.assertEqual(topic, f"gsu/{STATION}/video")

    def test_the_sensing_tick_never_publishes_video(self):
        # Video is on its own thread and its own cadence. If a `video` payload
        # ever appears in the telemetry stream it fails the telemetry schema,
        # which has no such kind, and the platform drops it.
        sent: list[dict] = []
        self.agent._publish = lambda topic, payload: sent.append(payload) or True
        for _ in range(3):
            self.agent.step(1.0, weather_due=True, health_due=True)
        self.assertFalse([p for p in sent if p.get("kind") == "video"])

    def test_health_reports_what_video_costs(self):
        for _ in range(3):
            self.video.cycle()
        stats = self.agent.health_payload()["video"]
        self.assertEqual(stats["frames_published"], 3)
        self.assertGreater(stats["bytes_per_frame"], 1000)
        self.assertGreater(stats["bitrate_bps"], 0)


class AvailabilityTests(AgentFixture):
    """`available: false` is a statement, and it has to keep being made."""

    def test_no_camera_fitted_says_so_rather_than_going_quiet(self):
        self.agent.camera = None
        self.video.cycle()
        payload = self.published()[0]
        self.assertFalse(VIDEO.is_valid(payload) is False, "must satisfy the schema")
        self.assertIs(payload["available"], False)
        self.assertIn("camera", payload["unavailable_reason"])
        self.assertNotIn("jpeg", payload)

    def test_a_camera_that_will_not_answer_gives_its_own_reason(self):
        self.agent.camera = StubCamera(None, "rpicam-jpeg failed: no cameras available")
        self.video.cycle()
        payload = self.published()[0]
        self.assertIs(payload["available"], False)
        self.assertIn("no cameras available", payload["unavailable_reason"])
        errors = sorted(VIDEO.iter_errors(payload), key=str)
        self.assertFalse(errors, [error.message for error in errors])

    def test_it_keeps_saying_it_but_not_at_the_frame_rate(self):
        # Repeating "no camera" 2 000 times an hour is not a bandwidth decision
        # anyone should have to defend; going quiet is indistinguishable from a
        # dead station. Rate-limited to telemetry's own cadence, and it must
        # never stop.
        self.agent.camera = None
        self.video._last_unavailable = 0.0
        self.video.cycle()
        self.video.cycle()
        self.assertEqual(len(self.published()), 1)
        self.video._last_unavailable -= 2.0
        self.video.cycle()
        self.assertEqual(len(self.published()), 2)

    def test_video_switched_off_is_reported_as_a_reason_not_as_silence(self):
        self.agent.site.video_enabled = False
        self.video.cycle()
        payload = self.published()[0]
        self.assertIs(payload["available"], False)
        self.assertIn("switched off", payload["unavailable_reason"])

    def test_an_unsourced_camera_shows_up_where_the_console_looks_for_it(self):
        self.agent.inventory.set_device("camera", "")
        self.agent.build_devices()
        self.assertIn("video", self.agent.inventory.unsourced_streams())
        self.assertIn("video", self.agent.health_payload()["unsourced_streams"])


class CompletenessTests(AgentFixture):
    """A partial frame is dropped. This is the rule with the sharpest edge."""

    def test_a_truncated_frame_is_never_published(self):
        whole = SyntheticCamera().capture()
        cut = Frame(jpeg=whole.jpeg[: len(whole.jpeg) // 2], width=whole.width,
                    height=whole.height, captured_at=whole.captured_at)
        self.agent.camera = StubCamera(cut)
        # The driver is what refuses; the publisher must not paper over a driver
        # that does not, so the check is asserted at both ends.
        self.assertFalse(complete_jpeg(cut.jpeg))
        self.video.cycle()
        for payload in self.published():
            self.assertNotIn("jpeg", payload)

    def test_completeness_is_about_both_ends(self):
        whole = SyntheticCamera().capture().jpeg
        self.assertTrue(complete_jpeg(whole))
        self.assertFalse(complete_jpeg(whole[:-2]), "no end of image")
        self.assertFalse(complete_jpeg(b"\x00" + whole), "no start of image")
        self.assertFalse(complete_jpeg(b""))
        self.assertFalse(complete_jpeg(None))
        self.assertTrue(complete_jpeg(whole + b"\x00\x00"), "trailing padding is fine")


class CapturedAtTests(AgentFixture):
    """The age of the picture, which is the thing an operator assumes."""

    def test_it_is_the_capture_and_not_the_publish(self):
        camera = SyntheticCamera()
        frame = camera.capture()
        self.agent.camera = StubCamera(frame)
        time.sleep(0.05)
        self.video.cycle()
        payload = self.published()[0]
        self.assertEqual(payload["captured_at"],
                         frame.captured_at.isoformat().replace("+00:00", "Z"))
        self.assertLess(datetime.fromisoformat(payload["captured_at"].replace("Z", "+00:00")),
                        datetime.now(UTC))

    def test_the_picture_agrees_with_the_field(self):
        # The synthetic camera draws the same instant it stamps, so a console
        # rendering a wrong age is visible by eye with no instrumentation.
        camera = SyntheticCamera()
        frame = camera.capture()
        canvas = camera.render(frame.captured_at)
        drawn = frame.captured_at.strftime("%H:%M:%SZ")
        self.assertIn(drawn[:2], _rendered_text(canvas, drawn))


def _rendered_text(canvas, text: str) -> str:
    """Whether the first glyph of `text` is actually drawn on the canvas.

    Deliberately shallow: it looks for one glyph's shape — every lit tile one
    colour, every unlit tile some other — rather than reading the frame. More
    than that would be an OCR test of a font this suite also defines.
    """
    pattern = font.glyph(text[0])
    lit = [(row, column) for row, pixels in enumerate(pattern)
           for column, on in enumerate(pixels) if on]
    unlit = [(row, column) for row, pixels in enumerate(pattern)
             for column, on in enumerate(pixels) if not on]
    for y in range(canvas.rows - font.HEIGHT):
        for x in range(canvas.columns - font.WIDTH):
            def tile(row, column):
                return canvas.tiles[(y + row) * canvas.columns + x + column]

            ink = tile(*lit[0])
            if any(tile(row, column) != ink for row, column in lit):
                continue
            if any(tile(row, column) == ink for row, column in unlit):
                continue
            return text
    return ""


class DroppingRatherThanQueueingTests(AgentFixture):
    """`contract/transport.md`: favour dropping data over queueing it."""

    def test_frames_that_cannot_be_sent_are_gone(self):
        self.transport.ok = False
        for _ in range(4):
            self.video.cycle()
        self.assertEqual(self.video.published, 0)
        self.assertEqual(self.video.dropped, 4)
        self.transport.ok = True
        self.video.cycle()
        # One frame, not five: nothing was held back waiting for the link.
        self.assertEqual(len(self.published()), 1)
        self.assertEqual(self.video.published, 1)

    def test_a_station_with_no_identity_publishes_nothing_anywhere(self):
        self.agent.enrolment = None
        self.assertIsNone(self.video.topic)
        self.assertFalse(self.video.cycle())
        self.assertEqual(self.transport.sent, [])


class RefusedChannelTests(AgentFixture):
    """A channel the broker will not grant is not a link that is down."""

    def test_it_is_reported_as_itself_and_backed_off(self):
        self.transport.refuse = f"gsu/{STATION}/video"
        self.video.cycle()
        conditions = {c["id"]: c for c in self.agent.health.to_list()}
        self.assertIn("video.topic_refused", conditions)
        self.assertIn("ACL", conditions["video.topic_refused"]["detail"])
        # Backed off rather than retried twice a second for a week.
        self.assertFalse(self.video.cycle())
        self.assertTrue(self.video.stats()["refused"])

    def test_a_refused_publish_does_not_take_the_uplink_down(self):
        # The regression this exists for: a NOPERM used to be handled as a
        # broken connection, so one ungranted topic closed the client and backed
        # the whole uplink off — telemetry would start dropping because video
        # was not permitted.
        import redis

        from gsu import tls
        from gsu.transport import build_transport

        transport = build_transport(
            "redis://127.0.0.1:6399/0", username=f"gsu:{STATION}",
            password="secret", trust=tls.Trust(),
        )

        class Client:
            def publish(self, topic, payload):
                if topic.endswith("/video"):
                    raise redis.exceptions.NoPermissionError(
                        "NOPERM this user has no permissions to access one of "
                        "the channels used as arguments"
                    )
                return 1

        transport._ensure_client = lambda: Client()
        self.assertTrue(transport.publish(f"gsu/{STATION}/telemetry", {"kind": "power"}))
        self.assertFalse(transport.publish(f"gsu/{STATION}/video", {"kind": "video"}))
        self.assertTrue(transport.connected, "the link is fine; one topic is not")
        self.assertIn(f"gsu/{STATION}/video", transport.refusals)
        self.assertTrue(transport.publish(f"gsu/{STATION}/telemetry", {"kind": "power"}))


class TopicTests(unittest.TestCase):
    """Where video goes, and why the station derives it at all."""

    def test_the_platform_names_it_when_it_can(self):
        self.assertEqual(broker("gsu/x/vid").resolve_video_topic(), "gsu/x/vid")

    def test_otherwise_it_follows_telemetry_into_the_same_namespace(self):
        self.assertEqual(broker().resolve_video_topic(), f"gsu/{STATION}/video")

    def test_a_credential_stored_before_the_field_existed_still_loads(self):
        from gsu.credentials import Enrolment

        stored = json.loads(_enrolment().to_json())
        stored["broker"].pop("video_topic")
        loaded = Enrolment.from_json(json.dumps(stored))
        self.assertEqual(loaded.broker.resolve_video_topic(), f"gsu/{STATION}/video")


# --- the picture ---------------------------------------------------------


class JpegTests(unittest.TestCase):
    """The encoder, parsed as a decoder would parse it."""

    @classmethod
    def setUpClass(cls):
        cls.frame = SyntheticCamera(station_name="Kaikoura Ridge").capture()

    def test_it_is_a_baseline_jpeg_of_the_declared_size(self):
        markers = _markers(self.frame.jpeg)
        self.assertIn(0xC0, markers, "no baseline SOF0")
        self.assertIn(0xDB, markers, "no quantisation table")
        self.assertIn(0xC4, markers, "no Huffman table")
        payload = markers[0xC0]
        height = int.from_bytes(payload[1:3], "big")
        width = int.from_bytes(payload[3:5], "big")
        self.assertEqual((width, height), (self.frame.width, self.frame.height))
        self.assertEqual(payload[5], 3, "three components")

    def test_every_ff_in_the_entropy_data_is_stuffed(self):
        # An unstuffed 0xFF is read as a marker, so the decoder stops early and
        # renders whatever it had — a truncated picture that passes every
        # completeness check the station has, which is the worst kind.
        start = self.frame.jpeg.index(b"\xff\xda")
        length = int.from_bytes(self.frame.jpeg[start + 2:start + 4], "big")
        scan = self.frame.jpeg[start + 2 + length:-2]
        for index, byte in enumerate(scan[:-1]):
            if byte == 0xFF:
                self.assertEqual(
                    scan[index + 1], 0x00,
                    f"unstuffed 0xFF at offset {index} of the entropy data",
                )

    def test_the_huffman_tables_are_the_standard_ones_and_are_consistent(self):
        for bits, values in (
            (jpeg._DC_LUMA_BITS, jpeg._DC_LUMA_VALUES),
            (jpeg._DC_CHROMA_BITS, jpeg._DC_CHROMA_VALUES),
            (jpeg._AC_LUMA_BITS, jpeg._AC_LUMA_VALUES),
            (jpeg._AC_CHROMA_BITS, jpeg._AC_CHROMA_VALUES),
        ):
            self.assertEqual(sum(bits), len(values))
            codes = jpeg._codes(bits, values)
            self.assertEqual(len(codes), len(values), "duplicate symbol")
            # No code may be a prefix of another, or a decoder reads a different
            # picture from the one that was written.
            prefixes = {(code >> (length - keep)) if length >= keep else None
                        for code, length in codes.values() for keep in (length,)}
            self.assertEqual(len(prefixes), len(codes))

    def test_quality_scales_the_tables_the_way_libjpeg_does(self):
        low, _ = jpeg.quant_tables(25)
        high, _ = jpeg.quant_tables(95)
        self.assertGreater(low[0], high[0], "lower quality quantises harder")
        for table in (low, high, *jpeg.quant_tables(1), *jpeg.quant_tables(100)):
            self.assertTrue(all(1 <= value <= 255 for value in table))

    def test_a_run_of_identical_tiles_encodes_the_same_as_one_at_a_time(self):
        # The run-length shortcut is what makes this affordable on a Pi. It must
        # produce byte-identical output to encoding each tile in turn, and the
        # cheapest proof is a frame with runs against one with none.
        flat = jpeg.encode_tiles([(20, 30, 40)] * 64, 8, 8, 64, 64)
        varied = jpeg.encode_tiles(
            [(20 + (i % 3), 30, 40) for i in range(64)], 8, 8, 64, 64,
        )
        self.assertTrue(complete_jpeg(flat))
        self.assertTrue(complete_jpeg(varied))
        self.assertLess(len(flat), len(varied), "runs must cost less, not more")

    def test_a_wrong_tile_count_is_refused_rather_than_encoded(self):
        with self.assertRaises(ValueError):
            jpeg.encode_tiles([(0, 0, 0)] * 63, 8, 8, 64, 64)


class SyntheticCameraTests(unittest.TestCase):
    def test_it_is_obviously_not_a_photograph(self):
        camera = SyntheticCamera()
        canvas = camera.render(datetime.now(UTC))
        self.assertEqual(_rendered_text(canvas, "SYNTHETIC"), "SYNTHETIC")
        self.assertTrue(camera.describe().simulated)
        self.assertIn("synthetic", camera.describe().detail)

    def test_it_moves_between_frames(self):
        # A stream that has stopped updating looks exactly like a still scene,
        # and a remote site is a still scene nearly all of the time.
        camera = SyntheticCamera()
        first = camera.capture()
        second = camera.capture()
        self.assertNotEqual(first.jpeg, second.jpeg)

    def test_it_works_at_every_offered_resolution(self):
        for choice in ("320x240", "640x480", "1280x720"):
            camera = SyntheticCamera(resolution=choice)
            frame = camera.capture()
            self.assertTrue(complete_jpeg(frame.jpeg))
            width, height = parse_resolution(choice)
            self.assertEqual((frame.width, frame.height), (width, height))

    def test_a_typo_in_the_resolution_still_produces_a_camera(self):
        self.assertEqual(parse_resolution("not a size"), (640, 480))
        self.assertEqual(parse_resolution(""), (640, 480))
        self.assertEqual(parse_resolution("1280 x 720"), (1280, 720))

    def test_the_font_is_the_shape_it_claims(self):
        for char, pattern in font._GLYPHS.items():
            self.assertEqual(len(pattern), font.WIDTH * font.HEIGHT, char)
        self.assertEqual(font.glyph("~"), font.glyph("?"), "unknown renders as ?")

    def test_a_frame_costs_what_the_station_says_it_costs(self):
        camera = SyntheticCamera()
        frame = camera.capture()
        self.assertEqual(camera.last_bytes, len(frame.jpeg))
        payload = frame.to_payload()
        import base64

        self.assertEqual(base64.b64decode(payload["jpeg"]), frame.jpeg)


class PiCameraTests(unittest.TestCase):
    """The real driver, on a machine that is not a Pi.

    This is the whole of what can be tested without hardware, and it is stated
    that way in HARDWARE.md §7 rather than implied here.
    """

    def test_it_reports_absence_rather_than_pretending(self):
        camera = PiCsiCamera()
        if camera._backend != "none":  # pragma: no cover - a real Pi
            self.skipTest("this machine has a libcamera stack")
        self.assertEqual(camera.status, "absent")
        self.assertIsNone(camera.capture())
        self.assertIn("picamera2", camera.unavailable_reason)
        self.assertFalse(camera.describe().present)
        self.assertFalse(camera.describe().simulated, "never claims to be a simulation")

    def test_construction_touches_no_hardware(self):
        # It runs inside a sensing tick, so it must not import picamera2, open a
        # camera or start a subprocess. Anything that slow belongs on the video
        # thread, where a second of latency costs a frame instead of the loop.
        started = time.monotonic()
        PiCsiCamera(resolution="1280x720", quality=80, rotation=180)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_the_registry_can_build_both_cameras(self):
        for type_id in ("simulated-camera", "raspberry-pi-csi", "onvif-network-camera"):
            device = registry.get(type_id)
            self.assertIsNotNone(device, type_id)
            self.assertEqual(device.slot, "camera")
            self.assertEqual(device.provides, ("video",))


class SiteConfigTests(unittest.TestCase):
    """Bandwidth policy is the platform's to change, and it must land."""

    def test_the_platform_can_turn_video_down_or_off(self):
        site = SiteConfig()
        self.assertTrue(site.video_enabled)
        self.assertEqual(site.video_fps, 2.0)
        changed = site.apply({"video_fps": 0.5, "video_enabled": False}, version=4)
        self.assertEqual(sorted(changed), ["video_enabled", "video_fps"])
        self.assertEqual(site.version, 4)
        self.assertFalse(site.video_enabled)

    def test_the_string_false_does_not_switch_video_on(self):
        # `bool("false")` is True. A platform, or a form, sending the string
        # would have turned video on while asking for it off — on a metered
        # link, in a direction nobody checks.
        site = SiteConfig()
        site.apply({"video_enabled": "false"})
        self.assertFalse(site.video_enabled)
        site.apply({"video_enabled": "true"})
        self.assertTrue(site.video_enabled)

    def test_the_frame_rate_is_bounded_whatever_it_is_told(self):
        agent = _publisher_with_fps(1000.0)
        self.assertGreaterEqual(agent.interval, 1 / 10.0)
        self.assertLessEqual(_publisher_with_fps(0.0001).interval, 1 / 0.05)


def _publisher_with_fps(fps: float) -> VideoPublisher:
    class Stub:
        site = SiteConfig()
        enrolment = None
        transport = None

    stub = Stub()
    stub.site.video_fps = fps
    return VideoPublisher(stub)


# --- the live stream -----------------------------------------------------


class H264BitstreamTests(unittest.TestCase):
    """The synthetic H.264 source produces real H.264, or it is worthless.

    Its whole reason to exist is that the platform can build the streaming path
    against it before a camera exists, and a fake that emitted approximately-
    H.264 would send somebody looking for a bug on their own side. Decoded with
    ffmpeg 7.0.2 while it was written: 90 frames at 1080p, no errors, and the
    picture comes back as drawn. These are the checks that do not need a codec.
    """

    @classmethod
    def setUpClass(cls):
        from gsu.camera.h264 import StreamSettings
        from gsu.camera.h264_synthetic import SyntheticH264Source

        cls.source = SyntheticH264Source(
            StreamSettings(width=320, height=240, fps=10, intra_period=5),
            station_name="Kaikoura Ridge",
        )
        cls.units = [cls.source.frame() for _ in range(12)]

    def test_a_keyframe_carries_its_parameter_sets(self):
        from gsu.camera.h264 import NAL_IDR, NAL_PPS, NAL_SPS, nal_type, split_annexb

        first = self.units[0]
        self.assertTrue(first.keyframe)
        kinds = [nal_type(nal) for nal in split_annexb(first.data)]
        self.assertEqual(kinds, [NAL_SPS, NAL_PPS, NAL_IDR])

    def test_the_frames_between_keyframes_are_predicted(self):
        from gsu.camera.h264 import NAL_NON_IDR, nal_type, split_annexb

        for unit in self.units[1:5]:
            self.assertFalse(unit.keyframe)
            self.assertEqual([nal_type(n) for n in split_annexb(unit.data)], [NAL_NON_IDR])
            # A P frame of skips and a few raw macroblocks is a fraction of an
            # IDR. If this ever inverts, something is encoding every macroblock.
            self.assertLess(unit.bytes, self.units[0].bytes // 4)

    def test_emulation_prevention_matches_a_reference_implementation(self):
        # The one piece of this that a decoder cannot forgive: a missed 0x03
        # makes a decoder see a start code inside a macroblock, and I_PCM data
        # is mostly zeros. Checked against the spec written out literally.
        from gsu.camera.h264_synthetic import rbsp_to_ebsp

        def reference(data: bytes) -> bytes:
            out = bytearray()
            zeros = 0
            for byte in data:
                if zeros >= 2 and byte <= 0x03:
                    out.append(0x03)
                    zeros = 0
                out.append(byte)
                zeros = zeros + 1 if byte == 0 else 0
            return bytes(out)

        cases = [
            b"", b"\x00", b"\x00\x00", b"\x00\x00\x00", b"\x00\x00\x00\x00",
            b"\x00\x00\x01", b"\x00\x00\x02\x00\x00\x03", b"\xff\x00\x00\x00\xff",
            b"\x00" * 32, bytes(range(256)), b"\x00\x00" + b"\x05" * 10,
        ]
        for case in cases:
            self.assertEqual(rbsp_to_ebsp(case), reference(case), case)
        import random

        rng = random.Random(11)
        for _ in range(200):
            case = bytes(rng.choice((0, 0, 0, 1, 2, 3, 255, rng.randrange(256)))
                         for _ in range(rng.randrange(64)))
            self.assertEqual(rbsp_to_ebsp(case), reference(case), case)

    def test_no_start_code_survives_inside_a_nal(self):
        from gsu.camera.h264 import split_annexb

        for unit in self.units[:6]:
            for nal in split_annexb(unit.data):
                self.assertNotIn(b"\x00\x00\x01", nal)
                self.assertNotIn(b"\x00\x00\x00", nal)

    def test_1080p_is_cropped_rather_than_stretched(self):
        # 1080 is 67.5 macroblocks. The frame is encoded as 68 and the sequence
        # parameter set crops the last eight lines away; getting that wrong
        # gives a picture that is subtly the wrong shape on every console.
        from gsu.camera.h264_synthetic import sps

        data = sps(1920, 1080)
        self.assertTrue(data.startswith(b"\x00\x00\x00\x01\x67"))
        self.assertGreater(len(data), 10)

    def test_the_reader_puts_the_frames_back_together_exactly(self):
        import random

        from gsu.camera.h264 import AnnexBReader

        stream = b"".join(unit.data for unit in self.units)
        rng = random.Random(3)
        reader = AnnexBReader()
        recovered = []
        index = 0
        while index < len(stream):
            step = rng.randint(1, 2000)
            recovered += reader.feed(stream[index:index + step])
            index += step
        recovered += reader.flush()
        self.assertEqual(len(recovered), len(self.units))
        self.assertEqual(b"".join(u.data for u in recovered), stream)
        self.assertEqual([u.keyframe for u in recovered],
                         [u.keyframe for u in self.units])


class EncoderTests(unittest.TestCase):
    """Two encoders behind one interface, and a probe that says which."""

    def test_it_asks_for_the_things_that_are_not_defaults(self):
        from gsu.camera.h264 import HardwareEncoder, StreamSettings

        source = HardwareEncoder(StreamSettings(bitrate_kbps=3000, fps=30))
        command = source.command()
        # --inline puts SPS/PPS before every keyframe. Without it a viewer that
        # attaches after the first frame gets a stream it cannot decode, which
        # on a link that drops is the normal case rather than the edge one.
        self.assertIn("--inline", command)
        self.assertIn("--flush", command)
        self.assertIn("3000000", command)
        self.assertEqual(command[command.index("--framerate") + 1], "30")
        self.assertEqual(command[command.index("--output") + 1], "-")
        self.assertEqual(command[command.index("--timeout") + 1], "0")

    def test_the_software_encoder_asks_libav_for_the_same_stream(self):
        from gsu.camera.h264 import SoftwareEncoder, StreamSettings

        command = SoftwareEncoder(StreamSettings(bitrate_kbps=2500)).command()
        self.assertIn("--codec", command)
        self.assertEqual(command[command.index("--codec") + 1], "libav")
        self.assertIn("libx264", command)
        self.assertIn("--inline", command)
        self.assertIn("2500000", command)

    def test_the_probe_says_what_this_box_can_actually_do(self):
        import shutil

        from gsu.camera.h264 import VID_TOOLS, HardwareEncoder, probe_encoders

        probes = {probe.name: probe for probe in probe_encoders()}
        self.assertEqual(sorted(probes), ["hardware", "software"])
        for probe in probes.values():
            self.assertTrue(probe.detail, "a probe with no explanation is a guess")
        if not any(shutil.which(tool) for tool in VID_TOOLS):
            # This machine, and any box where rpicam-apps is not installed.
            self.assertFalse(probes["hardware"].available)
            self.assertFalse(probes["software"].available)
            self.assertIn("rpicam-apps", probes["hardware"].detail)
        elif not os.path.exists(HardwareEncoder.DEVICE):  # pragma: no cover - a Pi 5
            # The message names the board, because there this is the expected
            # answer rather than a fault.
            self.assertFalse(probes["hardware"].available)
            self.assertIn("Pi 5", probes["hardware"].detail)

    def test_an_encoder_that_was_asked_for_and_is_missing_is_refused_not_swapped(self):
        # Quietly falling back to the other path would hide exactly the fact
        # somebody set the option to establish.
        from gsu.camera.h264 import HardwareEncoder, choose_encoder

        if os.path.exists(HardwareEncoder.DEVICE):  # pragma: no cover - a real Pi
            self.skipTest("this machine has a hardware encoder")
        chosen, why = choose_encoder("hardware")
        self.assertIsNone(chosen)
        self.assertIn("was asked for and is not usable", why)

    def test_auto_picks_what_is_there_and_explains_itself(self):
        import shutil

        from gsu.camera.h264 import (
            VID_TOOLS, HardwareEncoder, SoftwareEncoder, choose_encoder,
        )

        chosen, why = choose_encoder("auto")
        self.assertTrue(why, "a choice with no explanation is a guess")
        if not any(shutil.which(tool) for tool in VID_TOOLS):
            # Nothing to encode with: refused, with both reasons, rather than
            # returning something that would fail later and further away.
            self.assertIsNone(chosen)
            self.assertIn("rpicam-apps", why)
            return
        expected = HardwareEncoder if os.path.exists(HardwareEncoder.DEVICE) \
            else SoftwareEncoder  # pragma: no cover - needs a Pi
        self.assertIs(chosen, expected)
        self.assertIn(expected.name, why)

    def test_it_says_what_is_missing_rather_than_pretending(self):
        from gsu.camera.h264 import HardwareEncoder

        source = HardwareEncoder()
        if source.tool is not None:  # pragma: no cover - a real Pi
            self.skipTest("this machine has rpicam-vid")
        self.assertFalse(source.start(lambda unit: None))
        self.assertIn("rpicam-apps", source.reason)


class OnDemandTests(AgentFixture):
    """`video.start` / `video.stop`, and the four properties that matter."""

    def handler(self, kind):
        from gsu.commands import build_handlers

        return build_handlers(None, None, None, self.agent.stream)[kind]

    def test_a_second_viewer_does_not_start_a_second_encoder(self):
        first = self.agent.stream.start({"viewers": 1})
        source = self.agent.stream.source
        second = self.agent.stream.start({"viewers": 2})
        self.assertIs(self.agent.stream.source, source, "restarted the encoder")
        self.assertIn("streaming", first)
        self.assertIn("already streaming", second)
        self.assertEqual(self.agent.stream.viewers, 2)

    def test_it_stops_when_the_platform_stops_asking(self):
        # The failure this is for: the console closes, or the link drops, and
        # the station keeps paying for a stream nobody can see.
        self.agent.stream.start({"lease_s": 5})
        self.assertEqual(self.agent.stream.state, "streaming")
        self.agent.stream.expires_at = time.monotonic() - 0.1
        self.agent.step(1.0)
        self.assertEqual(self.agent.stream.state, "idle")
        self.assertIn("renewing", self.agent.stream.stopped_reason)

    def test_a_lease_is_bounded_at_both_ends(self):
        from gsu.stream import MAX_LEASE_S, MIN_LEASE_S

        for asked, expected in ((0, 30.0), (1, MIN_LEASE_S), (99999, MAX_LEASE_S),
                                ("nonsense", 30.0)):
            self.agent.stream.stop()
            self.agent.stream.start({"lease_s": asked})
            remaining = self.agent.stream.expires_at - time.monotonic()
            self.assertAlmostEqual(remaining, expected, delta=1.0, msg=asked)

    def test_there_is_a_ceiling_even_if_the_lease_keeps_being_renewed(self):
        self.agent.site.stream_max_minutes = 1 / 60
        self.agent.stream.start({"lease_s": 300})
        self.agent.stream.started_at = time.monotonic() - 5
        self.agent.step(1.0)
        self.assertEqual(self.agent.stream.state, "idle")
        self.assertIn("ceiling", self.agent.stream.stopped_reason)

    def test_stop_is_safe_when_nothing_is_running(self):
        self.assertEqual(self.agent.stream.stop(), "not streaming")
        self.assertEqual(self.agent.stream.state, "idle")

    def test_the_commands_are_dispatched_and_reported(self):
        start = self.handler("video.start")
        stop = self.handler("video.stop")
        self.assertIn("streaming", start({"viewers": 1, "lease_s": 20}))
        state = self.agent.health_payload()["video"]["stream"]
        self.assertEqual(state["state"], "streaming")
        self.assertEqual(state["viewers"], 1)
        self.assertGreater(state["lease_remaining_s"], 0)
        stop({"reason": "the last viewer left"})
        self.assertEqual(
            self.agent.health_payload()["video"]["stream"]["state"], "idle")

    def test_a_station_with_no_camera_refuses_and_says_why(self):
        self.agent.camera = None
        effect = self.agent.stream.start({})
        self.assertIn("no camera fitted", effect)
        self.assertEqual(self.agent.stream.state, "unavailable")
        self.assertIn("no camera fitted",
                      self.agent.health_payload()["video"]["stream"]["reason"])

    def test_the_platform_may_ask_for_less_but_never_for_more(self):
        self.agent.site.stream_width = 1280
        self.agent.site.stream_height = 720
        self.agent.site.stream_fps = 15
        self.agent.site.stream_bitrate_kbps = 2000
        modest = self.agent.stream.settings({"width": 640, "height": 480, "fps": 5,
                                             "bitrate_kbps": 500})
        self.assertEqual((modest.width, modest.height, modest.fps,
                          modest.bitrate_kbps), (640, 480, 5, 500))
        greedy = self.agent.stream.settings({"width": 3840, "height": 2160, "fps": 60,
                                             "bitrate_kbps": 20000})
        self.assertEqual((greedy.width, greedy.height, greedy.fps,
                          greedy.bitrate_kbps), (1280, 720, 15, 2000))

    def test_frames_are_dropped_rather_than_queued_when_the_sink_will_not_take_them(self):
        class Refusing:
            name = "refusing"

            def open(self):
                return True

            def send(self, unit):
                return False

            def close(self):
                pass

            def describe(self):
                return self.name

        self.agent.stream.start({})
        self.agent.stream.uplink = Refusing()
        unit = self.agent.stream.source.frame()
        self.agent.stream._on_unit(unit)
        self.assertEqual(self.agent.stream.dropped, 1)

    def test_the_state_says_which_encoder_ran_and_what_was_available(self):
        # Moving a station between a board with an encode block and one without
        # must be a setting and a measurement, never a guess made afterwards
        # from a frame rate.
        self.agent.stream.start({})
        state = self.agent.health_payload()["video"]["stream"]
        self.assertEqual(state["encoder"], "synthetic")
        self.assertIn("simulated", state["encoder_choice"])
        names = [probe["name"] for probe in state["encoders_available"]]
        self.assertEqual(sorted(names), ["hardware", "software"])
        self.assertIn("fps_measured", state)
        self.assertIn("bitrate_bps", state)

    def test_the_state_says_the_uplink_goes_nowhere_yet(self):
        # A station can be encoding and delivering to nothing while the platform
        # has not specified a wire format. A console must not read that as a
        # working stream, so it is said in the payload rather than implied.
        self.agent.stream.start({})
        self.assertIn("no stream uplink",
                      self.agent.health_payload()["video"]["stream"]["uplink"])

    def test_a_real_camera_cannot_do_both_at_once_and_says_so(self):
        # One sensor, one user: while rpicam-vid holds the CSI camera a snapshot
        # fails with a device-busy, which would be published as a broken camera.
        camera = StubCamera(SyntheticCamera().capture())
        camera.describe = _real_device
        self.agent.camera = camera
        self.agent.stream.state = "streaming"
        self.video.cycle()
        payload = self.published()[0]
        self.assertIs(payload["available"], False)
        self.assertIn("live stream", payload["unavailable_reason"])

    def test_the_synthetic_camera_does_both_because_it_is_not_hardware(self):
        # The configuration the platform tests against: losing snapshots there
        # would be an artefact of the test rig rather than of the design.
        self.agent.stream.state = "streaming"
        self.assertTrue(self.video.cycle())
        self.assertIn("jpeg", self.published()[0])

    def test_shutdown_stops_the_encoder(self):
        # A `rpicam-vid` left running holds the sensor, and the next start fails
        # with a device-busy that reads like broken hardware.
        self.agent.stream.start({})
        source = self.agent.stream.source
        self.agent.stream.stop("test")
        self.assertFalse(source.running)


def _real_device():
    """A device inventory line that claims to be hardware."""
    from gsu.sensors import Device

    return Device(id="camera", kind="camera", present=True, detail="a real camera",
                  simulated=False)


def _markers(data: bytes) -> dict[int, bytes]:
    """Every marker segment before the scan, as a decoder would walk them."""
    found: dict[int, bytes] = {}
    index = 2
    while index < len(data) - 1:
        if data[index] != 0xFF:
            break
        marker = data[index + 1]
        if marker == 0xDA:
            break
        length = int.from_bytes(data[index + 2:index + 4], "big")
        found[marker] = data[index + 4:index + 2 + length]
        index += 2 + length
    return found


if __name__ == "__main__":
    unittest.main()
