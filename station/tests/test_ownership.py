"""Who owns the camera: the lease, and the four failures it has to refuse.

This file exists because three previous fixes for one wedge were each correct
and each insufficient, and the reason they were insufficient is that none of
them was a *statement of ownership* — they were places that tried to be polite
about a shared device. What is tested here is the statement:

    a sensor has one owner at a time; ownership is granted as a token; only the
    token that was granted can give it back.

Each test below is a failure that actually happened, or a failure the shape of
the code makes reachable, rather than an exercise of the API.
"""

from __future__ import annotations

import threading
import time
import unittest

from gsu.camera.h264 import sniff_codec, split_annexb
from gsu.camera.ownership import DEFAULT_WAIT_S, SensorBusy, SensorLease


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.lease = SensorLease("camera")

    def test_one_owner_at_a_time_and_the_loser_is_told_who_won(self):
        first = self.lease.acquire("the live stream")
        self.assertIsNotNone(first)
        self.assertIsNone(self.lease.acquire("the camera preview"))
        self.assertEqual(self.lease.holder, "the live stream")
        self.assertIn("the live stream", self.lease.describe())
        self.lease.release(first)
        self.assertTrue(self.lease.free)
        self.assertIsNotNone(self.lease.acquire("the camera preview"))

    def test_a_stale_token_cannot_release_the_current_holder(self):
        """The zombie release. A boolean flag cannot refuse this one.

        The wedge that took the first station off the air was a driver instance
        nobody referenced any more, still acting on a sensor its replacement had
        been given. Under a flag, that instance's release frees the successor's
        hold and the two run concurrently — the bug wearing the fix as a
        disguise.
        """
        stale = self.lease.acquire("the outgoing driver")
        self.lease.release(stale)
        current = self.lease.acquire("the live stream")

        self.assertFalse(self.lease.release(stale), "a stale token freed the sensor")
        self.assertEqual(self.lease.holder, "the live stream")
        self.assertIsNone(self.lease.acquire("the camera preview"))
        self.lease.release(current)

    def test_releasing_nothing_is_harmless(self):
        self.assertFalse(self.lease.release(None))
        token = self.lease.acquire("the live stream")
        self.assertTrue(self.lease.release(token))
        self.assertFalse(self.lease.release(token), "a token worked twice")

    def test_two_tokens_are_never_equal(self):
        # Names repeat — there is only one preview and one stream — so identity
        # cannot come from the name.
        first = self.lease.acquire("the camera preview")
        self.lease.release(first)
        second = self.lease.acquire("the camera preview")
        self.assertNotEqual(first, second)

    def test_a_waiting_caller_gets_it_the_moment_it_is_free(self):
        # The stream waits for an in-flight capture rather than sleeping a
        # fixed 2.5 s and hoping. It must actually wake when the sensor frees.
        held = self.lease.acquire("the camera preview")
        got: list = []

        def waiter():
            got.append(self.lease.acquire("the live stream", wait=5.0))

        thread = threading.Thread(target=waiter)
        started = time.monotonic()
        thread.start()
        time.sleep(0.05)
        self.lease.release(held)
        thread.join(timeout=5.0)
        self.assertTrue(got and got[0], "the waiter never got the sensor")
        self.assertLess(time.monotonic() - started, 2.0,
                        "the waiter slept instead of waking on the release")

    def test_a_caller_that_will_not_wait_is_refused_immediately(self):
        # A preview must never queue behind a stream and then fire the instant
        # it ends — that is the race that killed the next stream at birth.
        held = self.lease.acquire("the live stream")
        started = time.monotonic()
        self.assertIsNone(self.lease.acquire("the camera preview"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.lease.release(held)

    def test_the_context_manager_always_gives_it_back(self):
        with self.lease.held_by("the camera preview"):
            self.assertEqual(self.lease.holder, "the camera preview")
        self.assertTrue(self.lease.free)

        with self.assertRaises(ValueError):
            with self.lease.held_by("the camera preview"):
                raise ValueError("the capture blew up")
        self.assertTrue(self.lease.free, "an exception kept the sensor")

    def test_the_context_manager_refuses_rather_than_running_without_it(self):
        held = self.lease.acquire("the live stream")
        with self.assertRaises(SensorBusy) as caught:
            with self.lease.held_by("the camera preview", wait=0.01):
                self.fail("the body ran without the sensor")
        self.assertEqual(caught.exception.holder, "the live stream")
        self.assertIn("the live stream", str(caught.exception))
        self.lease.release(held)

    def test_contention_is_counted_for_telemetry(self):
        held = self.lease.acquire("the live stream")
        self.lease.acquire("the camera preview")
        state = self.lease.state()
        self.assertEqual(state["holder"], "the live stream")
        self.assertEqual(state["refusals"], 1)
        self.assertEqual(state["grants"], 1)
        self.lease.release(held)

    def test_it_is_actually_exclusive_under_threads(self):
        """Not a formality: the whole file is worthless if this is not true."""
        overlaps = []
        inside = [0]
        guard = threading.Lock()

        def worker():
            for _ in range(40):
                token = self.lease.acquire("worker", wait=DEFAULT_WAIT_S)
                if token is None:            # pragma: no cover - would be a bug
                    overlaps.append("refused under contention")
                    return
                with guard:
                    inside[0] += 1
                    if inside[0] != 1:
                        overlaps.append("two holders at once")
                time.sleep(0.0005)
                with guard:
                    inside[0] -= 1
                self.lease.release(token)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(overlaps, [])
        self.assertTrue(self.lease.free)


class EncoderProcessLifecycleTests(unittest.TestCase):
    """The one camera hold in this codebase that survives a service restart.

    `rpicam-vid --timeout 0` holds the sensor until something kills it. The
    pump respawns it itself after a lost acquisition race, and there was a
    window between the retry wait returning and the `Popen` that followed:
    a `stop()` landing inside it terminated the *old* process, nulled the
    attribute, joined a thread already on its way out — and the pump then
    created a new encoder that nothing referenced and nothing would ever
    reap. Orphaned, it is reparented to init, and it is then outside the
    service's control group: restarting the station does not clear it, which
    is exactly the symptom that was reported and that an in-process fix could
    never have explained.
    """

    def encoder(self):
        from gsu.camera.h264 import HardwareEncoder, StreamSettings

        source = HardwareEncoder(StreamSettings(width=320, height=240, fps=10))
        source.tool = "rpicam-vid"
        return source

    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.stdout = None
            self.stderr = None

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.terminated = True
            self.alive = False

        def kill(self):                      # pragma: no cover - never reached
            self.alive = False

        def wait(self, timeout=None):
            return 0

    def patched(self, spawned: list):
        from unittest import mock

        def popen(*args, **kwargs):
            process = self.FakeProcess()
            spawned.append(process)
            return process

        return mock.patch("gsu.camera.h264.subprocess.Popen", popen)

    def test_nothing_can_be_spawned_once_stop_has_been_called(self):
        spawned: list = []
        source = self.encoder()
        with self.patched(spawned):
            source._stop.clear()
            self.assertTrue(source._spawn())
            source.stop()
            self.assertTrue(spawned[0].terminated, "the process was not reaped")
            self.assertFalse(source._spawn(),
                             "a process was spawned after the session stopped")
        self.assertEqual(len(spawned), 1)
        self.assertIsNone(source._process)

    def test_a_respawn_that_races_stop_creates_nothing(self):
        """The window itself, driven rather than raced.

        Timing this by sleeping and hoping proved worthless — the interleaving
        that matters is narrow enough that a stress loop passes against the
        broken code. So it is held open deliberately: the pump is parked at the
        exact instant after its retry wait returned and before its `Popen`,
        `stop()` is allowed to complete, and only then is the pump released.
        That is the sequence that orphaned an `rpicam-vid` on the hardware.
        """
        spawned: list = []
        source = self.encoder()
        at_the_gate = threading.Event()
        stop_finished = threading.Event()
        outcome: list = []

        with self.patched(spawned):
            source._stop.clear()
            self.assertTrue(source._spawn())      # the one stop() will reap

            def pump():
                at_the_gate.set()
                stop_finished.wait(5.0)
                outcome.append(source._spawn())

            thread = threading.Thread(target=pump)
            thread.start()
            self.assertTrue(at_the_gate.wait(5.0))
            source.stop()
            stop_finished.set()
            thread.join(timeout=5.0)

        self.assertEqual(outcome, [False], "a respawn slipped past stop()")
        self.assertEqual(len(spawned), 1,
                         "an orphaned rpicam-vid was created after stop()")
        self.assertTrue(spawned[0].terminated)
        self.assertIsNone(source._process)


class CodecSniffTests(unittest.TestCase):
    """Reading the codec off the bytes, which is what makes a stale probe
    survivable rather than silent."""

    def test_it_recognises_each_codecs_parameter_sets(self):
        self.assertEqual(sniff_codec([b"\x67\x64\x00\x33"]), "h264")   # SPS
        self.assertEqual(sniff_codec([b"\x68\xee\x3c\x80"]), "h264")   # PPS
        self.assertEqual(sniff_codec([b"\x40\x01\x0c\x01"]), "hevc")   # VPS
        self.assertEqual(sniff_codec([b"\x42\x01\x01\x01"]), "hevc")   # SPS
        self.assertEqual(sniff_codec([b"\x44\x01\xc1\x72"]), "hevc")   # PPS

    def test_an_h264_slice_is_never_mistaken_for_hevc(self):
        """The bug the first version of this function actually had.

        H.264's non-IDR slice header is 0x41 and its IDR is 0x45. Read with
        HEVC's two-byte rule those are types 32 and 34 — VPS and PPS — so a
        sniffer that only shifts and masks declares every H.264 stream to be
        H.265 on its second frame, and then stops it as a codec mismatch. The
        suite caught this before the hardware did.
        """
        for byte in (0x41, 0x45, 0x21, 0x25, 0x01, 0x65, 0x61):
            self.assertIsNone(
                sniff_codec([bytes([byte, 0x9A, 0x00])]),
                f"0x{byte:02x} was classified from a slice header",
            )

    def test_it_says_nothing_rather_than_guessing(self):
        self.assertIsNone(sniff_codec([]))
        self.assertIsNone(sniff_codec([b""]))
        self.assertIsNone(sniff_codec([b"\x80\x00"]), "forbidden_zero_bit set")
        # An HEVC slice: a real NAL, and still not something to conclude from.
        self.assertIsNone(sniff_codec([b"\x26\x01\xaf"]))

    def test_a_real_synthetic_stream_reads_as_h264_at_every_keyframe(self):
        from gsu.camera.h264 import StreamSettings
        from gsu.camera.h264_synthetic import SyntheticH264Source

        source = SyntheticH264Source(StreamSettings(width=320, height=240, fps=10))
        answers = [sniff_codec(split_annexb(source.frame().data)) for _ in range(6)]
        self.assertEqual(answers[0], "h264", "the keyframe carried no verdict")
        self.assertEqual(set(answers) - {None}, {"h264"},
                         "a frame was classified as something other than H.264")
