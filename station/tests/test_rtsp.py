"""The RTSP camera driver: every seam that can be proven without a camera.

No RTSP camera has ever been plugged into this code — HARDWARE.md §10 is the
register of what that means. What CAN be held to account without one, and is:
the URLs it builds, the secrets it must never render, the completeness gate on
snapshots, the honesty of every failure sentence, and the remux pipeline from
MPEG-TS on a pipe through to fMP4 fragments, the camera's timestamps and all —
driven with real synthetic H.264 wrapped in TS through the same reader and muxer
the field paths use.
"""

import os
import subprocess
import tempfile
import threading
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
    split_credentials,
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


class SplitCredentialTests(unittest.TestCase):
    """The setup page's half of the bargain: a pasted URL gives up its
    credentials to the fields that are stored once and never rendered."""

    def test_a_pasted_url_gives_up_its_credentials(self):
        cleaned, username, password = split_credentials(
            "rtsp://admin:hunter2@cam.local:554/live?channel=1"
        )
        self.assertEqual(cleaned, "rtsp://cam.local:554/live?channel=1")
        self.assertEqual(username, "admin")
        self.assertEqual(password, "hunter2")
        # And the driver accepts what the split produced — the round trip is
        # the whole point.
        self.assertEqual(
            build_url(cleaned, username=username, password=password),
            "rtsp://admin:hunter2@cam.local:554/live?channel=1",
        )

    def test_percent_encoding_is_undone_so_it_is_not_applied_twice(self):
        cleaned, username, password = split_credentials(
            "rtsp://viewer:s3cr3t%20pa%2Fss%40word@cam.local/live"
        )
        self.assertEqual(cleaned, "rtsp://cam.local/live")
        self.assertEqual(password, PASSWORD)
        # build_url re-encodes; the URL ffmpeg gets is byte-identical to the
        # one the vendor printed.
        self.assertIn("s3cr3t%20pa%2Fss%40word",
                      build_url(cleaned, username=username, password=password))

    def test_a_url_without_credentials_is_untouched(self):
        address = "rtsp://cam.local:8554/live?channel=1&subtype=0"
        self.assertEqual(split_credentials(address), (address, "", ""))

    def test_a_bare_host_is_untouched(self):
        self.assertEqual(split_credentials("192.168.1.9"), ("192.168.1.9", "", ""))
        self.assertEqual(split_credentials(""), ("", "", ""))

    def test_a_username_alone_still_moves(self):
        cleaned, username, password = split_credentials("rtsp://admin@cam.local/1")
        self.assertEqual((cleaned, username, password),
                         ("rtsp://cam.local/1", "admin", ""))

    def test_an_at_sign_in_the_query_is_not_read_as_credentials(self):
        # The @ belongs to the query, not to an authority. Inventing a
        # username out of it would corrupt the address.
        address = "rtsp://cam.local/live?token=user@example"
        self.assertEqual(split_credentials(address), (address, "", ""))
        address = "rtsp://cam.local?token=user@example"
        self.assertEqual(split_credentials(address), (address, "", ""))


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

    def test_ffmpegs_words_survive_a_timeout(self):
        """The one path where the diagnosis was thrown away.

        `subprocess.run` puts whatever the child wrote before it was killed on
        the exception. This reported only that the time had passed — the least
        informative true thing available — while "401 Unauthorized" sat unread
        in `exc.stderr`.
        """
        expired = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=8)
        expired.stderr = (b"[rtsp @ 0x1] method DESCRIBE failed: 401\n"
                          b"rtsp://192.0.2.10: Server returned 401 Unauthorized\n")
        camera = fitted_camera()
        with mock.patch("gsu.camera.rtsp.subprocess.run", side_effect=expired):
            self.assertIsNone(camera.capture())
        self.assertIn("401 Unauthorized", camera.unavailable_reason)

    def test_a_camera_that_never_flags_a_keyframe_still_gets_a_picture(self):
        """Keyframes preferred, not required.

        `-skip_frame nokey` needs the demuxer to have flagged them, and when
        that flag never comes the wait never ends: the capture times out and
        the preview goes from a black picture to no picture at all, which is
        worse. So the keyframe attempt is first and on a short budget, and a
        plain decode is the fallback.
        """
        jpeg = a_real_jpeg()
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if "-skip_frame" in command:
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=8)
            return self.run_result(stdout=jpeg)

        camera = fitted_camera()
        with mock.patch("gsu.camera.rtsp.subprocess.run", side_effect=fake_run):
            frame = camera.capture()

        self.assertIsNotNone(frame, camera.unavailable_reason)
        self.assertEqual(len(calls), 2, "the fallback did not run")
        self.assertNotIn("-skip_frame", calls[1])

    def test_a_camera_is_asked_about_keyframes_once_not_every_frame(self):
        """Otherwise the preview pays the keyframe budget for ever.

        Eight seconds of waiting and a second subprocess, per frame, against a
        preview that refreshes every two and a half — on a camera that will
        never answer differently, because whether its keyframes are flagged is
        a property of the camera.
        """
        jpeg = a_real_jpeg()
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "-skip_frame" in command:
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=8)
            return self.run_result(stdout=jpeg)

        camera = fitted_camera()
        with mock.patch("gsu.camera.rtsp.subprocess.run", side_effect=fake_run):
            self.assertIsNotNone(camera.capture())      # learns it, the slow way
            commands.clear()
            self.assertIsNotNone(camera.capture())      # and does not ask again
            self.assertIsNotNone(camera.capture())

        self.assertEqual(len(commands), 2, "one ffmpeg per capture after the first")
        for command in commands:
            self.assertNotIn("-skip_frame", command)

    def test_what_was_learned_does_not_survive_a_rebuild(self):
        # The address, the substream and the encoder can all change under a
        # driver, and the cost of asking again is one slow capture.
        self.assertIsNone(fitted_camera()._skip_frame_works)

    def test_the_two_attempts_together_do_not_exceed_the_old_timeout(self):
        # Or the preview loop's own pacing is broken by a camera that is merely
        # slow, which is the failure this was traded against.
        from gsu.camera.rtsp import CAPTURE_TIMEOUT_S, KEYFRAME_WAIT_S

        budgets = []

        def fake_run(command, **kwargs):
            budgets.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=kwargs["timeout"])

        camera = fitted_camera()
        with mock.patch("gsu.camera.rtsp.subprocess.run", side_effect=fake_run):
            camera.capture()
        self.assertLessEqual(sum(budgets), CAPTURE_TIMEOUT_S)
        self.assertEqual(budgets[0], KEYFRAME_WAIT_S)

    def test_only_keyframes_are_decoded_or_the_still_is_black(self):
        """An RTSP connection joins a stream in progress.

        The first pictures to arrive predict from an IDR this decoder never
        saw, so decoding them against an all-zero reference produces a valid,
        correctly-sized, black JPEG — a preview that resizes to the camera's
        real resolution and shows nothing, which reads as a dark room rather
        than as a decode with no reference behind it.

        `-skip_frame nokey` has to sit before `-i`: it is a decoder option, and
        after the input it would apply to the output instead and do nothing.
        """
        camera = fitted_camera()
        with mock.patch("gsu.camera.rtsp.subprocess.run",
                        return_value=self.run_result(stdout=a_real_jpeg())) as run:
            camera.capture()
        command = run.call_args[0][0]
        self.assertIn("-skip_frame", command)
        self.assertEqual(command[command.index("-skip_frame") + 1], "nokey")
        self.assertLess(command.index("-skip_frame"), command.index("-i"),
                        "a decoder option after -i applies to the output")

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
        # MPEG-TS out, so the camera's PES timestamps survive for the reader to
        # recover — the night-stutter fix. Still no bitstream filter: `-c copy`
        # into TS takes RTP's Annex B as it arrives, and h264_mp4toannexb would
        # reject a stream that is not MP4 length-prefixed (how the first real
        # camera failed).
        self.assertEqual(command[command.index("-f") + 1], "mpegts")
        self.assertNotIn("h264_mp4toannexb", command)
        self.assertNotIn("-bsf:v", command)
        self.assertIn("-an", command)
        joined = " ".join(command)
        for encoder in ("libx264", "h264_v4l2m2m", "-b:v", "-crf"):
            self.assertNotIn(encoder, joined,
                             "a transcode flag on a box that cannot transcode")

    def test_the_pump_turns_piped_mpegts_into_timestamped_access_units(self):
        """The real pump thread, real synthetic H.264 wrapped in MPEG-TS, a pipe
        instead of a camera: cat is the subprocess and the reader/muxer path is
        the same code the field runs. The remux reads TS now, not raw Annex B,
        so this proves the camera's own timestamps survive the whole pump and
        land on the access units — which is the entire night-stutter fix."""
        from gsu.camera.h264_synthetic import SyntheticH264Source
        from gsu.media.fmp4 import Fmp4Muxer
        from tests.mpegts_stream import build_ts

        synthetic = SyntheticH264Source(
            StreamSettings(width=320, height=240, fps=10, intra_period=5),
        )
        frames = [synthetic.frame() for _ in range(8)]
        # A few frames a second with a deliberately irregular gap — the night
        # camera the fix is for. In 90 kHz ticks: 0.2s, 0.15s, 0.2s, 0.3s, ...
        gaps = [18000, 13500, 18000, 27000, 18000, 9000, 18000]
        pts = [90000]
        for gap in gaps:
            pts.append(pts[-1] + gap)
        stream = build_ts([f.data for f in frames], pts)
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(stream)
            path = handle.name
        self.addCleanup(os.unlink, path)

        source = self.source()
        source.command = lambda: ["cat", path]
        source.tool = "cat"

        units = []
        self.assertTrue(source.start(units.append))
        source._thread.join(timeout=10)

        self.assertEqual(len(units), len(frames))
        self.assertTrue(units[0].keyframe)
        # The camera's timestamps came through the pump intact — the property
        # `_pts_duration` turns into a per-sample duration downstream.
        self.assertEqual([u.pts for u in units], pts,
                         "the camera's timestamps did not survive the pump")

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

    def test_a_wedged_consumer_does_not_stall_the_pipe(self):
        """The drain thread's whole reason to exist: a stalled parse/mux/uplink
        must not backpressure the encoder's pipe. That backpressure made ffmpeg
        miss its RTSP keepalive, and the camera dropped the feed every ~2 min on
        a 2B remuxing 1080p. Wedge the consumer on its first access unit and
        prove the pipe is read to EOF anyway — without the drain thread the read
        loop is stuck inside the callback and never reads another byte."""
        from gsu.camera.h264 import AnnexBReader
        from gsu.camera.h264_synthetic import SyntheticH264Source

        synthetic = SyntheticH264Source(
            StreamSettings(width=320, height=240, fps=10, intra_period=5))
        data = b"".join(synthetic.frame().data for _ in range(8))
        pieces = [data[i:i + 65536] for i in range(0, len(data), 65536)]
        drained = threading.Event()

        class _Stdout:
            def read(self, _n):
                if pieces:
                    return pieces.pop(0)
                drained.set()
                return b""

        got_unit = threading.Event()
        release = threading.Event()

        def on_unit(_unit):
            got_unit.set()
            release.wait(5.0)          # wedge the consumer on the first unit

        source = self.source()
        source._on_unit = on_unit
        source._reader = AnnexBReader(source.nal_rules)
        source._process = SimpleNamespace(
            stdout=_Stdout(), stderr=None, poll=lambda: 0,
            wait=lambda timeout=None: 0, terminate=lambda: None,
            kill=lambda: None,
        )
        source._stop.clear()
        thread = threading.Thread(target=source._pump, daemon=True)
        self.addCleanup(thread.join, 2.0)
        self.addCleanup(source._stop.set)
        self.addCleanup(release.set)
        thread.start()

        self.assertTrue(got_unit.wait(3.0),
                        "the consumer never received an access unit")
        self.assertTrue(drained.wait(3.0),
                        "the pipe was not drained while the consumer was wedged")
        release.set()

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
                                  setup_enabled=False, single_instance=False, demo=True))
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

    def test_the_muxer_clock_is_paced_to_the_probed_rate_not_the_stored_one(self):
        """The first stream after any restart used to run 20 per cent fast.

        `settings()` is called before `_build_source()`, and it used to read
        `camera.stream_fps` — which at that moment still held the value seeded
        from a stored `fps` param, because the probe that corrects it runs
        inside `stream_source()`. A stored 30 against a camera sending 25 built
        the muxer's clock at 30, so the timeline advanced faster than frames
        arrived: stutter and catch-up. Only on the first stream, because every
        later one reused the cached probe — which is what made it look
        intermittent and restart-linked.
        """
        camera = fitted_camera()
        session = self.agent_with(camera).stream
        session.agent.site.stream_fps = 30

        def probe(refresh=False):
            camera.stream_fps = 25.0        # what stream_source() learns
            return "h264"

        camera.probe_codec = probe
        source = session._build_source(session.settings())
        # The rate travels on the source, which is built after the probe.
        self.assertEqual(source.stream_fps, 25.0)
        # And site policy is left alone: it is what the station would ask an
        # encoder for, and there is no encoder to ask on a remux path.
        self.assertEqual(session.settings().fps, 30)

    def test_a_leftover_fps_param_cannot_reach_the_driver_at_all(self):
        """`50a4d85` removed the registry field; boxes still carry the value.

        The inventory filters stored params by *constructor signature*, not by
        what the registry currently declares — so a field removed from the
        registry goes on being honoured for as long as the driver will accept
        it. Removing the argument is what actually retires the setting.
        """
        from gsu.devices.inventory import _instantiate

        with mock.patch("gsu.camera.rtsp.shutil.which",
                        return_value="/usr/bin/ffmpeg"):
            camera = _instantiate(
                "gsu.camera.rtsp:RtspCamera",
                {"address": "192.168.1.9", "fps": 30, "transport": "tcp"},
            )
        self.assertIsNone(camera.stream_fps)

    def test_a_network_camera_is_not_sensor_exclusive(self):
        self.assertFalse(sensor_exclusive(fitted_camera()))
        self.assertFalse(sensor_exclusive(SyntheticCamera()))
        self.assertFalse(sensor_exclusive(None))
        # The `True` half of this test went with the CSI driver: it was the
        # only camera that owned a local sensor, and every remaining source is
        # a reader of a stream somebody else produces.

    def test_the_preview_keeps_working_while_a_network_camera_streams(self):
        """No lease is taken for a camera that owns no local sensor.

        Both of this station's paths are readers of a stream the camera
        already serves, so there is nothing to arbitrate — and the preview
        must never be told the camera is busy on account of a contention that
        cannot happen.
        """
        agent = self.agent_with(fitted_camera())
        agent.stream.state = "streaming"
        self.assertFalse(sensor_exclusive(agent.camera))
        self.assertTrue(agent.sensor_lease.free)
        agent.stream.state = "idle"


class RegistryTests(unittest.TestCase):
    def test_the_network_camera_entry_is_drivable_and_keeps_its_secret_field(self):
        from gsu.devices import registry

        device = registry.get("onvif-network-camera")
        self.assertEqual(device.driver, "gsu.camera.rtsp:RtspCamera")
        parameters = {p.name: p for p in device.parameters}
        self.assertEqual(parameters["password"].type, "password")
        # No fps field: the camera decides its own rate and the station only
        # copies what arrives, so it is probed from the stream rather than
        # typed in. A hand-entered 30 against a real 25 played the result
        # fast at the far end, which is what removed the field.
        self.assertNotIn("fps", parameters)
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


class BackendReasonTests(unittest.TestCase):
    """`backend_reason` explains a fault, or says nothing.

    It used to describe the working case too — "RTSP via ffmpeg from <url>;
    snapshots decode one frame, the live stream is remuxed without
    re-encoding". The URL is in the form directly below it on the setup page,
    and the rest is how this build is implemented. A field that is full on
    every healthy station is one people stop reading, which matters because the
    fault it exists for (a venv built without --system-site-packages) looks
    exactly like slow hardware.
    """

    def test_a_working_camera_says_nothing(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            camera = RtspCamera(address="cam.example")
        self.assertEqual(camera.backend, "ffmpeg")
        self.assertEqual(camera.backend_reason, "")

    def test_it_never_repeats_the_address_from_the_form(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            camera = RtspCamera(address="cam.example")
        self.assertNotIn("cam.example", camera.backend_reason)

    def test_a_missing_ffmpeg_still_explains_itself(self):
        # The case the field exists for: nothing else on the page would tell
        # anybody why a correctly configured camera produces no picture.
        with mock.patch("shutil.which", return_value=None):
            camera = RtspCamera(address="cam.example")
        self.assertEqual(camera.backend, "none")
        self.assertIn("ffmpeg", camera.backend_reason.lower())
