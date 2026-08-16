"""Keep a few overs on disk so somebody can say what was actually said.

WHY THIS EXISTS. Every accuracy claim about airband transcription so far has
rested on reading the machine's own output and judging whether it looks like
aviation. That finds gross failures — a 1 s over transcribed as "Get on the
phone." is obviously wrong — and it cannot measure anything. It cannot say
whether a change helped, by how much, or whether it helped one frequency and
hurt another. There is no ground truth, because the audio is written to a
`TemporaryDirectory` and deleted the moment whisper returns.

So: an opt-in ring of recent overs, each WAV beside a JSON sidecar carrying what
the machine made of it. Somebody listens, types what was really said into the
sidecar, and there is a corpus. From there a word error rate is arithmetic
rather than an opinion, and every later change — a prompt, a decoder flag, a
different model — is a measurement instead of an argument.

OFF BY DEFAULT, AND BOUNDED IN TWO DIRECTIONS. This writes to the SD card of an
unattended box in a field. `GSU_OVER_CAPTURE` names a directory and turns it on;
nothing happens without it. The ring keeps `CAPTURE_MAX` overs and deletes the
oldest, so a station left with it on fills a known amount of card and then stops
growing — roughly 250 kB an over, so a couple of hundred megabytes at the
default. It is an instrument to switch on for a week, not a feature to leave
running.

THE AUDIO IS THE FILTERED COPY, deliberately — the same bandpassed, resampled
signal whisper is given, not the raw receiver output. A corpus that does not
match what the model heard would measure the wrong thing: a human listening to
cleaner audio than the model got will mark errors the model could not have
avoided, and the resulting error rate would flatter every change equally.
"""

from __future__ import annotations

import json
import logging
import secrets
import wave
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: Overs kept before the oldest is dropped. At roughly 250 kB each — a five
#: second over of 16 kHz 16-bit mono, plus a sidecar — this is about 50 MB.
#:
#: Sized for the job rather than for the card: a hundred labelled overs is
#: enough to separate a large change from noise, and nobody is going to hand-
#: transcribe a thousand.
CAPTURE_MAX = 200


def capture(
    directory: Path,
    pcm: bytes,
    rate: int,
    *,
    text: str,
    frequency_hz: int,
    duration_s: float,
    model: str,
) -> None:
    """Write one over and what the machine made of it. Never raises.

    Best-effort by construction: this is an instrument bolted to the side of a
    station, and a full card or a read-only filesystem must cost a sample rather
    than the transcription that was going to happen anyway.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Microseconds AND a random suffix. Milliseconds alone collide — two
        # captures in the same millisecond silently overwrote each other, which
        # in production is rare (overs are seconds apart) and in a test loop is
        # every time. Data loss with no signal is not worth the shorter name.
        # The fixed-width timestamp still leads, so sorting by name is sorting
        # by time, which `_trim` depends on.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        base = directory / f"over-{stamp}-{secrets.token_hex(2)}"

        with wave.open(str(base.with_suffix(".wav")), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(pcm)

        base.with_suffix(".json").write_text(
            json.dumps(
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "frequency_hz": frequency_hz,
                    "duration_s": round(duration_s, 2),
                    "model": model,
                    # What the machine said.
                    "machine": text,
                    # What was ACTUALLY said. Empty for a human to fill in — and
                    # empty is meaningfully different from "": a sidecar nobody
                    # has looked at must not be counted as a perfect score, so
                    # null is the unlabelled state and "" is a deliberate
                    # "nothing intelligible was said".
                    "truth": None,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _trim(directory)
    except Exception:  # noqa: BLE001 - an instrument must not break the station
        log.debug("Over capture failed; continuing.", exc_info=True)


def _trim(directory: Path) -> None:
    """Drop the oldest overs past the cap.

    Sorted by NAME rather than mtime: the name carries a UTC timestamp to the
    millisecond, and it is stable in a way mtime is not on a box whose clock has
    been observed coming back days wrong after a power event.
    """
    waves = sorted(directory.glob("over-*.wav"))
    for stale in waves[: max(0, len(waves) - CAPTURE_MAX)]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".json").unlink(missing_ok=True)
