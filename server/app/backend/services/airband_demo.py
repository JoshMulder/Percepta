"""Simulated airband receiver, ported from Remote-Radio's demo source.

Remote-Radio's `DemoSource` synthesises IQ at 240 ksps and pushes it through the
real DSP chain — offset tuning, decimation, AM envelope detection, squelch — so
that running without a dongle exercises the same code as running with one.

This port deliberately skips the IQ round trip. There is no hardware here and no
DSP chain to exercise: generating complex samples only to demodulate them back
would burn CPU on every ground station to arrive at audio we already have. What
is carried across is everything an operator can actually observe:

  * per-channel noise floors, the same table, so squelch behaves differently on
    a quiet rural channel than on a noisy urban one
  * WAV broadcasts keyed by frequency in the filename, looping with a gap
  * a broadcast only being audible when tuned near it, fading across the
    channel rather than switching on at the edge
  * the signal level that results, which is what drives the meter and the gate

Drop a 16-bit WAV named `<freq_hz>.wav` into app/assets to put it on air at that
frequency, exactly as in Remote-Radio. A mock Christchurch ATIS ships on 127.200.
"""

import logging
import math
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

AUDIO_RATE = 24_000
CHANNEL_HZ = 25_000

#: Audio level of open-squelch noise, RMS.
#:
#: An AM receiver envelope-detects and carrier-normalises, so its AGC brings the
#: output to a usable level whatever the RF input was - which is why opening the
#: squelch on an empty channel gives you loud hiss, not silence. An earlier
#: version here emitted noise at its true RF amplitude, around -85 dBFS, and was
#: simply inaudible.
#:
#: So RF level and audio level are deliberately separate below: the dBFS figures
#: drive the meter and the gate, and this drives what you hear.
NOISE_AUDIO_RMS = 0.06

#: How far a strong transmission quiets the background. Real AM does not null
#: the noise the way FM capture does, so a trace always remains.
NOISE_QUIETING = 0.95

#: Signal-to-noise, in dB above the channel floor, at which quieting is full.
#: Below this the background comes back proportionally, so a weak or off-channel
#: transmission sounds like one - buried in hiss - rather than arriving clean.
FULL_QUIETING_SNR_DB = 25.0

#: Fixed gain on demodulated speech, so a transmission sits comfortably above
#: the open-squelch hiss.
#:
#: Deliberately fixed rather than automatic. A real receiver's AGC is inherent in
#: carrier-normalised detection, and an earlier version here modelled one - but
#: the simulator produces one-second blocks, and a gain that re-evaluates once a
#: second pumps audibly between syllables and overshoots into clipping on a weak
#: off-channel signal. A constant is both quieter to listen to and easier to
#: reason about, and nothing downstream depends on the level being normalised.
#: Capped just under the headroom the loaded WAV leaves: _load_wav normalises to
#: 0.9 peak, so anything above ~1.1 clips on speech transients. 1.8 did, and
#: sounded like it.
VOICE_GAIN = 1.1
#: Silence between loops, so a broadcast does not run back-to-back forever.
GAP_S = 2.0
#: How far off-channel a transmission is still audible. Beyond this the
#: receiver's filter has rejected it.
CAPTURE_HZ = 12_500

#: In-channel noise floor per channel, dBFS. Remote-Radio's table verbatim -
#: these are what make squelch worth demonstrating, since a fixed threshold set
#: on the quiet channel opens on noise on the noisy one.
CHANNEL_FLOORS_DB: dict[int, float] = {
    121_500_000: -85.0,  # Guard: quiet
    118_700_000: -68.0,  # AKL TWR: noisy urban site
    118_800_000: -74.0,  # WLG TWR
    118_400_000: -80.0,  # CHC TWR
    127_200_000: -78.0,  # CHC ATIS
    119_500_000: -82.0,  # Timaru: quiet rural site
}

ASSETS = Path(__file__).resolve().parents[2] / "assets"

#: Decoded broadcasts, shared by every receiver in the process.
#:
#: One AirbandDemo per ground station means N copies of the same audio otherwise,
#: and a 43 s clip resampled to 24 kHz float32 is about 4 MB each. The arrays are
#: only ever read - each receiver keeps its own playback position - so sharing
#: them is free.
_CACHE: dict[int, np.ndarray] | None = None


def channel_floor_db(freq_hz: int) -> float:
    """Noise floor for the channel containing this frequency."""
    channel = int(round(freq_hz / CHANNEL_HZ)) * CHANNEL_HZ
    known = CHANNEL_FLOORS_DB.get(channel)
    if known is not None:
        return known
    # Stable pseudo-random floor in -84..-70 dB, fixed per channel, so an
    # unlisted channel is still consistent from one visit to the next.
    h = (channel // CHANNEL_HZ) * 2654435761 % 1000
    return -84.0 + 14.0 * (h / 1000.0)


def _load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit PCM WAV supported")
        rate, channels = w.getframerate(), w.getnchannels()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    audio = raw.astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    peak = float(np.abs(audio).max())
    if peak > 0:
        audio *= 0.9 / peak

    # Straight to the audio rate, not the IQ rate - there is no IQ stage here.
    n_out = int(len(audio) * AUDIO_RATE / rate)
    resampled = np.interp(
        np.arange(n_out) / AUDIO_RATE,
        np.arange(len(audio)) / rate,
        audio,
    ).astype(np.float32)
    return np.concatenate([resampled, np.zeros(int(GAP_S * AUDIO_RATE), np.float32)])


def _broadcasts() -> dict[int, np.ndarray]:
    """Load the demo broadcasts once per process."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    loaded: dict[int, np.ndarray] = {}
    if ASSETS.is_dir():
        for path in sorted(ASSETS.glob("*.wav")):
            if not path.stem.isdigit():
                continue
            try:
                loaded[int(path.stem)] = _load_wav(path)
                log.info(
                    "Demo broadcast on %.3f MHz: %s (%.0f s loop)",
                    int(path.stem) / 1e6,
                    path.name,
                    len(loaded[int(path.stem)]) / AUDIO_RATE,
                )
            except Exception:
                log.exception("Could not load demo broadcast %s", path)
    log.info("Airband demo loaded %d broadcast(s)", len(loaded))
    _CACHE = loaded
    return _CACHE


class AirbandDemo:
    """One simulated receiver. Call `block()` on a fixed cadence."""

    def __init__(self) -> None:
        self.broadcasts = _broadcasts()
        # Positions are per receiver, so two stations on the same channel are
        # not in lock-step even though they share the audio behind them.
        self.pos: dict[int, int] = dict.fromkeys(self.broadcasts, 0)
        self._rng = np.random.default_rng()

    def block(self, freq_hz: int, samples: int) -> tuple[np.ndarray, float]:
        """Next audio block for a receiver tuned to `freq_hz`.

        Returns the audio and the in-channel signal level in dBFS. Every
        broadcast advances whether or not it is being listened to, so tuning to
        a channel joins a transmission already in progress rather than starting
        it from the top - which is how a radio behaves.
        """
        floor_db = channel_floor_db(freq_hz)
        noise = self._rng.standard_normal(samples).astype(np.float32)

        voice = np.zeros(samples, np.float32)
        best_db = floor_db

        for freq, mod in self.broadcasts.items():
            start = self.pos[freq]
            self.pos[freq] = int((start + samples) % len(mod))

            delta = abs(freq - freq_hz)
            if delta >= CAPTURE_HZ:
                continue
            # Fades across the channel rather than switching on at the edge, so
            # stepping 25 kHz at a time sounds like tuning past something.
            capture = math.cos((delta / CAPTURE_HZ) * (math.pi / 2)) ** 2

            idx = (start + np.arange(samples)) % len(mod)
            chunk = mod[idx] * capture
            voice = voice + chunk

            # RF level of the transmission - well above the floor while it is
            # modulating, near it during the gap between loops. This is what the
            # meter shows and what the squelch gate compares against; it is
            # deliberately not what sets the audio level.
            rms = float(np.sqrt(np.mean(np.square(chunk))) + 1e-9)
            best_db = max(best_db, 20 * math.log10(rms))

        # Quieting follows signal-to-noise, not raw amplitude: what matters is
        # how far above this channel's own floor the transmission sits. A strong
        # local signal nearly silences the hiss; a marginal one only dips it, so
        # it still sounds marginal.
        snr_db = max(0.0, best_db - floor_db)
        quiet = min(1.0, snr_db / FULL_QUIETING_SNR_DB)
        audio = (
            noise * (NOISE_AUDIO_RMS * (1.0 - NOISE_QUIETING * quiet))
            + voice * VOICE_GAIN
        ).astype(np.float32)
        np.clip(audio, -1.0, 1.0, out=audio)
        return audio, best_db

    @staticmethod
    def to_pcm16(audio: np.ndarray) -> bytes:
        return (audio * 32767.0).astype("<i2").tobytes()
