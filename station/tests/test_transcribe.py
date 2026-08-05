"""The offline airband transcriber.

The whisper.cpp subprocess is always mocked: these tests are about the queue,
the availability gating, the output cleaning and the drop-oldest behaviour that
keeps transcription from ever competing with the audio path — not about the
model, which only a real Pi can exercise.
"""

import array
import math
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


class BandpassTests(unittest.TestCase):
    """The transcription-only voice band-pass: it passes speech and cuts the
    rumble and out-of-band hiss whisper trips on, on the copy handed to the
    model — never the audio the listeners and the recording get."""

    def _tone(self, hz, rate=WHISPER_RATE, secs=0.3, amp=10000):
        n = int(rate * secs)
        s = array.array("h", [
            int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)
        ])
        return s.tobytes()

    def _rms(self, pcm):
        s = array.array("h")
        s.frombytes(pcm)
        return (sum(v * v for v in s) / len(s)) ** 0.5 if s else 0.0

    def test_passes_the_voice_band(self):
        raw = self._tone(1000)
        self.assertGreater(
            self._rms(transcribe.bandpass_voice(raw)), 0.6 * self._rms(raw))

    def test_cuts_the_hiss_above_the_band(self):
        raw = self._tone(6000)
        self.assertLess(
            self._rms(transcribe.bandpass_voice(raw)), 0.5 * self._rms(raw))

    def test_cuts_the_rumble_below_the_band(self):
        raw = self._tone(120)
        self.assertLess(
            self._rms(transcribe.bandpass_voice(raw)), 0.5 * self._rms(raw))

    def test_empty_pcm_is_unchanged(self):
        self.assertEqual(transcribe.bandpass_voice(b""), b"")


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


class PromptTests(unittest.TestCase):
    """The initial prompt is how airband vocabulary is fed to a general model."""

    def _command_for(self, prompt):
        seen = {}

        def fake(command, **_):
            seen["cmd"] = list(command)
            out = command[command.index("-of") + 1]
            Path(out + ".txt").write_text("roger\n")
            return mock.Mock(returncode=0, stderr=b"")

        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "over.wav"
            wav.write_bytes(b"")
            with mock.patch.object(transcribe.subprocess, "run", side_effect=fake), \
                    mock.patch.object(transcribe.shutil, "which", return_value=None):
                transcribe.whisper_transcribe("whisper-cli", "m", wav, prompt)
        return seen["cmd"]

    def test_a_prompt_is_passed_to_whisper(self):
        cmd = self._command_for("Aviation. Alpha Bravo Charlie.")
        self.assertIn("--prompt", cmd)
        self.assertEqual(cmd[cmd.index("--prompt") + 1], "Aviation. Alpha Bravo Charlie.")

    def test_no_prompt_flag_when_empty(self):
        self.assertNotIn("--prompt", self._command_for(""))

    def test_the_transcriber_defaults_to_the_aviation_vocabulary(self):
        # No prompt configured falls back to the built-in aviation phraseology,
        # which is what carries the phonetic alphabet and clearances a general
        # model would otherwise mishear against the channel noise.
        t = Transcriber(lambda *a: None)
        self.assertEqual(t._prompt, transcribe.AVIATION_PROMPT)
        self.assertIn("phonetic alphabet", t._prompt.lower())

    def test_a_configured_prompt_overrides_the_default(self):
        t = Transcriber(lambda *a: None, prompt="Only this.")
        self.assertEqual(t._prompt, "Only this.")


class ModelSelectionTests(unittest.TestCase):
    """Start-up benchmarking keeps the largest installed model this board can run
    inside the budget, capped at the configured one — or turns transcription off.
    _benchmark is stubbed so the selection logic is tested without a real model;
    the side_effect reads the model path as its last argument, so it works whether
    or not the patched method is called bound."""

    def _select(self, names, configured, benchmark):
        with tempfile.TemporaryDirectory() as directory:
            for name in names:
                (Path(directory) / name).write_bytes(b"x")
            t = Transcriber(lambda *a: None, enabled=True,
                            model=str(Path(directory) / configured))
            t.installed = True
            with mock.patch.object(Transcriber, "_benchmark", side_effect=benchmark):
                t._select_model()
            return t

    def test_picks_the_largest_model_within_budget(self):
        # small is too slow here, base fits — base wins.
        times = {"ggml-small.en.bin": (40.0, ""), "ggml-base.en.bin": (10.0, "")}
        t = self._select(["ggml-small.en.bin", "ggml-base.en.bin"],
                         "ggml-small.en.bin", lambda *a: times[Path(a[-1]).name])
        self.assertTrue(t._capable)
        self.assertEqual(Path(t._model).name, "ggml-base.en.bin")

    def test_caps_at_the_configured_model(self):
        # small is present and would fit, but base is configured — the ceiling.
        t = self._select(["ggml-small.en.bin", "ggml-base.en.bin"],
                         "ggml-base.en.bin", lambda *a: (5.0, ""))
        self.assertEqual(Path(t._model).name, "ggml-base.en.bin")

    def test_a_broken_model_steps_down_to_a_working_one(self):
        outcomes = {"ggml-small.en.bin": (None, "failed to load model"),
                    "ggml-base.en.bin": (8.0, "")}
        t = self._select(["ggml-small.en.bin", "ggml-base.en.bin"],
                         "ggml-small.en.bin", lambda *a: outcomes[Path(a[-1]).name])
        self.assertTrue(t._capable)
        self.assertEqual(Path(t._model).name, "ggml-base.en.bin")

    def test_turns_transcription_off_when_nothing_is_fast_enough(self):
        t = self._select(["ggml-base.en.bin"], "ggml-base.en.bin",
                         lambda *a: (999.0, ""))
        self.assertFalse(t._capable)
        self.assertFalse(t.available)

    def test_a_broken_model_with_no_fallback_turns_off_and_says_so(self):
        # Real _benchmark path (subprocess mocked): a model that will not run and
        # nothing smaller beside it turns transcription off with a clear line.
        t = Transcriber(lambda *a: None, enabled=True, model=__file__)
        t.installed = True
        with mock.patch.object(
            transcribe.subprocess, "run",
            return_value=mock.Mock(returncode=1, stderr=b"error: bad model"),
        ):
            with self.assertLogs("gsu.radio", level="WARNING") as logs:
                t._select_model()
        self.assertFalse(t._capable)
        self.assertTrue(any("transcription off" in line.lower() for line in logs.output))

    def test_no_model_is_skipped(self):
        # No model configured — start() already said unavailable, so selection
        # must not run whisper here.
        t = Transcriber(lambda *a: None, enabled=True, model=None)
        t.installed = True
        with mock.patch.object(transcribe.subprocess, "run") as run:
            t._select_model()
        run.assert_not_called()
        self.assertTrue(t._capable)


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
