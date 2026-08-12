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
import math
import queue
import shutil
import subprocess
import tempfile
import threading
import time
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

#: The initial prompt handed to whisper, biasing it toward what airband actually
#: carries: ICAO phraseology, the phonetic alphabet, and clipped strings of
#: numbers and callsigns. whisper conditions on this as though it were speech
#: already heard, so the right words come out ahead of their near-homophones when
#: the audio is marginal — which, on a noisy AM channel, is most of the time. It
#: does not teach the model new words; it weights the ones it already knows.
#:
#: Overridable per site (GSU_WHISPER_PROMPT) to add local aerodrome names and
#: based-aircraft registrations, which no general model will otherwise get right.
#: Kept short on purpose: whisper only reads the last ~224 tokens of it.
AVIATION_PROMPT = (
    "Air traffic control radio, ICAO phraseology and the phonetic alphabet: "
    "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliett Kilo Lima "
    "Mike November Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey "
    "Xray Yankee Zulu. Cleared for takeoff, cleared to land, line up and wait, "
    "hold short, taxi to holding point, runway, wind, QNH, altimeter, squawk, "
    "flight level, climb, descend, maintain, heading, contact tower, roger, "
    "wilco, affirm, negative, standby, traffic, final, base leg, downwind."
)

#: How the models rank by capability, parsed from the ggml filename
#: (ggml-small.en.bin -> "small" -> 3). Larger is more accurate and slower; the
#: selector prefers the largest that runs inside the budget below.
_MODEL_RANK = {"tiny": 1, "base": 2, "small": 3, "medium": 4, "large": 5}

#: The longest a single decode window may take, on this box, for a model to be
#: kept. whisper works in 30 s windows and an over is almost always one, so this
#: bounds how far behind a busy channel can put transcription: at the budget the
#: model runs a full window in real time, with headroom for the niced scheduling.
#: A board too slow for even the smallest installed model turns transcription off.
#: A first cut — the honest number is a bench on the actual board, and the
#: selector logs what it measured either way.
MAX_WINDOW_PROCESS_S = 25.0


def _model_rank(model_path: str) -> int:
    """Capability rank from a ggml model filename; 0 if unrecognised."""
    stem = Path(model_path).name.removeprefix("ggml-")
    key = stem.split(".")[0].split("-")[0]  # small.en -> small; large-v3 -> large
    return _MODEL_RANK.get(key, 0)


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
        prompt: str = "",
        enabled: bool = False,
    ) -> None:
        self._on_text = on_text
        self._binary = binary
        self._model = model
        #: The initial prompt biasing whisper toward airband vocabulary. A
        #: configured one wins; otherwise the built-in aviation phraseology,
        #: because a station with transcription on is one listening to aircraft.
        self._prompt = prompt or AVIATION_PROMPT
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
        #: Set false when start-up benchmarking finds no installed model runs
        #: fast enough on this board — see `_select_model`. Optimistic until then.
        self._capable = True

    @property
    def available(self) -> bool:
        """Whether an over should actually be transcribed right now: the tools
        are installed, the operator has it switched on, and this board was found
        fast enough to keep up (see `_select_model`)."""
        return self.installed and self.enabled and self._capable

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
        self._select_model()
        if not self._capable:
            return  # nothing installed runs fast enough; _select_model said so
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

    def _select_model(self) -> None:
        """Pick the largest installed model this board can keep up with — or turn
        transcription off. Runs once at start-up, in the worker thread.

        `_probe` only checks the files are present; it cannot know whether this
        particular board can run them in time. A Pi 5 keeps up with `small.en`; a
        Pi 2B falls hopelessly behind it and belongs on `base.en` or `tiny.en`,
        or off. So this benchmarks the candidates — every ggml model the image
        baked, largest first, capped at the configured one — by decoding a window
        on each, and keeps the first inside `MAX_WINDOW_PROCESS_S`. If even the
        smallest is too slow, transcription turns itself off with one clear log
        line rather than silently falling further and further behind. It also
        subsumes the old start-up self-test: a model that benchmarks has run.
        """
        candidates = self._candidate_models()
        if not candidates:
            return  # no model at all — start() already reported it
        for model in candidates:
            name = Path(model).name
            elapsed, error = self._benchmark(model)
            if error:
                log.warning("Airband transcription: %s did not run (%s); trying "
                            "a smaller model.", name, error)
                continue
            if elapsed is not None and elapsed <= MAX_WINDOW_PROCESS_S:
                self._model = model
                log.info("Airband transcription: using %s — a decode window took "
                         "%.1fs here, inside the %.0fs budget.",
                         name, elapsed, MAX_WINDOW_PROCESS_S)
                return
            log.warning("Airband transcription: %s is too slow on this board "
                        "(%.1fs for a window, budget %.0fs); trying a smaller "
                        "model.", name, elapsed or 0.0, MAX_WINDOW_PROCESS_S)
        self._capable = False
        log.error("Airband transcription off: no installed model runs fast enough "
                  "on this board. Overs are still recorded, just not transcribed.")

    def _candidate_models(self) -> list[str]:
        """The models to weigh: the configured one first, then smaller fallbacks.

        The configured model is the operator's explicit choice, so it is always
        the FIRST candidate — tried before anything else, whatever its name. That
        matters for a custom fine-tune (a domain-adapted ATC model, say) whose
        filename does not rank: the old logic sorted it purely by rank, so a
        rank-0 custom model sitting beside the generic `ggml-small.en.bin` the
        image ships was buried behind it and never ran, which is the whole reason
        to configure one. Preferring the configured model directly fixes that.

        After it come the other baked `ggml-*.bin` — the ladder — largest first,
        but only those strictly SMALLER than the configured model, as boards too
        slow for the primary step down to. GSU_WHISPER_MODEL stays the ceiling:
        the selector steps down from it, never up past it. A custom (rank-0)
        primary has no rank to compare against, so it is treated as the top of
        the ladder — every generic model beside it is a smaller step down.
        """
        if not self._model:
            return []
        cap = _model_rank(self._model)
        found: list[tuple[int, str]] = []
        for path in Path(self._model).parent.glob("ggml-*.bin"):
            path = str(path)
            if path == self._model:
                continue  # the configured model is prepended below, not ranked in
            rank = _model_rank(path)
            # A smaller generic model to fall back to. When the configured model
            # is custom (cap == 0) it has no rank to compare, so every ranked
            # model beside it counts as a step down.
            if rank and (cap == 0 or rank < cap):
                found.append((rank, path))
        found.sort(key=lambda ranked: ranked[0], reverse=True)
        return [self._model] + [path for _, path in found]

    def _benchmark(self, model: str) -> tuple[float | None, str]:
        """Decode one second of silence and time it: (elapsed_s, error).

        A second of silence is padded to a full 30 s window by whisper, so this
        measures one window's decode — the cost of a typical over — without
        needing anyone to key up a radio. Returns the error whisper gave instead
        of a time on any failure, so `_select_model` can step past a broken model.
        """
        try:
            with tempfile.TemporaryDirectory() as directory:
                wav_path = Path(directory) / "bench.wav"
                with wave.open(str(wav_path), "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(WHISPER_RATE)
                    handle.writeframes(b"\x00\x00" * WHISPER_RATE)  # 1 s of silence
                start = time.monotonic()
                _, error = whisper_transcribe(
                    self._binary, model, wav_path, self._prompt)
        except Exception as exc:  # noqa: BLE001 - benchmarking must never crash the worker
            return None, str(exc)
        return (None, error) if error else (time.monotonic() - start, "")

    def _transcribe(self, over: _Over) -> str:
        # Band-pass the transcription copy to the voice band, always and
        # independently of the receiver's own voice filter: the audio going up
        # the link and to the recording is whatever the operator set, but whisper
        # always wants speech with the carrier rumble and the out-of-band hiss
        # taken off.
        pcm = bandpass_voice(resample_to_whisper(over.pcm, over.rate))
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "over.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(WHISPER_RATE)
                handle.writeframes(pcm)
            return run_whisper(self._binary, self._model or "", wav_path, self._prompt)

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


def _biquad(samples: list[float], b0: float, b1: float, b2: float,
            a1: float, a2: float) -> list[float]:
    """One Direct-Form-I biquad section over a float sample list."""
    out = [0.0] * len(samples)
    x1 = x2 = y1 = y2 = 0.0
    for i, x in enumerate(samples):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, x
        y2, y1 = y1, y
        out[i] = y
    return out


def bandpass_voice(
    pcm: bytes, rate: int = WHISPER_RATE, low_hz: float = 300.0, high_hz: float = 5000.0
) -> bytes:
    """The speech band (~300-5000 Hz) of 16-bit mono PCM, in stdlib.

    A transcription-only stage: it runs on the copy handed to whisper, never on
    the audio the listeners and the recording get, so a site can listen full-band
    while whisper still sees speech with the carrier rumble below and the worst of
    the channel hiss above taken off. Two 2nd-order Butterworth sections (high-pass
    then low-pass, RBJ cookbook): 12 dB/octave skirts for a few multiplies a
    sample, on an over the model is about to spend seconds on. No numpy, keeping
    this file's no-heavy-deps promise.

    The ceiling is 5 kHz, not the 3.4 kHz of a comms-voice channel. 3.4 kHz is the
    right cut for a human ear on a narrow AM channel, but it throws away the
    consonant/fricative energy (s, f, t, sh live at 4-8 kHz) that whisper leans on
    to tell the phonetic alphabet and digit strings apart — measured on real overs,
    dropping the ceiling to 3.4 kHz turned "New Zealand 647" into "take 4-7". 5 kHz
    recovers those cues while still cutting the noisy top octave, where opening up
    fully let whisper hallucinate on hiss. Wideband whisper was trained on natural
    speech, so the lighter the band-limiting the better, up to that noise limit.
    """
    if not pcm:
        return pcm
    src = array.array("h")
    src.frombytes(pcm)
    if len(src) < 4:
        return pcm
    high_hz = min(high_hz, rate / 2.0 * 0.99)

    def _section(data: list[float], f0: float, highpass: bool) -> list[float]:
        w0 = 2.0 * math.pi * f0 / rate
        cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
        alpha = sin_w0 / (2.0 * 0.7071067811865476)  # Butterworth Q = 1/sqrt(2)
        a0 = 1.0 + alpha
        if highpass:
            b0, b1, b2 = (1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2
        else:
            b0, b1, b2 = (1 - cos_w0) / 2, 1 - cos_w0, (1 - cos_w0) / 2
        return _biquad(data, b0 / a0, b1 / a0, b2 / a0,
                       (-2 * cos_w0) / a0, (1 - alpha) / a0)

    xs = _section([float(v) for v in src], low_hz, True)
    xs = _section(xs, high_hz, False)
    out = array.array("h", bytes(2 * len(xs)))
    for i, value in enumerate(xs):
        out[i] = max(-32768, min(32767, int(value)))
    return out.tobytes()


def run_whisper(binary: str, model: str, wav_path: Path, prompt: str = "") -> str:
    """The transcript for a WAV, or empty on any failure. Logs *why* on a
    failure — that this used to swallow was the reason a broken model or a wrong
    flag looked exactly like a quiet channel from the outside."""
    text, error = whisper_transcribe(binary, model, wav_path, prompt)
    if error:
        log.warning("Airband transcription failed: %s", error)
    return text


def whisper_transcribe(
    binary: str, model: str, wav_path: Path, prompt: str = ""
) -> tuple[str, str]:
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
    if prompt:
        # whisper's initial prompt: it conditions on this as though it were text
        # already spoken, so the vocabulary in it wins over near-homophones when
        # the audio is marginal. One argument — subprocess takes a list, no shell.
        command += ["--prompt", prompt]
    if shutil.which("nice"):
        command = ["nice", "-n", str(NICE)] + command
    try:
        # Both streams captured: whisper.cpp prints its diagnostics to whichever
        # it prints them to, and a build that puts the reason for a non-zero exit
        # on stdout (not stderr) is exactly how "exited 1: no output" happened.
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TRANSCRIBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{binary!r} did not run: {exc}"
    if result.returncode != 0:
        streams = b" ".join(
            s for s in (result.stderr, result.stdout) if isinstance(s, (bytes, bytearray))
        ).decode("utf-8", "replace")
        tail = " ".join(streams.split()[-60:])
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
