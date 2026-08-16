"""The over-capture ring: an instrument that must not become a liability.

This writes to the SD card of an unattended box in a field, so the only property
that really matters is that it is BOUNDED. Everything else here is about the
sidecar being honest enough to compute a word error rate from.
"""

from __future__ import annotations

import json
import wave

from gsu.radio import corpus


def _pcm(seconds: float = 1.0, rate: int = 16000) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def _capture(directory, n=1, text="Timaru traffic"):
    for _ in range(n):
        corpus.capture(
            directory, _pcm(), 16000,
            text=text, frequency_hz=119500000, duration_s=1.0, model="ggml-small.en.bin",
        )


def test_writes_a_wav_and_a_sidecar(tmp_path):
    _capture(tmp_path)
    assert len(list(tmp_path.glob("over-*.wav"))) == 1
    sidecar = next(tmp_path.glob("over-*.json"))
    data = json.loads(sidecar.read_text())
    assert data["machine"] == "Timaru traffic"
    assert data["frequency_hz"] == 119500000


def test_truth_starts_null_not_empty(tmp_path):
    """Null is "nobody has looked at this"; "" is "nothing was said".

    Conflating them would let an unlabelled corpus score as though every over
    were silence correctly transcribed — a perfect result from doing no work.
    """
    _capture(tmp_path)
    data = json.loads(next(tmp_path.glob("over-*.json")).read_text())
    assert data["truth"] is None


def test_the_ring_is_bounded(tmp_path):
    # The property that keeps this from filling an SD card in a field.
    _capture(tmp_path, n=corpus.CAPTURE_MAX + 25)
    assert len(list(tmp_path.glob("over-*.wav"))) == corpus.CAPTURE_MAX


def test_trimming_takes_the_sidecar_with_the_audio(tmp_path):
    # An orphaned sidecar would be scored against audio nobody can listen to.
    _capture(tmp_path, n=corpus.CAPTURE_MAX + 10)
    assert len(list(tmp_path.glob("over-*.json"))) == corpus.CAPTURE_MAX


def test_the_wav_is_playable(tmp_path):
    # A corpus somebody cannot listen to is not a corpus.
    _capture(tmp_path)
    with wave.open(str(next(tmp_path.glob("over-*.wav")))) as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        assert handle.getnframes() > 0


def test_a_broken_destination_costs_a_sample_not_the_station(tmp_path):
    """Best-effort by construction.

    A full card or a read-only filesystem must cost a sample, never the
    transcription that was going to happen anyway — this is an instrument bolted
    to the side of a station, not part of it.
    """
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    corpus.capture(
        blocked / "under", _pcm(), 16000,
        text="x", frequency_hz=1, duration_s=1.0, model="m",
    )  # must not raise
