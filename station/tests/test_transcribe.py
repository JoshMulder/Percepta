"""The offline airband transcriber.

The whisper.cpp subprocess is always mocked: these tests are about the queue,
the availability gating, the output cleaning and the drop-oldest behaviour that
keeps transcription from ever competing with the audio path — not about the
model, which only a real Pi can exercise.
"""

import array
import tempfile
import time
import unittest
import wave
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gsu.radio import transcribe
from gsu.radio.transcribe import QUEUE_MAX, WHISPER_RATE, Transcriber

# One second of (silent) 16-bit mono at 8 kHz — past MIN_OVER_SECONDS.
_OVER = b"\x01\x00" * 8000
_RATE = 8000


class CleanTests(unittest.TestCase):
    def test_strips_bracketed_non_speech_tags(self):
        text = "[BLANK_AUDIO]\nCleared to land runway two seven\n(wind)\n"
        self.assertEqual(
            transcribe.clean_transcript(text), "Cleared to land runway two seven"
        )

    def test_an_over_with_no_speech_is_empty_not_a_tag(self):
        self.assertEqual(transcribe.clean_transcript("[BLANK_AUDIO]\n"), "")
        self.assertEqual(transcribe.clean_transcript("   \n\n"), "")


class ResampleTests(unittest.TestCase):
    def test_24k_becomes_16k(self):
        # One second at 24 kHz must come back as one second at 16 kHz — the rate
        # whisper.cpp will accept, and the whole reason transcription was silent.
        pcm = b"\x01\x00" * 24_000
        out = transcribe.resample_to_whisper(pcm, 24_000)
        self.assertEqual(len(out) // 2, 16_000)

    def test_16k_is_passed_straight_through(self):
        pcm = b"\x02\x00" * 16_000
        self.assertIs(transcribe.resample_to_whisper(pcm, 16_000), pcm)

    def test_a_ramp_keeps_its_shape(self):
        # A rising ramp resampled must still rise monotonically — a sign the
        # interpolation is not scrambling the samples.
        ramp = array.array("h", [i - 1000 for i in range(2000)])
        out = array.array("h")
        out.frombytes(transcribe.resample_to_whisper(ramp.tobytes(), 24_000))
        self.assertTrue(all(out[i] <= out[i + 1] for i in range(len(out) - 1)))


class WhisperInputRateTests(unittest.TestCase):
    def test_the_wav_handed_to_whisper_is_16k(self):
        # The end-to-end guard: whatever the over's rate, the WAV the CLI is
        # given is 16 kHz. Before this the WAV was 24 kHz and whisper wrote
        # nothing, so every transcript was empty.
        seen = {}

        def fake(command, **_):
            wav = command[command.index("-f") + 1]
            with wave.open(wav, "rb") as handle:
                seen["rate"] = handle.getframerate()
            out = command[command.index("-of") + 1]
            Path(out + ".txt").write_text("cleared to land\n")
            return mock.Mock(returncode=0)

        got = []
        with mock.patch.object(transcribe.subprocess, "run", side_effect=fake), \
                mock.patch.object(transcribe.shutil, "which", return_value=None):
            t = Transcriber(lambda *a: got.append(a), enabled=True)
            t.installed = True
            t.start()
            t.submit(b"\x01\x00" * 24_000, 24_000, 118_700_000, datetime.now(UTC))
            deadline = time.time() + 3.0
            while not got and time.time() < deadline:
                time.sleep(0.02)
            t.shutdown()
        self.assertEqual(seen.get("rate"), WHISPER_RATE)


class RunWhisperTests(unittest.TestCase):
    def test_reads_the_transcript_whisper_wrote(self):
        with mock.patch.object(transcribe.subprocess, "run") as run:
            def fake(command, **_):
                out = command[command.index("-of") + 1]
                Path(out + ".txt").write_text("Cleared to land\n[BLANK_AUDIO]\n")
                return mock.Mock(returncode=0)

            run.side_effect = fake
            import tempfile

            with tempfile.TemporaryDirectory() as directory:
                wav = Path(directory) / "over.wav"
                wav.write_bytes(b"")
                text = transcribe.run_whisper("whisper-cli", "model.bin", wav)
        self.assertEqual(text, "Cleared to land")

    def test_a_binary_that_will_not_run_is_empty_not_an_error(self):
        with mock.patch.object(
            transcribe.subprocess, "run", side_effect=OSError("no such binary")
        ):
            with tempfile.TemporaryDirectory() as directory:
                out = transcribe.run_whisper(
                    "x", "m", Path(directory) / "over.wav"
                )
        self.assertEqual(out, "")


class WhisperFailureTests(unittest.TestCase):
    """The failures that used to be swallowed. A silent whisper.cpp was
    indistinguishable from a quiet channel, which is why transcription 'not
    working' had no thread to pull; now every failure names itself."""

    def _run(self, run_result=None, side_effect=None, write=None):
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "over.wav"
            wav.write_bytes(b"")

            def fake(command, **_):
                if write is not None:
                    out = command[command.index("-of") + 1]
                    Path(out + ".txt").write_text(write)
                return run_result

            with mock.patch.object(
                transcribe.subprocess, "run",
                side_effect=side_effect or fake,
            ):
                return transcribe.whisper_transcribe("whisper-cli", "m", wav)

    def test_a_nonzero_exit_surfaces_whispers_own_stderr(self):
        text, error = self._run(
            run_result=mock.Mock(returncode=1, stderr=b"error: failed to load model")
        )
        self.assertEqual(text, "")
        self.assertIn("exited 1", error)
        self.assertIn("failed to load model", error)

    def test_a_clean_exit_that_wrote_nothing_is_flagged(self):
        text, error = self._run(run_result=mock.Mock(returncode=0, stderr=b""))
        self.assertEqual(text, "")
        self.assertIn("wrote no", error)

    def test_success_returns_text_and_no_error(self):
        text, error = self._run(
            run_result=mock.Mock(returncode=0, stderr=b""), write="roger that\n"
        )
        self.assertEqual(text, "roger that")
        self.assertEqual(error, "")


class SelfTestTests(unittest.TestCase):
    def test_it_logs_a_broken_whisper_at_start_up(self):
        # A model that exists (this file) so the self-test runs, and a subprocess
        # that fails — the box must say so at start-up, not transcribe nothing in
        # silence until someone keys up a radio to find out.
        t = Transcriber(lambda *a: None, enabled=True, model=__file__)
        t.installed = True
        with mock.patch.object(
            transcribe.subprocess, "run",
            return_value=mock.Mock(returncode=1, stderr=b"error: bad model"),
        ):
            with self.assertLogs("gsu.radio", level="WARNING") as logs:
                t._self_test()
        self.assertTrue(any("self-test FAILED" in line for line in logs.output))

    def test_it_is_skipped_when_there_is_no_model(self):
        # No model to test against — start() already said unavailable, so the
        # self-test must not run whisper (nor log a failure) here.
        t = Transcriber(lambda *a: None, enabled=True, model=None)
        t.installed = True
        with mock.patch.object(transcribe.subprocess, "run") as run:
            t._self_test()
        run.assert_not_called()


class AvailabilityTests(unittest.TestCase):
    def test_off_without_a_model(self):
        t = Transcriber(lambda *a: None, enabled=True, model=None)
        self.assertFalse(t.available)

    def test_off_when_the_binary_is_not_on_path(self):
        # The model exists (this file), but the binary does not.
        t = Transcriber(
            lambda *a: None,
            enabled=True,
            binary="gsu-not-a-real-binary-zzz",
            model=__file__,
        )
        self.assertFalse(t.available)

    def test_installed_but_switched_off_is_unavailable(self):
        # The setup-page switch is `enabled`; the tools being present is
        # `installed`. Available only when both hold.
        with mock.patch.object(transcribe.shutil, "which", return_value="/usr/bin/x"):
            t = Transcriber(lambda *a: None, enabled=False, model=__file__)
        self.assertTrue(t.installed)
        self.assertFalse(t.enabled)
        self.assertFalse(t.available)

    def test_the_install_reason_names_what_is_missing(self):
        t = Transcriber(lambda *a: None, enabled=True, model=None)
        self.assertFalse(t.installed)
        self.assertIn("model", t.install_reason)

    def test_submit_is_a_no_op_when_unavailable(self):
        t = Transcriber(lambda *a: None, enabled=False)
        t.submit(_OVER, _RATE, 118_700_000, datetime.now(UTC))
        self.assertEqual(t._queue.qsize(), 0)


class QueueTests(unittest.TestCase):
    def _available(self, on_text):
        # `installed` is a hardware question; force it so the queue logic can be
        # tested without a binary or a model on the machine running the suite.
        # `available` is then a property of installed AND the live switch.
        t = Transcriber(on_text, enabled=True)
        t.installed = True
        return t

    def test_a_full_queue_drops_the_oldest_rather_than_blocking(self):
        t = self._available(lambda *a: None)  # never started, so nothing drains
        for _ in range(QUEUE_MAX + 5):
            t.submit(_OVER, _RATE, 118_700_000, datetime.now(UTC))
        self.assertEqual(t._queue.qsize(), QUEUE_MAX)

    def test_too_short_an_over_is_not_worth_the_cpu(self):
        t = self._available(lambda *a: None)
        t.submit(b"\x00\x00" * 10, _RATE, 118_700_000, datetime.now(UTC))
        self.assertEqual(t._queue.qsize(), 0)

    def test_a_submitted_over_becomes_a_transcript(self):
        got = []
        t = self._available(
            lambda freq, started, dur, text: got.append((freq, text))
        )
        with mock.patch.object(
            transcribe, "run_whisper", return_value="cleared to land"
        ):
            t.start()
            t.submit(_OVER, _RATE, 121_500_000, datetime.now(UTC))
            deadline = time.time() + 3.0
            while not got and time.time() < deadline:
                time.sleep(0.02)
            t.shutdown()
        self.assertEqual(got, [(121_500_000, "cleared to land")])


if __name__ == "__main__":
    unittest.main()
