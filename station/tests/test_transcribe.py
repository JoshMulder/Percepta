"""The offline airband transcriber.

The whisper.cpp subprocess is always mocked: these tests are about the queue,
the availability gating, the output cleaning and the drop-oldest behaviour that
keeps transcription from ever competing with the audio path — not about the
model, which only a real Pi can exercise.
"""

import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gsu.radio import transcribe
from gsu.radio.transcribe import QUEUE_MAX, Transcriber

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
            import tempfile

            with tempfile.TemporaryDirectory() as directory:
                out = transcribe.run_whisper(
                    "x", "m", Path(directory) / "over.wav"
                )
        self.assertEqual(out, "")


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

    def test_off_when_disabled_even_if_everything_is_present(self):
        with mock.patch.object(transcribe.shutil, "which", return_value="/usr/bin/x"):
            t = Transcriber(lambda *a: None, enabled=False, model=__file__)
        self.assertFalse(t.available)

    def test_submit_is_a_no_op_when_unavailable(self):
        t = Transcriber(lambda *a: None, enabled=False)
        t.submit(_OVER, _RATE, 118_700_000, datetime.now(UTC))
        self.assertEqual(t._queue.qsize(), 0)


class QueueTests(unittest.TestCase):
    def _available(self, on_text):
        # Availability is a hardware question; force it so the queue logic can be
        # tested without a binary or a model on the machine running the suite.
        t = Transcriber(on_text, enabled=False)
        t.available = True
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
