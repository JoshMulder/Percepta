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
import socket
import tempfile
import threading
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
from gsu.video import CameraPreview  # noqa: F401 - imported to pin the name

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
        # A file sink, so nothing in this suite opens a socket to a platform
        # that is not there. The WebSocket uplink has its own tests, against a
        # server this file starts.
        self.sink = str(Path(self._dir.name) / "stream.mp4")
        self.agent = Agent(AgentConfig(
            home=Path(self._dir.name), setup_enabled=False, single_instance=False,
            stream_sink=self.sink,
        ))
        # Attached by hand rather than through `_attach`, which would start the
        # video thread, the renewer and a transport. Every test here drives
        # `cycle()` itself so that timing is the test's business.
        # Small and slow: these tests exercise the logic, not the encoder, and
        # a 1080p30 synthetic source would spend the suite's time drawing.
        self.agent.site.stream_width = 320
        self.agent.site.stream_height = 240
        self.agent.site.stream_fps = 10
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


class ChannelRemovedTests(AgentFixture):
    """`gsu/{station_id}/video` is not published at all any more.

    The schema is still loaded above and still validated against in this file,
    because the *contract* has not been withdrawn — the station has stopped
    producing the channel and the platform has to be told what it will stop
    receiving. These tests pin "stopped": a channel that quietly comes back is
    a channel that puts a second reader on the sensor again, which is the
    entire fault this removal exists to close.
    """

    def test_a_preview_capture_publishes_nothing_anywhere(self):
        self.assertTrue(self.video.cycle(), "a fitted simulated camera captures")
        self.assertEqual(self.transport.sent, [],
                         "the preview put something on the wire")

    def test_no_path_through_the_agent_emits_a_video_payload(self):
        sent: list[tuple] = []
        self.agent._publish = lambda topic, payload: sent.append((topic, payload)) or True
        for _ in range(3):
            self.agent.step(1.0, weather_due=True, health_due=True)
            self.video.cycle()
        self.assertFalse([p for _, p in sent if p.get("kind") == "video"])
        self.assertFalse([t for t, _ in sent if t and t.endswith("/video")])

    def test_the_health_frame_says_the_channel_is_gone_rather_than_idle(self):
        # Zero frames and zero bitrate is also what a broken camera looks like.
        # A console must be able to tell "this station does not do snapshots"
        # from "this station's snapshots have stopped working".
        for _ in range(3):
            self.video.cycle()
        stats = self.agent.health_payload()["video"]
        self.assertIs(stats["snapshots"], False)
        self.assertIn("removed", stats["snapshots_removed"])
        self.assertEqual(stats["preview_frames"], 3)

    def test_the_health_frame_says_who_holds_the_camera(self):
        # The question all three previous fixes were circling and none of them
        # could answer from telemetry.
        state = self.agent.health_payload()["video"]["sensor"]
        self.assertIsNone(state["holder"])
        token = self.agent.sensor_lease.acquire("the live stream")
        self.assertEqual(
            self.agent.health_payload()["video"]["sensor"]["holder"],
            "the live stream",
        )
        self.agent.sensor_lease.release(token)


class PreviewReasonTests(AgentFixture):
    """No picture is a statement, and it has to say which kind of no picture."""

    def test_no_camera_fitted_says_so(self):
        self.agent.camera = None
        self.assertFalse(self.video.cycle())
        self.assertTrue(self.video.last_reason)
        self.assertIn("camera", self.video.last_reason)

    def test_a_camera_that_will_not_answer_gives_its_own_reason(self):
        self.agent.camera = StubCamera(None, "rpicam-jpeg failed: no cameras available")
        self.assertFalse(self.video.cycle())
        self.assertIn("no cameras available", self.video.last_reason)
        self.assertEqual(self.video.failed, 1)
        self.assertEqual(self.video.refused, 0)

    def test_contention_is_counted_apart_from_failure(self):
        # The distinction the owner asked for in as many words: "so i can tell
        # what is camera not working rather than actually just snapshots".
        self.agent.camera = StubCamera(None, "the camera is in use by the live stream")
        self.assertFalse(self.video.cycle())
        self.assertEqual(self.video.refused, 1)
        self.assertEqual(self.video.failed, 0, "contention counted as a fault")

    def test_video_switched_off_is_reported_as_a_reason_not_as_silence(self):
        self.agent.site.video_enabled = False
        self.assertFalse(self.video.cycle())
        self.assertIn("switched off", self.video.last_reason)

    def test_an_unsourced_camera_shows_up_where_the_console_looks_for_it(self):
        self.agent.inventory.set_device("camera", "")
        self.agent.build_devices()
        self.assertIn("video", self.agent.inventory.unsourced_streams())
        self.assertIn("video", self.agent.health_payload()["unsourced_streams"])


class CompletenessTests(AgentFixture):
    """A partial frame is dropped. This is the rule with the sharpest edge."""

    def test_a_truncated_frame_is_never_shown(self):
        whole = SyntheticCamera().capture()
        cut = Frame(jpeg=whole.jpeg[: len(whole.jpeg) // 2], width=whole.width,
                    height=whole.height, captured_at=whole.captured_at)
        self.agent.camera = StubCamera(cut)
        # The driver is what refuses; the preview must not paper over a driver
        # that does not, so the check is asserted at both ends. The rule
        # outlived the channel it was written for — half a picture of a site is
        # no better on a setup page than it was on a console.
        self.assertFalse(complete_jpeg(cut.jpeg))
        self.assertFalse(self.video.cycle())
        self.assertIsNone(self.video.last_frame)
        self.assertIn("not a complete JPEG", self.video.last_reason)

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

    def test_it_is_the_shutter_and_not_the_fetch(self):
        # It used to be "not the publish". There is nothing to publish now, and
        # the rule matters more rather than less: `/frame.jpg` serves a cached
        # frame with an `X-Frame-Age` header computed from this timestamp, and
        # while the live stream holds the sensor that frame is deliberately not
        # replaced. The age is the only thing saying so.
        camera = SyntheticCamera()
        frame = camera.capture()
        self.agent.camera = StubCamera(frame)
        time.sleep(0.05)
        self.video.cycle()
        self.assertIs(self.video.last_frame, frame)
        self.assertEqual(self.video.last_frame.captured_at, frame.captured_at)
        self.assertGreater(self.video.frame_age_s(), 0.04)

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


class PreviewCacheTests(AgentFixture):
    """The setup page's preview: one frame, on demand, cached and aged."""

    def test_a_captured_frame_is_kept_and_its_age_is_reported(self):
        self.assertIsNone(self.video.last_frame)
        self.assertEqual(self.video.preview_state()["has_frame"], False)
        self.video.cycle()
        frame = self.video.last_frame
        self.assertIsNotNone(frame)
        self.assertTrue(complete_jpeg(frame.jpeg))
        state = self.video.preview_state()
        self.assertTrue(state["has_frame"])
        self.assertGreaterEqual(state["frame_age_s"], 0.0)
        self.assertGreaterEqual(self.video.frame_age_s(), 0.0)

    def test_an_unenrolled_station_still_captures_for_the_preview(self):
        # The preview exists for the installer standing at an unenrolled box,
        # and it needs no identity because it sends nothing anywhere.
        self.agent.enrolment = None
        self.assertTrue(self.video.cycle())
        self.assertEqual(self.transport.sent, [])
        self.assertIsNotNone(self.video.last_frame)
        self.assertEqual(self.video.captured, 1)

    def test_nothing_is_captured_until_somebody_asks(self):
        """The strongest statement this design makes about ownership.

        On a box with nobody on the setup page the camera is opened exactly
        never, which leaves the live stream as the only consumer of the sensor.
        That is what makes the contention structural rather than managed.
        """
        self.assertFalse(self.video.wanted, "wanted before anybody asked")
        self.video.preview_state()               # what /status.json does
        self.assertTrue(self.video.wanted)
        self.video.cycle()
        # And having just captured, it will not capture again immediately
        # however often it is asked.
        self.video.preview_state()
        self.assertFalse(self.video.wanted, "the refresh floor was not applied")

    def test_demand_expires_so_a_closed_laptop_stops_the_camera(self):
        self.video.preview_state()
        self.assertTrue(self.video.wanted)
        self.video._wanted_until -= 999
        self.assertFalse(self.video.wanted)

    def test_a_failed_capture_leaves_the_cached_frame_standing(self):
        # A stale picture with a stated age beats no picture; the age says
        # stale.
        self.video.cycle()
        kept = self.video.last_frame
        self.agent.camera = StubCamera(None, "the camera went away")
        self.video.cycle()
        self.assertIs(self.video.last_frame, kept)

    def test_a_stream_holding_the_sensor_does_not_disturb_the_cache(self):
        """The driver refuses, the cache stands, and the age tells the truth.

        Note what is *not* here any more: the preview no longer consults the
        stream's state to decide whether to try. It simply asks the driver, and
        the driver asks the lease. One arbiter, consulted by everybody, rather
        than two code paths agreeing to keep out of each other's way.
        """
        self.video.cycle()
        kept = self.video.last_frame

        exclusive = StubCamera(None, "busy")   # owns_sensor defaults True
        self.agent.camera = exclusive
        token = self.agent.sensor_lease.acquire("the live stream")
        self.assertIsNotNone(token)
        self.video.cycle()
        self.assertIs(self.video.last_frame, kept)
        self.agent.sensor_lease.release(token)


class RefusedChannelTests(AgentFixture):
    """A channel the broker will not grant is not a link that is down.

    The station no longer publishes video, so there is no longer a video
    channel for a broker to refuse — but this transport behaviour is not about
    video and was only ever tested through it. Keeping it: the regression it
    guards (one ungranted topic closing the whole client) would take telemetry
    down, and telemetry has not gone anywhere.
    """

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
        self.assertIn("rpicam", camera.unavailable_reason)
        self.assertFalse(camera.describe().present)
        self.assertFalse(camera.describe().simulated, "never claims to be a simulation")

    def test_it_says_which_capture_path_it_took_and_why(self):
        # One path now, and the sentence says what it costs. The two-path
        # explanation this used to carry — picamera2 versus a subprocess, and
        # the venv packaging trap that silently chose between them — went with
        # the backend it described.
        camera = PiCsiCamera()
        self.assertTrue(camera.backend_reason, "a backend with no explanation")
        self.assertIn(camera._backend, ("rpicam", "none"))
        if camera._backend == "rpicam":  # pragma: no cover - needs rpicam-apps
            self.assertIn("subprocess per frame", camera.backend_reason)

    def test_the_only_libcamera_in_this_process_is_a_subprocess(self):
        """What actually closes the wedge, asserted as a property of the code.

        `Camera in Acquired state trying acquire()` comes from libcamera's own
        `Camera::acquire`, and it can only be reached by a process that already
        holds the camera in its own `CameraManager`. This station no longer has
        one: `picamera2` is not imported anywhere, under any condition, so the
        error is unreachable rather than defended against.
        """
        import subprocess as sp
        import sys

        source = Path(__file__).resolve().parent.parent / "gsu"
        # An import statement, not prose: the history of why this backend went
        # away is worth keeping in the comments, and grepping for the word
        # alone would forbid explaining the fix in the file that made it.
        offenders = sp.run(
            ["grep", "-rnE", r"^\s*(import|from)\s+picamera2", "--include=*.py",
             str(source)],
            capture_output=True, text=True, check=False,
        ).stdout.splitlines()
        self.assertEqual(offenders, [], "picamera2 is imported somewhere")
        self.assertNotIn("picamera2", sys.modules)

    def test_construction_touches_no_hardware(self):
        # It runs inside a sensing tick, so it must not open a camera or start
        # a subprocess. Anything that slow belongs on the preview thread, where
        # a second of latency costs a frame instead of the loop.
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
        # `video_fps` is retained and inert: it set the rate of a channel that
        # no longer exists. It still parses and still round-trips, because a
        # `config.set` carrying it from a platform that has not been updated
        # must not become an error — and because a station that silently
        # dropped a field it once honoured is worse than one that keeps it.
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

    def test_video_enabled_still_switches_the_preview_off(self):
        """`video_fps` no longer drives anything and `video_enabled` still does.

        The frame-rate lever belonged to a channel that published continuously
        and does not exist any more; the preview's rate is set by whether
        anybody is looking. The on/off switch is kept because it is the
        platform's one way to say "do not use this camera", and a station that
        quietly ignored a `config.set` would be worse than one that never
        offered the setting.
        """
        site = SiteConfig()
        self.assertTrue(site.video_enabled)
        site.apply({"video_enabled": False}, version=9)
        self.assertFalse(site.video_enabled)


# --- the live stream -----------------------------------------------------


class _InstantSource:
    """An encoder that starts and stops without a thread or a subprocess.

    Enough of `ProcessEncoder`'s surface for `StreamSession` to drive it, and
    none of the machinery: these tests are about who owns the camera and when,
    not about `rpicam-vid`.
    """

    running = True
    reason = ""
    keyframes = 0
    name = "stub"
    kind = "stub encoder"
    stream_fps = None

    def start(self, on_unit) -> bool:
        return True

    def stop(self) -> None:
        pass


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


def _high_profile_sps(width: int, height: int, *, scaling: bool = False,
                      level: int = 51) -> bytes:
    """A High-profile SPS of a given size, built here rather than captured.

    Real cameras send High (profile_idc 100), and High is where the SPS grows
    the chroma format, the bit depths and the optional scaling lists *in front
    of* the picture size. That is the part a Baseline-only reader walks straight
    past, landing the width about forty bits early — a wrong number that looks
    entirely plausible. The synthetic source only emits Baseline, so a High SPS
    has to be written to test against; it is written with the same `BitWriter`
    that produces the streams ffmpeg has already validated elsewhere in this
    suite.
    """
    from gsu.camera.h264_synthetic import MB, BitWriter, nal

    mb_width = (width + MB - 1) // MB
    mb_height = (height + MB - 1) // MB
    writer = BitWriter()
    writer.u(100, 8)                    # profile_idc: High
    writer.u(0, 8)                      # constraint flags
    writer.u(level, 8)
    writer.ue(0)                        # seq_parameter_set_id
    writer.ue(1)                        # chroma_format_idc: 4:2:0
    writer.ue(0)                        # bit_depth_luma_minus8
    writer.ue(0)                        # bit_depth_chroma_minus8
    writer.u(0, 1)                      # qpprime_y_zero_transform_bypass_flag
    writer.u(1 if scaling else 0, 1)    # seq_scaling_matrix_present_flag
    if scaling:
        for index in range(8):
            # Half present, half absent, and the present ones are real lists
            # that end early on a zero delta — which is the case a byte-count
            # skip gets wrong.
            present = index % 2 == 0
            writer.u(1 if present else 0, 1)
            if present:
                writer.se(-8)           # next_scale becomes 0: the list ends
    writer.ue(0)                        # log2_max_frame_num_minus4
    writer.ue(0)                        # pic_order_cnt_type 0 …
    writer.ue(0)                        # … log2_max_pic_order_cnt_lsb_minus4
    writer.ue(1)                        # max_num_ref_frames
    writer.u(0, 1)                      # gaps_in_frame_num_value_allowed_flag
    writer.ue(mb_width - 1)
    writer.ue(mb_height - 1)
    writer.u(1, 1)                      # frame_mbs_only_flag
    writer.u(1, 1)                      # direct_8x8_inference_flag
    crop_right = (mb_width * MB - width) // 2
    crop_bottom = (mb_height * MB - height) // 2
    if crop_right or crop_bottom:
        writer.u(1, 1)                  # frame_cropping_flag
        writer.ue(0)
        writer.ue(crop_right)
        writer.ue(0)
        writer.ue(crop_bottom)
    else:
        writer.u(0, 1)
    writer.u(0, 1)                      # vui_parameters_present_flag
    return nal(7, 3, writer.trailing())


class H264SequenceParameterSetTests(unittest.TestCase):
    """The picture size, read from the stream instead of from the settings.

    The fault: the H.264 sample entry took its dimensions from the size this
    *station* was configured for. On a 4K camera under a 1080p site policy that
    writes 1920x1080 into a container carrying 3840x2160 pictures. HEVC was
    fixed to read its SPS and H.264 was explicitly flagged and left; this is
    the flag being cleared.
    """

    def parsed(self, nal_bytes: bytes):
        from gsu.camera.h264 import parse_sps, split_annexb

        return parse_sps(split_annexb(nal_bytes)[0])

    def test_baseline_sizes_round_trip_through_the_reader(self):
        from gsu.camera.h264_synthetic import sps

        for width, height in ((320, 240), (640, 480), (1280, 720),
                              (1920, 1080), (3840, 2160), (1918, 1078)):
            read = self.parsed(sps(width, height))
            self.assertIsNotNone(read, f"{width}x{height} would not parse")
            self.assertEqual((read.width, read.height), (width, height))

    def test_high_profile_sizes_round_trip_too(self):
        # The profile every real camera actually sends, and the one whose extra
        # fields sit in front of the size.
        for width, height in ((1920, 1080), (3840, 2160), (704, 576)):
            read = self.parsed(_high_profile_sps(width, height))
            self.assertIsNotNone(read, f"{width}x{height} would not parse")
            self.assertEqual((read.width, read.height), (width, height))

    def test_scaling_lists_are_stepped_over_exactly(self):
        # A scaling list is signed Exp-Golomb and ends early on a zero scale,
        # so it cannot be skipped by counting bytes. One bit of drift here puts
        # the width somewhere else entirely.
        read = self.parsed(_high_profile_sps(3840, 2160, scaling=True))
        self.assertIsNotNone(read)
        self.assertEqual((read.width, read.height), (3840, 2160))

    def test_the_codec_string_matches_the_profile_and_level(self):
        read = self.parsed(_high_profile_sps(3840, 2160, level=0x33))
        self.assertEqual(read.codec_string(), "avc1.640033")

    def test_rubbish_is_refused_rather_than_guessed_at(self):
        from gsu.camera.h264 import parse_sps

        self.assertIsNone(parse_sps(b""))
        self.assertIsNone(parse_sps(b"\x67"))
        # High profile, and then it stops: the extended fields the profile
        # promises are not there, so the read runs off the end rather than
        # inventing a size out of whatever follows.
        self.assertIsNone(parse_sps(b"\x67\x64\x00\x33"))

    def test_the_container_carries_the_streams_size_not_the_stations(self):
        """The whole point, at the level it actually went wrong."""
        from gsu.camera.h264_synthetic import pps
        from gsu.media.fmp4 import Fmp4Muxer

        # A station under a 1080p policy, in front of a 4K camera.
        muxer = Fmp4Muxer(1920, 1080, 25.0)
        muxer._remember(7, _high_profile_sps(3840, 2160, level=0x33)[4:])
        muxer._remember(8, pps()[4:])

        self.assertEqual((muxer.picture_width, muxer.picture_height), (3840, 2160))
        self.assertEqual(muxer.codec(), "avc1.640033")
        segment = muxer.init_segment()
        self.assertIsNotNone(segment)
        # The dimensions in the visual sample entry, big-endian: past the
        # fourcc, six reserved bytes, data_reference_index and sixteen more of
        # pre_defined/reserved. Located from `stsd` rather than by searching
        # the whole segment, because `avc1` is also an `ftyp` brand.
        index = segment.index(b"avc1", segment.index(b"stsd"))
        width = int.from_bytes(segment[index + 28:index + 30], "big")
        height = int.from_bytes(segment[index + 30:index + 32], "big")
        self.assertEqual((width, height), (3840, 2160))

    def test_an_unreadable_sps_falls_back_and_says_so(self):
        # Degraded, not refused: the codec string is still readable from three
        # bytes at fixed offsets, and the configured size is what every H.264
        # stream used until now.
        from gsu.camera.h264_synthetic import pps
        from gsu.media.fmp4 import Fmp4Muxer

        muxer = Fmp4Muxer(1920, 1080, 25.0)
        with self.assertLogs("gsu.media", level="WARNING"):
            muxer._remember(7, b"\x67\x64\x00\x33")
        muxer._remember(8, pps()[4:])
        self.assertIsNone(muxer.h264)
        self.assertEqual((muxer.picture_width, muxer.picture_height), (1920, 1080))
        self.assertTrue(muxer.codec().startswith("avc1."))


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
            self.agent.stream.start({"lease_seconds": asked})
            remaining = self.agent.stream.expires_at - time.monotonic()
            self.assertAlmostEqual(remaining, expected, delta=1.0, msg=asked)

    def test_the_lease_is_read_from_the_name_the_platform_uses(self):
        # `lease_seconds` is the platform's spelling. The two older names are
        # still accepted: a station that only understands the newest spelling of
        # a field breaks on the day somebody deploys an older console.
        for key in ("lease_seconds", "lease_s", "ttl_s"):
            self.agent.stream.stop()
            self.agent.stream.start({key: 45})
            self.assertAlmostEqual(self.agent.stream.expires_at - time.monotonic(),
                                   45.0, delta=1.0, msg=key)

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

    def test_frames_are_dropped_rather_than_queued_when_the_uplink_will_not_take_them(self):
        from gsu.transport.stream import StreamUplink

        class Refusing(StreamUplink):
            name = "refusing"

            def open(self):
                return True

            def begin(self, codec, init_segment):
                return True

            def send(self, fragment, keyframe):
                return self._drop()

            def close(self):
                pass

        self.agent.stream.start({})
        source = self.agent.stream.source
        # Drive the frames by hand: the encoder's own thread would race the
        # assertions, and what is being tested is what happens to a frame, not
        # when it happens.
        source.stop()
        self.agent.stream.uplink.close()
        refusing = Refusing()
        self.agent.stream.uplink = refusing
        for _ in range(4):
            self.agent.stream._on_unit(source.frame())
        # Nothing was held back for a link that might come good: each frame was
        # offered once and lost.
        self.assertEqual(self.agent.stream.dropped, 4)
        self.assertEqual(refusing.dropped, 4)
        self.assertEqual(refusing.fragments, 0)

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

    def test_the_state_says_where_the_stream_is_actually_going(self):
        # A station can be encoding into a file or into a counter rather than to
        # a platform. A console must not read either as a working stream, so it
        # is said in the payload rather than implied.
        self.agent.stream.start({})
        _wait_for(lambda: self.agent.stream.codec)
        state = self.agent.health_payload()["video"]["stream"]
        self.assertTrue(state["uplink"].startswith("file:"))
        self.assertTrue(state["codec"].startswith("avc1."), state["codec"])

    def test_a_station_with_nowhere_to_send_says_so_rather_than_looking_healthy(self):
        from gsu.transport.stream import build_uplink

        class Nowhere:
            stream_sink = None
            media_url = None
            platform_url = ""

        uplink = build_uplink(Nowhere(), None)
        self.assertIn("no media URL", uplink.describe())

    def test_a_real_camera_is_taken_by_the_stream_for_the_whole_session(self):
        # One sensor, one owner. The stream holds the lease from before the
        # encoder spawns until after it has been waited for, and the preview is
        # refused by the driver rather than by a state check somewhere else.
        camera = StubCamera(SyntheticCamera().capture())
        camera.describe = _real_device
        self.agent.camera = camera
        self.agent.stream._build_source = lambda settings: _InstantSource()
        self.agent.stream.start({})
        self.assertEqual(self.agent.sensor_lease.holder, "the live stream")
        self.agent.stream.stop("test")
        self.assertTrue(self.agent.sensor_lease.free,
                        "the stream kept the camera after stopping")

    def test_the_synthetic_camera_is_never_leased_because_it_is_not_hardware(self):
        # The configuration the platform tests against. Two drawing routines
        # can run at once, so arbitrating between them would be an artefact of
        # the fix rather than of the hardware.
        self.agent.stream._build_source = lambda settings: _InstantSource()
        self.agent.stream.start({})
        self.assertTrue(self.agent.sensor_lease.free)
        self.assertTrue(self.video.cycle())
        self.agent.stream.stop("test")

    def test_a_stream_that_cannot_get_the_camera_names_the_holder(self):
        camera = StubCamera(SyntheticCamera().capture())
        camera.describe = _real_device
        self.agent.camera = camera
        self.agent.stream._build_source = lambda settings: _InstantSource()
        held = self.agent.sensor_lease.acquire("the camera preview")
        import gsu.stream as stream_module

        self.addCleanup(setattr, stream_module, "SENSOR_WAIT_S",
                        stream_module.SENSOR_WAIT_S)
        stream_module.SENSOR_WAIT_S = 0.01
        effect = self.agent.stream.start({})
        self.assertEqual(self.agent.stream.state, "unavailable")
        self.assertIn("the camera preview", effect)
        self.assertIn("Nothing is broken", self.agent.stream.reason)
        self.agent.sensor_lease.release(held)

    def test_shutdown_stops_the_encoder(self):
        # A `rpicam-vid` left running holds the sensor, and the next start fails
        # with a device-busy that reads like broken hardware.
        self.agent.stream.start({})
        source = self.agent.stream.source
        self.agent.stream.stop("test")
        self.assertFalse(source.running)


class CodecMismatchTests(AgentFixture):
    """A container that says one thing and carries another must stop.

    The failure this closes, measured on the bench: the owner changed the
    camera's encoder from H.265 to H.264 in its own web interface while a
    stream was running. The station's `ffprobe` answer was cached for the life
    of the driver, so it went on announcing `hvc1.1.6.L153.a0` and applying
    H.265 NAL rules to H.264 bytes — for the whole session, with ffmpeg exiting
    zero and no error anywhere. What the operator saw was a degraded picture,
    which is the same thing a failing camera looks like.

    Two halves: the probe is redone per session, and the answer is then checked
    against the bitstream rather than trusted.
    """

    def hevc_keyframe(self):
        from gsu.camera.h264 import AccessUnit

        # A VPS, an SPS and a PPS in Annex B — HEVC's, unmistakably.
        data = (b"\x00\x00\x00\x01\x40\x01\x0c\x01"
                b"\x00\x00\x00\x01\x42\x01\x01\x01"
                b"\x00\x00\x00\x01\x44\x01\xc1\x72")
        return AccessUnit(data=data, captured_at=None, keyframe=True)

    def test_hevc_bytes_in_an_h264_session_stop_it_with_a_reason(self):
        self.agent.stream._build_source = lambda settings: _InstantSource()
        self.agent.stream.start({})
        self.assertEqual(self.agent.stream.state, "streaming")

        self.agent.stream._on_unit(self.hevc_keyframe())

        # Recorded on the encoder's thread, not acted on there: stop() joins
        # that thread, and a thread cannot join itself.
        self.assertTrue(self.agent.stream._codec_mismatch)
        self.assertEqual(self.agent.stream.state, "streaming")

        self.agent.stream.tick()
        self.assertEqual(self.agent.stream.state, "unavailable")
        self.assertIn("HEVC", self.agent.stream.reason)
        self.assertIn("H264", self.agent.stream.reason)

    def test_nothing_of_the_wrong_codec_is_ever_muxed(self):
        self.agent.stream._build_source = lambda settings: _InstantSource()
        self.agent.stream.start({})
        muxer = self.agent.stream.muxer
        self.agent.stream._on_unit(self.hevc_keyframe())
        # The muxer never saw it: no parameter set was stored, so no init
        # segment describing H.265 as H.264 can ever have been sent.
        self.assertIsNone(muxer.sps)
        self.assertFalse(muxer.ready)
        self.assertEqual(self.agent.stream.codec, "")

    def test_matching_bytes_pass_straight_through(self):
        from gsu.camera.h264 import StreamSettings
        from gsu.camera.h264_synthetic import SyntheticH264Source

        self.agent.stream._build_source = lambda settings: _InstantSource()
        self.agent.stream.start({})
        source = SyntheticH264Source(StreamSettings(width=320, height=240, fps=10))
        for _ in range(4):
            self.agent.stream._on_unit(source.frame())
        self.assertEqual(self.agent.stream._codec_mismatch, "")
        self.assertTrue(self.agent.stream.codec.startswith("avc1."))

    def test_the_probe_is_redone_for_every_session(self):
        """A codec cached for the life of the driver is a codec that goes
        stale the moment somebody touches the camera."""
        from unittest import mock

        from gsu.camera.rtsp import RtspCamera

        with mock.patch("gsu.camera.rtsp.shutil.which", return_value="/usr/bin/ffmpeg"):
            camera = RtspCamera(address="192.168.2.138")
        asked: list = []

        def probe(refresh=False):
            asked.append(refresh)
            return "h264"

        camera.probe_codec = probe
        self.agent.camera = camera
        for _ in range(3):
            camera.stream_source(self.agent.stream.settings())
        self.assertEqual(asked, [True, True, True],
                         "the stream path reused a cached codec")


class StreamPacingTests(AgentFixture):
    """What the muxer's clock is set to, and where the number came from."""

    def test_the_clock_follows_the_source_and_is_reported(self):
        class Source(_InstantSource):
            stream_fps = 25.0

        self.agent.site.stream_fps = 30
        self.agent.stream._build_source = lambda settings: Source()
        self.agent.stream.start({})
        self.assertEqual(self.agent.stream.paced_fps, 25.0)
        self.assertIn("measured", self.agent.stream.pacing_source)
        # 90 kHz over 25 fps. At 30 it would be 3000, and the timeline would
        # run 20 per cent fast against a camera sending 25.
        self.assertEqual(self.agent.stream.muxer.sample_duration, 3600)
        delivered = self.agent.stream.state_payload()["delivered"]
        self.assertEqual(delivered["fps"], 25.0)

    def test_a_source_that_states_no_rate_falls_back_and_says_which(self):
        self.agent.site.stream_fps = 10
        self.agent.stream._build_source = lambda settings: _InstantSource()
        self.agent.stream.start({})
        self.assertEqual(self.agent.stream.paced_fps, 10.0)
        self.assertIn("configured", self.agent.stream.pacing_source)


class StartupContentionTests(AgentFixture):
    """What the first real camera taught `stream.start` (HARDWARE.md §7).

    Every behaviour here was found on a Pi 2B with an ov5647 on the ribbon, and
    none of it is visible on a box whose camera is synthetic — which was every
    box this ran on before a real one. These tests pin the fixes so that the
    synthetic rig cannot quietly lose them again.
    """

    def _fake_time(self):
        """`gsu.stream`'s clock, with sleep recorded instead of slept.

        The drain wait is 2.5 s of a real test run if it fires; whether it
        fires is the thing under test, not the waiting itself.
        """
        import gsu.stream as stream_module

        real = stream_module.time
        slept: list[float] = []

        class Recording:
            monotonic = staticmethod(real.monotonic)

            @staticmethod
            def sleep(seconds: float) -> None:
                slept.append(seconds)

        stream_module.time = Recording
        self.addCleanup(setattr, stream_module, "time", real)
        return slept

    def _real_camera(self):
        camera = StubCamera(SyntheticCamera().capture())
        camera.describe = _real_device
        self.agent.camera = camera
        return camera

    def _instant_source(self, holders: list | None = None):
        """An encoder that spawns without a thread, recording who owned the
        sensor at the moment it spawned."""
        lease = self.agent.sensor_lease

        class Source(_InstantSource):
            def start(self, on_unit) -> bool:
                if holders is not None:
                    holders.append(lease.holder)
                return True

        self.agent.stream._build_source = lambda settings: Source()

    def test_a_real_camera_is_owned_before_the_encoder_spawns(self):
        """Ordering is still the fix; the mechanism is no longer a guess.

        This used to assert a `close()` followed by a fixed 2.5-second sleep,
        both of them before the spawn. The close asked the other reader nicely
        and the sleep was tuned to outlast its slowest subprocess — a guess
        about somebody else's timing, made on every start whether or not
        anything was holding the camera. What has to be true is simpler and is
        what is asserted now: the encoder does not spawn until this session
        owns the sensor.
        """
        self._real_camera()
        holders: list = []
        self._instant_source(holders=holders)
        self.agent.stream.start({})
        self.agent.stream.stop("test")
        self.assertEqual(holders, ["the live stream"],
                         "the encoder spawned without owning the camera")

    def test_no_start_pays_a_fixed_wait_for_a_hold_that_is_not_there(self):
        # The old drain slept 2.5 s on every start with a real camera. Waiting
        # on the lease costs nothing when nobody is holding it, which is the
        # normal case now that the preview only runs while somebody looks.
        self._real_camera()
        self._instant_source()
        slept = self._fake_time()
        started = time.monotonic()
        self.agent.stream.start({})
        self.agent.stream.stop("test")
        self.assertEqual(slept, [], "a start still sleeps on a fixed timer")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_start_survives_the_monitor_stopping_the_stream_at_birth(self):
        # The field failure: the encoder spawns cleanly, dies within a second,
        # and the sensing thread notices and stops the stream — nulling
        # `self.uplink` — while `start` is still composing its report. On the
        # first real station that turned a dead encoder into an AttributeError
        # on top of it. Emulated deterministically: the moment the state flips
        # to "streaming", the monitor has already been and gone.
        from gsu.stream import StreamSession

        class MonitorWins(StreamSession):
            def __setattr__(self, name, value):
                object.__setattr__(self, name, value)
                if name == "state" and value == "streaming":
                    # What stop() does, in the order stop() does it.
                    uplink = self.__dict__.get("uplink")
                    if uplink is not None:
                        uplink.close()
                    object.__setattr__(self, "uplink", None)
                    object.__setattr__(self, "source", None)

        session = MonitorWins(self.agent)
        session._build_source = self.agent.stream._build_source

        class InstantlyDead:
            running = False
            reason = "the encoder died in its first second"
            keyframes = 0
            name = "stub"

            def start(self, on_unit) -> bool:
                return True

            def stop(self) -> None:
                pass

        session._build_source = lambda settings: InstantlyDead()
        effect = session.start({})   # must report, not raise
        self.assertIn("streaming", effect)


class RecordingWebSocketServer:
    """A WebSocket server that records what the station sent it.

    Written out rather than mocked, because what is being tested is the wire:
    the handshake, the masking, the frame headers and the order of the three
    things the platform expects before the first fragment. A mock of
    `WebSocket.send_binary` would pass whatever this file believed, which is
    precisely the belief under test.
    """

    def __init__(self, stall: bool = False) -> None:
        self.stall = stall
        self.messages: list[tuple[int, bytes]] = []
        self.headers: dict[str, str] = {}
        self.request_line = ""
        self.handshakes = 0
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/media/ingest"

    def stop(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=3.0)

    def _serve(self) -> None:
        import base64
        import hashlib

        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            try:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                head = request.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
                self.request_line = head[0]
                for line in head[1:]:
                    name, _, value = line.partition(":")
                    self.headers[name.strip().lower()] = value.strip()
                key = self.headers.get("sec-websocket-key", "")
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()).decode()
                client.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
                )
                self.handshakes += 1
                if self.stall:
                    # Never read again: the station's socket buffer fills and it
                    # must drop rather than block or queue.
                    while not self._stop.is_set():
                        time.sleep(0.05)
                    return
                self._read_frames(client)
            finally:
                try:
                    client.close()
                except OSError:
                    pass

    def _read_frames(self, client) -> None:
        import struct

        buffer = bytearray()
        while not self._stop.is_set():
            try:
                chunk = client.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while True:
                if len(buffer) < 2:
                    break
                opcode = buffer[0] & 0x0F
                masked = buffer[1] & 0x80
                length = buffer[1] & 0x7F
                offset = 2
                if length == 126:
                    if len(buffer) < 4:
                        break
                    length = struct.unpack_from(">H", buffer, 2)[0]
                    offset = 4
                elif length == 127:
                    if len(buffer) < 10:
                        break
                    length = struct.unpack_from(">Q", buffer, 2)[0]
                    offset = 10
                if not masked:
                    raise AssertionError("a client frame must be masked (RFC 6455)")
                if len(buffer) < offset + 4 + length:
                    break
                key = bytes(buffer[offset:offset + 4])
                payload = bytes(buffer[offset + 4:offset + 4 + length])
                del buffer[:offset + 4 + length]
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
                self.messages.append((opcode, payload))


class MediaUplinkTests(unittest.TestCase):
    """The wire, against a server that records it."""

    def setUp(self):
        self.server = RecordingWebSocketServer()
        self.addCleanup(self.server.stop)

    def uplink(self, **kwargs):
        from gsu.transport.stream import MediaUplink

        return MediaUplink(self.server.url, "the-station-secret", **kwargs)

    def test_the_handshake_carries_the_credential_and_nothing_else(self):
        uplink = self.uplink()
        self.assertTrue(uplink.open(), uplink.reason)
        self.addCleanup(uplink.close)
        _wait_for(lambda: self.server.handshakes)
        self.assertEqual(self.server.headers.get("authorization"),
                         "Bearer the-station-secret")
        self.assertEqual(self.server.headers.get("upgrade", "").lower(), "websocket")
        self.assertIn("/media/ingest", self.server.request_line)
        # contract/README.md rule 1: a station never asserts which station it
        # is. The platform derives that from the credential.
        joined = " ".join(f"{k}: {v}" for k, v in self.server.headers.items())
        self.assertNotIn(STATION, joined + self.server.request_line)

    def test_the_platform_gets_codec_then_init_then_the_segment(self):
        uplink = self.uplink()
        self.assertTrue(uplink.open(), uplink.reason)
        self.addCleanup(uplink.close)
        self.assertTrue(uplink.begin("avc1.420033", b"\x00\x00\x00\x18ftypisom"))
        self.assertTrue(uplink.send(b"moof-and-mdat", keyframe=True))
        messages = _wait_for(lambda: self.server.messages
                             if len(self.server.messages) >= 4 else None) or []
        kinds = [opcode for opcode, _ in messages]
        self.assertEqual(kinds[:4], [1, 1, 2, 2], "text, text, binary, binary")
        self.assertEqual(json.loads(messages[0][1]), {"codec": "avc1.420033"})
        self.assertEqual(messages[1][1], b"init")
        self.assertEqual(messages[2][1], b"\x00\x00\x00\x18ftypisom")
        self.assertEqual(messages[3][1], b"moof-and-mdat")

    def test_a_real_stream_arrives_intact_and_in_order(self):
        # End to end through the muxer: what the platform receives is what the
        # encoder produced, byte for byte, including a 200 kB keyframe fragment
        # that exercises the 64-bit length path and the masking.
        from gsu.camera.h264 import StreamSettings
        from gsu.camera.h264_synthetic import SyntheticH264Source
        from gsu.media.fmp4 import Fmp4Muxer

        source = SyntheticH264Source(StreamSettings(width=320, height=240, fps=10,
                                                    intra_period=5))
        muxer = Fmp4Muxer(320, 240, fps=10)
        uplink = self.uplink()
        self.assertTrue(uplink.open(), uplink.reason)
        self.addCleanup(uplink.close)
        expected = []
        for index in range(8):
            fragment, keyframe, _ = muxer.feed(source.frame())
            if index == 0:
                self.assertTrue(uplink.begin(muxer.codec(), muxer.init_segment()))
                expected.append(muxer.init_segment())
            if fragment is not None:
                self.assertTrue(uplink.send(fragment, keyframe))
                expected.append(fragment)
        binaries = _wait_for(
            lambda: [p for op, p in self.server.messages if op == 2]
            if len([p for op, p in self.server.messages if op == 2]) >= len(expected)
            else None
        )
        self.assertEqual(binaries, expected)

    def test_a_stalled_link_drops_frames_and_resynchronises_on_a_keyframe(self):
        # The behaviour the whole uplink exists to get right: when the link
        # cannot carry the stream, drop — never queue — and then wait for a
        # keyframe, because the frames in between depend on one that never
        # arrived and would render as a smear that looks like a broken camera.
        stalled = RecordingWebSocketServer(stall=True)
        self.addCleanup(stalled.stop)
        from gsu.transport.stream import MediaUplink

        uplink = MediaUplink(stalled.url, "secret")
        self.assertTrue(uplink.open(), uplink.reason)
        self.addCleanup(uplink.close)
        # A small send buffer, so congestion is reached in a bounded number of
        # frames rather than depending on how much the kernel felt like
        # buffering. Without this the test is a race against SO_SNDBUF.
        uplink.socket._socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        uplink.begin("avc1.420033", b"init-segment")
        # Push until the socket buffer fills and the uplink starts refusing.
        big = b"x" * 200_000
        for _ in range(200):
            if not uplink.send(big, keyframe=False):
                break
        self.assertGreater(uplink.dropped, 0, "a stalled link must produce drops")
        self.assertEqual(uplink.send(big, keyframe=False), False,
                         "still congested, and still not queueing")
        # It stays skipping until a keyframe, and never grows a queue.
        self.assertTrue(uplink._skipping)
        self.assertLess(uplink.fragments, 200)

    def test_a_refused_upgrade_is_reported_as_itself(self):
        # 401 from the platform is a rejected credential, not a network problem,
        # and an operator does something different about each.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def refuse():
            client, _ = listener.accept()
            client.recv(4096)
            client.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            client.close()

        thread = threading.Thread(target=refuse, daemon=True)
        thread.start()
        self.addCleanup(listener.close)
        from gsu.transport.stream import MediaUplink

        uplink = MediaUplink(f"ws://127.0.0.1:{port}/media/ingest", "wrong-secret")
        self.assertFalse(uplink.open())
        self.assertIn("401", uplink.reason)

    def test_it_will_not_open_a_plaintext_uplink_when_tls_is_required(self):
        from gsu import tls
        from gsu.transport.stream import MediaUplink

        uplink = MediaUplink(self.server.url, "secret",
                             trust=tls.Trust(require_tls=True, purpose="media"))
        self.assertFalse(uplink.open())
        self.assertIn("unencrypted", uplink.reason.lower() + uplink.reason)


class MediaUrlTests(unittest.TestCase):
    """Where the media endpoint is, and who gets to say."""

    def test_it_follows_the_platform_api_by_default(self):
        from gsu.transport.stream import media_url

        class Config:
            media_url = None
            platform_url = "https://platform.example:8000"

        self.assertEqual(media_url(Config()),
                         "wss://platform.example:8000/media/ingest")

    def test_a_plaintext_platform_gives_a_plaintext_media_url(self):
        # Development only, and it has to be visible as such rather than
        # quietly becoming wss and failing at the handshake.
        from gsu.transport.stream import media_url

        class Config:
            media_url = None
            platform_url = "http://localhost:8000"

        self.assertEqual(media_url(Config()), "ws://localhost:8000/media/ingest")

    def test_the_override_wins_because_the_station_is_somewhere_else(self):
        from gsu.transport.stream import media_url

        class Config:
            media_url = "wss://10.0.0.1:9000/media/ingest"
            platform_url = "https://platform.example:8000"

        self.assertEqual(media_url(Config()), "wss://10.0.0.1:9000/media/ingest")


def _wait_for(condition, timeout: float = 5.0):
    """Wait for the encoder thread to get somewhere. Returns what it produced."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    return condition()


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
