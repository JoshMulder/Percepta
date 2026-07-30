"""The RTSP camera driver: every seam that can be proven without a camera.

No RTSP camera has ever been plugged into this code — HARDWARE.md §10 is the
register of what that means. What CAN be held to account without one, and is:
the URLs it builds, the secrets it must never render, the completeness gate on
snapshots, the honesty of every failure sentence, and the remux pipeline from
Annex B bytes on a pipe through to fMP4 fragments — driven with real synthetic
H.264 through the same reader and muxer the field paths use.
"""

import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from gsu.camera import complete_jpeg, jpeg_dimensions, sensor_exclusive
from gsu.camera.h264 import StreamSettings
from gsu.camera.rtsp import (
    RtspCamera,
    RtspRemuxSource,
    build_url,
    redact,
)
from gsu.camera.synthetic import SyntheticCamera

PASSWORD = "s3cr3t pa/ss@word"


def fitted_camera(**overrides) -> RtspCamera:
    params = {
        "address": "192.168.1.9",
        "username": "viewer",
        "password": PASSWORD,
        "rtsp_path": "/Streaming/Channels/101",
    }
    params.update(overrides)
    with mock.patch("gsu.camera.rtsp.shutil.which", return_value="/usr/bin/ffmpeg"):
        return RtspCamera(**params)


def a_real_jpeg() -> bytes:
    """A complete JPEG with known dimensions, from the synthetic encoder."""
    return SyntheticCamera(resolution="320x240").capture().jpeg


class UrlTests(unittest.TestCase):
    def test_host_and_path_become_a_url_with_encoded_credentials(self):
        url = build_url("192.168.1.9", 554, "/Streaming/Channels/101",
                        "viewer", PASSWORD)
        self.assertTrue(url.startswith("rtsp://viewer:"))
        self.assertNotIn(PASSWORD, url, "raw password must be percent-encoded")
        self.assertIn("s3cr3t%20pa%2Fss%40word", url)
        self.assertTrue(url.endswith("@192.168.1.9:554/Streaming/Channels/101"))

    def test_a_bare_path_gains_its_slash_and_a_bare_host_its_port(self):
        self.assertEqual(build_url("cam.local", 8554, "stream1"),
                         "rtsp://cam.local:8554/stream1")

    def test_a_full_url_is_taken_as_written(self):
        url = build_url("rtsp://cam.local:8554/live?channel=1&subtype=0")
        self.assertEqual(url, "rtsp://cam.local:8554/live?channel=1&subtype=0")

    def test_credentials_inside_the_url_are_refused(self):
        # One stored copy of the secret, in the field that is never rendered.
        with self.assertRaises(ValueError) as caught:
            build_url("rtsp://admin:hunter2@cam.local/live")
        self.assertIn("username and password fields", str(caught.exception))

    def test_no_address_is_a_sentence_not_a_traceback(self):
        with self.assertRaises(ValueError):
            build_url("")

    def test_redact_strips_credentials_and_nothing_else(self):
        url = build_url("cam.local", 554, "/live", "viewer", PASSWORD)
        self.assertEqual(redact(url), "rtsp://cam.local:554/live")
        self.assertEqual(redact("rtsp://cam.local/live"), "rtsp://cam.local/live")


class JpegDimensionTests(unittest.TestCase):
    def test_dimensions_come_out_of_a_real_frame(self):
        self.assertEqual(jpeg_dimensions(a_real_jpeg()), (320, 240))

    def test_garbage_is_none_never_a_guess(self):
        self.assertIsNone(jpeg_dimensions(None))
        self.assertIsNone(jpeg_dimensions(b""))
        self.assertIsNone(jpeg_dimensions(b"\xff\xd8\xff"))
        self.assertIsNone(jpeg_dimensions(b"not a jpeg at all" * 10))


class SnapshotTests(unittest.TestCase):
    """capture() through a faked ffmpeg."""

    def run_result(self, stdout=b"", stderr=b"", code=0):
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    def test_a_complete_frame_is_published_with_its_own_dimensions(self):
        camera = fitted_camera()
        jpeg = a_real_jpeg()
        with mock.patch("gsu.camera.rtsp.subprocess.run",
                        return_value=self.run_result(stdout=jpeg)) as run:
            frame = camera.capture()
        self.assertIsNotNone(frame)
        self.assertEqual((frame.width, frame.height), (320, 240))
        self.assertEqual(camera.status, "streaming")
        command = run.call_args[0][0]
        self.assertIn("-frames:v", command)
        self.assertIn("-rtsp_transport", command)

    def test_an_incomplete_frame_is_dropped_with_a_reason(self):
        camera = fitted_camera()
        with mock.patch("gsu.camera.rtsp.subprocess.run",
                        return_value=self.run_result(stdout=a_real_jpeg()[:100])):
            self.assertIsNone(camera.capture())
        self.assertTrue(camera.unavailable_reason)

    def test_ffmpegs_own_words_reach_the_reason(self):
        camera = fitted_camera()
        stderr = b"rtsp://x/live: 401 Unauthorized"
        with mock.patch("gsu.camera.rtsp.subprocess.run",
                        return_value=self.run_result(stderr=stderr, code=1)):
            self.assertIsNone(camera.capture())
        self.assertIn("401 Unauthorized", camera.unavailable_reason)

    def test_a_timeout_is_abandoned_and_said(self):
        camera = fitted_camera()
        with mock.patch(
            "gsu.camera.rtsp.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15),
        ):
            self.assertIsNone(camera.capture())
        self.assertIn("did not deliver a frame", camera.unavailable_reason)

    def test_failures_back_off_rather_than_hammering_the_camera(self):
        camera = fitted_camera()
        with mock.patch("gsu.camera.rtsp.subprocess.run",
                        return_value=self.run_result(code=1)) as run:
            camera.capture()
            camera.capture()      # inside the back-off window
        self.assertEqual(run.call_count, 1)

    def test_three_failures_report_the_slot_failed(self):
        camera = fitted_camera()
        camera._next_attempt = 0.0
        with mock.patch("gsu.camera.rtsp.subprocess.run",
                        return_value=self.run_result(code=1)):
            for _ in range(3):
                camera._next_attempt = 0.0
                camera.capture()
        self.assertEqual(camera.status, "failed")

    def test_no_ffmpeg_is_a_packaging_sentence_not_a_camera_fault(self):
        with mock.patch("gsu.camera.rtsp.shutil.which", return_value=None):
            camera = RtspCamera(address="192.168.1.9")
        self.assertEqual(camera.status, "absent")
        self.assertIsNone(camera.capture())
        self.assertIn("apt install ffmpeg", camera.unavailable_reason)
        self.assertIn("ffmpeg", camera.describe().detail)


class SecretTests(unittest.TestCase):
    """The password is stored, used, and never seen again."""

    def test_nothing_a_human_reads_carries_the_password(self):
        camera = fitted_camera()
        surfaces = [
            camera.describe().detail,
            camera.backend_reason,
            camera.unavailable_reason,
        ]
        with mock.patch("gsu.camera.rtsp.subprocess.run",
                        return_value=SimpleNamespace(
                            returncode=1, stdout=b"",
                            stderr=camera._url.encode())):
            camera.capture()
        surfaces.append(camera.unavailable_reason)
        source = camera.stream_source(StreamSettings())
        surfaces.append(source.kind)
        for text in surfaces:
            self.assertNotIn(PASSWORD, text or "")
            self.assertNotIn("s3cr3t%20", text or "",
                             "the encoded form is the same secret")

    def test_the_url_ffmpeg_gets_does_carry_them(self):
        # The one place they must appear, because RTSP has no other channel.
        camera = fitted_camera()
        self.assertIn("viewer:", camera._url)
        source = camera.stream_source(StreamSettings())
        self.assertIn(camera._url, source.command())


class RemuxSourceTests(unittest.TestCase):
    def source(self, **kwargs) -> RtspRemuxSource:
        with mock.patch("gsu.camera.rtsp.shutil.which",
                        return_value="/usr/bin/ffmpeg"):
            return RtspRemuxSource(
                StreamSettings(width=320, height=240, fps=10),
                url="rtsp://cam.local/live", **kwargs,
            )

    def test_the_command_copies_and_never_encodes(self):
        command = self.source().command()
        self.assertIn("copy", command)
        self.assertIn("h264_mp4toannexb", command)
        self.assertIn("-an", command)
        joined = " ".join(command)
        for encoder in ("libx264", "h264_v4l2m2m", "-b:v", "-crf"):
            self.assertNotIn(encoder, joined,
                             "a transcode flag on a box that cannot transcode")

    def test_the_pump_turns_piped_annexb_into_access_units(self):
        """The real pump thread, real synthetic H.264, a pipe instead of a
        camera: cat is the subprocess and the reader/muxer path is the same
        code the field runs."""
        from gsu.camera.h264_synthetic import SyntheticH264Source
        from gsu.media.fmp4 import Fmp4Muxer

        synthetic = SyntheticH264Source(
            StreamSettings(width=320, height=240, fps=10, intra_period=5),
        )
        annexb = b"".join(synthetic.frame().data for _ in range(8))
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(annexb)
            path = handle.name
        self.addCleanup(os.unlink, path)

        source = self.source()
        source.command = lambda: ["cat", path]
        source.tool = "cat"

        units = []
        self.assertTrue(source.start(units.append))
        source._thread.join(timeout=10)

        self.assertGreaterEqual(len(units), 7)
        self.assertTrue(units[0].keyframe)

        muxer = Fmp4Muxer(320, 240, fps=10)
        fragments = 0
        for unit in units:
            fragment, _, _ = muxer.feed(unit)
            if fragment is not None:
                fragments += 1
        self.assertTrue(muxer.ready, "no parameter sets survived the remux")
        self.assertGreaterEqual(fragments, 7)
        self.assertIsNotNone(muxer.init_segment())
        source.stop()

    def test_a_source_that_dies_says_so_in_its_own_words(self):
        source = self.source()
        source.command = lambda: ["cat", "/does/not/exist"]
        source.tool = "cat"
        self.assertTrue(source.start(lambda unit: None))
        source._thread.join(timeout=10)
        self.assertIn("exited", source.reason)
        self.assertFalse(source.running)

    def test_no_ffmpeg_refuses_to_start_with_the_packaging_reason(self):
        with mock.patch("gsu.camera.rtsp.shutil.which", return_value=None):
            source = RtspRemuxSource(StreamSettings(), url="rtsp://cam/live")
        self.assertFalse(source.start(lambda unit: None))
        self.assertIn("ffmpeg", source.reason)


class StreamSeamTests(unittest.TestCase):
    """StreamSession takes the camera's own source and leaves the sensor
    relinquish dance to cameras that actually own a sensor."""

    def agent_with(self, camera):
        import tempfile as tf
        from pathlib import Path

        from gsu.agent import Agent
        from gsu.config import AgentConfig

        directory = tf.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        agent = Agent(AgentConfig(home=Path(directory.name),
                                  setup_enabled=False, single_instance=False))
        self.addCleanup(agent.shutdown)
        agent.camera = camera
        agent.inventory.drivers["camera"] = camera
        return agent

    def test_build_source_prefers_the_cameras_own_stream(self):
        camera = fitted_camera()
        session = self.agent_with(camera).stream
        with mock.patch("gsu.camera.rtsp.shutil.which",
                        return_value="/usr/bin/ffmpeg"):
            source = session._build_source(session.settings())
        self.assertIsInstance(source, RtspRemuxSource)
        self.assertIn("remux", session.encoder_choice)

    def test_a_camera_that_cannot_stream_yields_a_reason_not_an_encoder(self):
        with mock.patch("gsu.camera.rtsp.shutil.which", return_value=None):
            camera = RtspCamera(address="192.168.1.9")
        session = self.agent_with(camera).stream
        with mock.patch("gsu.camera.rtsp.shutil.which", return_value=None):
            source = session._build_source(session.settings())
        self.assertIsNone(source)
        self.assertIn("ffmpeg", session.reason)

    def test_the_muxer_clock_is_paced_to_the_cameras_own_rate(self):
        camera = fitted_camera(fps=25)
        session = self.agent_with(camera).stream
        self.assertEqual(session.settings().fps, 25)

    def test_a_network_camera_is_not_sensor_exclusive(self):
        self.assertFalse(sensor_exclusive(fitted_camera()))
        self.assertFalse(sensor_exclusive(SyntheticCamera()))
        self.assertFalse(sensor_exclusive(None))

        from gsu.camera.picsi import PiCsiCamera

        self.assertTrue(sensor_exclusive(PiCsiCamera()))

    def test_snapshots_keep_running_while_a_network_camera_streams(self):
        from gsu.video import VideoPublisher

        agent = self.agent_with(fitted_camera())
        agent.stream.state = "streaming"
        publisher = VideoPublisher(agent)
        self.assertFalse(publisher._stream_has_the_camera(agent.camera))
        agent.stream.state = "idle"


class RegistryTests(unittest.TestCase):
    def test_the_network_camera_entry_is_drivable_and_keeps_its_secret_field(self):
        from gsu.devices import registry

        device = registry.get("onvif-network-camera")
        self.assertEqual(device.driver, "gsu.camera.rtsp:RtspCamera")
        parameters = {p.name: p for p in device.parameters}
        self.assertEqual(parameters["password"].type, "password")
        self.assertIn("fps", parameters)
        self.assertIn("transport", parameters)

    def test_the_inventory_builds_it_and_a_bad_url_is_a_recorded_reason(self):
        import tempfile as tf
        from pathlib import Path

        from gsu.devices.inventory import Inventory

        with tf.TemporaryDirectory() as directory:
            inventory = Inventory(Path(directory) / "devices.json")
            inventory.set_device("camera", "onvif-network-camera", {
                "address": "rtsp://admin:pw@cam.local/live",
            })
            with mock.patch("gsu.camera.rtsp.shutil.which",
                            return_value="/usr/bin/ffmpeg"):
                driver = inventory.build("camera", {})
            self.assertIsNone(driver)
            self.assertIn("username and password fields",
                          inventory.reasons["camera"])

            inventory.set_device("camera", "onvif-network-camera", {
                "address": "cam.local", "password": PASSWORD,
            })
            with mock.patch("gsu.camera.rtsp.shutil.which",
                            return_value="/usr/bin/ffmpeg"):
                driver = inventory.build("camera", {})
            self.assertIsNotNone(driver)
            report = {r.slot: r for r in inventory.report()}["camera"]
            self.assertNotIn(PASSWORD, report.detail)


if __name__ == "__main__":
    unittest.main()
