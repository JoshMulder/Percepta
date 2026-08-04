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

    def _transcribe(self, over: _Over) -> str:
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "over.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(over.rate)
                handle.writeframes(over.pcm)
            return run_whisper(self._binary, self._model or "", wav_path)

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None


def run_whisper(binary: str, model: str, wav_path: Path) -> str:
    """Run whisper.cpp over a WAV and return the transcript, or empty on any
    failure — a missing binary, a non-zero exit, a timeout. Run through `nice`
    where it exists so it never competes with the audio path."""
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
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TRANSCRIBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("whisper.cpp did not run: %s", exc)
        return ""
    try:
        text = output.with_suffix(".txt").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""
    return clean_transcript(text)


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
