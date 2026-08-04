"""Notate airband transmissions into the event log, on the box, offline.

whisper.cpp as a subprocess — a native binary, not a Python dependency, so the
station's no-heavy-deps guarantee holds. Off by default (see `AgentConfig`).

**Live audio always wins.** This reads the audio of an over that has *already*
been captured and published, so it never touches the buffers the stream and the
listeners use. The subprocess is run through `nice`, so the kernel preempts it
the instant the audio sub-tick or the encoder wants the CPU. And the queue of
pending overs is bounded: if a busy channel outruns the board, the oldest overs
are dropped — a gap in the notes, never a stall in the audio.

Rough on purpose. Airband is hard for a general model — squelch noise, accents,
the phonetic alphabet, strings of numbers, callsigns — so the transcript is a
searchable gist of what was said, not a record of it.
"""

from __future__ import annotations

import array
import logging
import queue
import shutil
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("gsu.radio")

#: whisper.cpp accepts one sample rate — 16 kHz mono — and rejects a WAV at any
#: other rate outright, writing no transcript. The receiver's audio is 24 kHz,
#: so an over is resampled to this before it is handed over.
WHISPER_RATE = 16000

#: How many un-transcribed overs to hold before dropping the oldest. Small on
#: purpose: the queue is there to ride out a burst, not to build a backlog a
#: slow board will never clear, and a stale transcript is worth less than a
#: fresh one.
QUEUE_MAX = 8

#: `nice` increment for the subprocess — below the audio threads without
#: starving on an otherwise-idle box.
NICE = 10

#: Shorter than this is a squelch tail or a click, not speech worth the CPU.
MIN_OVER_SECONDS = 0.6

#: A single over should not take longer than this to transcribe; past it the
#: board is wedged and the process is killed rather than blocking the worker.
TRANSCRIBE_TIMEOUT_S = 120

#: `on_text(freq_hz, started_at, duration_s, text)`.
OnText = Callable[[int, datetime, float, str], None]


@dataclass
class _Over:
    pcm: bytes
    rate: int
    freq_hz: int
    started_at: datetime


class Transcriber:
    """A background worker that turns captured overs into transcript events."""

    def __init__(
        self,
        on_text: OnText,
        *,
        binary: str = "whisper-cli",
        model: str | None = None,
        enabled: bool = False,
    ) -> None:
        self._on_text = on_text
        self._binary = binary
        self._model = model
        self._queue: "queue.Queue[_Over]" = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: Whether the binary and model are present — fixed at construction, a
        #: deployment fact. Separate from `enabled`, which is the operator's live
        #: switch, so the setup page can show "not installed" distinctly from
        #: "switched off".
        self.installed, self.install_reason = self._probe()
        #: The live switch, from the site config (or the env override). Set by
        #: the agent each tick so the setup-page toggle takes effect at once.
        self.enabled = bool(enabled)

    @property
    def available(self) -> bool:
        """Whether an over should actually be transcribed right now: the tools
        are installed and the operator has it switched on."""
        return self.installed and self.enabled

    def _probe(self) -> tuple[bool, str]:
        """Are the binary and model both present? A reason if not, for the setup
        page to show — this does not run the model, only look for it."""
        if not self._model:
            return False, "no model file is configured (GSU_WHISPER_MODEL)"
        if shutil.which(self._binary) is None:
            return False, f"the {self._binary!r} binary is not on the station's PATH"
        if not Path(self._model).exists():
            return False, f"the model file {self._model} is not present"
        return True, ""

    def start(self) -> None:
        # Started whenever the tools are installed, not only when switched on:
        # the worker idles on an empty queue, so toggling on later needs no
        # restart. It logs its state once here.
        if not self.installed:
            log.info("Airband transcription unavailable: %s.", self.install_reason)
            return
        if self._thread is not None:
            return
        log.info("Airband transcription ready: %s with %s.",
                 self._binary, self._model)
        self._thread = threading.Thread(
            target=self._run, name="transcribe", daemon=True
        )
        self._thread.start()

    def submit(
        self, pcm: bytes, rate: int, freq_hz: int, started_at: datetime
    ) -> None:
        """Hand a completed over over for transcription. Never blocks: a full
        queue drops its oldest entry rather than making the caller wait, because
        the caller is the sensing loop and it must not."""
        if not self.available or not pcm:
            return
        if len(pcm) < int(MIN_OVER_SECONDS * rate) * 2:  # 16-bit mono
            return
        over = _Over(pcm=pcm, rate=rate, freq_hz=freq_hz, started_at=started_at)
        try:
            self._queue.put_nowait(over)
        except queue.Full:
            try:
                self._queue.get_nowait()
                log.warning("Transcription is behind; dropped an older over.")
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(over)
            except queue.Full:
                pass

    def _run(self) -> None:
        self._self_test()
        while not self._stop.is_set():
            try:
                over = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                text = self._transcribe(over)
            except Exception:  # noqa: BLE001 - one bad over must not end the worker
                log.exception("Transcription of an over failed; continuing.")
                continue
            if not text:
                continue
            duration = len(over.pcm) / 2 / over.rate
            try:
                self._on_text(over.freq_hz, over.started_at, duration, text)
            except Exception:  # noqa: BLE001
                log.exception("Recording a transcript failed; continuing.")

    def _self_test(self) -> None:
        """Prove the pipeline works before a transmission has to.

        On a remote box nobody can key up a radio to check transcription, and
        `_probe` only looks for the files — not that whisper.cpp actually runs
        against them. So the worker runs it once on a second of silence at
        start-up and logs the result: a broken binary, a model it cannot load or
        a flag this build does not take becomes one clear log line rather than a
        channel that transcribes nothing for no visible reason. Skipped when
        there is no model to test against, which `start()` has already reported.
        """
        if not self._model or not Path(self._model).exists():
            return
        try:
            with tempfile.TemporaryDirectory() as directory:
                wav_path = Path(directory) / "selftest.wav"
                with wave.open(str(wav_path), "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(WHISPER_RATE)
                    handle.writeframes(b"\x00\x00" * WHISPER_RATE)  # 1 s of silence
                _, error = whisper_transcribe(self._binary, self._model, wav_path)
        except Exception as exc:  # noqa: BLE001 - a self-test must never crash the worker
            log.warning("Airband transcription self-test could not run: %s.", exc)
            return
        if error:
            log.warning(
                "Airband transcription self-test FAILED — %s. Transmissions will "
                "be recorded but not transcribed until this is fixed.", error)
        else:
            log.info("Airband transcription self-test passed; whisper.cpp is working.")

    def _transcribe(self, over: _Over) -> str:
        pcm = resample_to_whisper(over.pcm, over.rate)
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "over.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(WHISPER_RATE)
                handle.writeframes(pcm)
            return run_whisper(self._binary, self._model or "", wav_path)

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None


def resample_to_whisper(pcm: bytes, rate: int) -> bytes:
    """16-bit mono PCM at `rate` to the 16 kHz whisper.cpp demands.

    Linear interpolation, in stdlib rather than numpy so this file keeps its
    no-heavy-deps promise — the transcriber is a background, niced worker and a
    few thousand multiplies on an over it is about to spend seconds on a model
    for is nothing. Enough for a transcript that is only after the gist, and the
    voice content is well under the 8 kHz that 16 kHz carries — doubly so once
    the receiver's voice filter has been through it.
    """
    if rate == WHISPER_RATE or not pcm:
        return pcm
    src = array.array("h")
    src.frombytes(pcm)
    n_in = len(src)
    if n_in < 2:
        return pcm
    n_out = round(n_in * WHISPER_RATE / rate)
    if n_out < 2:
        return pcm
    step = (n_in - 1) / (n_out - 1)
    out = array.array("h", bytes(2 * n_out))
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j + 1 < n_in else a
        out[i] = max(-32768, min(32767, int(round(a + (b - a) * frac))))
    return out.tobytes()


def run_whisper(binary: str, model: str, wav_path: Path) -> str:
    """The transcript for a WAV, or empty on any failure. Logs *why* on a
    failure — that this used to swallow was the reason a broken model or a wrong
    flag looked exactly like a quiet channel from the outside."""
    text, error = whisper_transcribe(binary, model, wav_path)
    if error:
        log.warning("Airband transcription failed: %s", error)
    return text


def whisper_transcribe(binary: str, model: str, wav_path: Path) -> tuple[str, str]:
    """Run whisper.cpp over a WAV. Returns (transcript, error): the error is a
    human string on any failure — a missing binary, a non-zero exit carrying
    whisper's own stderr, a timeout, an output file that was never written — and
    empty on success. Run through `nice` where it exists so it never competes
    with the audio path."""
    output = wav_path.with_suffix("")  # whisper writes <output>.txt with -otxt
    command = [
        binary,
        "-m", str(model),
        "-f", str(wav_path),
        "-l", "en",
        "-nt",             # no timestamps — the event carries the time
        "-otxt",
        "-of", str(output),
    ]
    if shutil.which("nice"):
        command = ["nice", "-n", str(NICE)] + command
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=TRANSCRIBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{binary!r} did not run: {exc}"
    if result.returncode != 0:
        tail = " ".join(
            (result.stderr or b"").decode("utf-8", "replace").split()[-40:]
        )
        return "", f"{binary!r} exited {result.returncode}: {tail or 'no output'}"
    try:
        text = output.with_suffix(".txt").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return "", (
            f"{binary!r} exited 0 but wrote no {output.with_suffix('.txt').name} — "
            "check the flags against this whisper.cpp build"
        )
    return clean_transcript(text), ""


def clean_transcript(text: str) -> str:
    """whisper emits bracketed non-speech tags on noise — [BLANK_AUDIO],
    [Music], (wind) — and a line or two of whitespace on silence. Drop those so
    an over that carried no speech records nothing rather than a tag."""
    kept: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if (line.startswith("[") and line.endswith("]")) or (
            line.startswith("(") and line.endswith(")")
        ):
            continue
        kept.append(line)
    return " ".join(kept).strip()
