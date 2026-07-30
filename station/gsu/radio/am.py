"""IQ in, a spectrum and audio out. The numpy half of the receiver.

`dsp.py` is the *measurement* — the out-of-channel noise floor rule that
`contract/README.md` rule 3 makes load-bearing — and it is pure Python because
it runs on a list of numbers once a second. This module is the *signal
processing*: 240 000 complex samples a second have to be mixed, filtered and
decimated, and that is a quarter of a million multiply-accumulates per second
before the filter is even considered. CPython does not do that in real time and
no amount of care will make it. Hence numpy, and hence the dependency note in
`requirements.txt`.

Pipeline, ported from `Remote-Radio/server/radio_server/dsp.py`:

    240 ksps complex IQ, tuned +60 kHz above the target channel
      -> mix down by 60 kHz, putting the channel at 0 Hz and the dongle's DC
         spike at +60 kHz, which is outside both the channel (±7.5 kHz) and the
         noise measurement window (15–50 kHz). That is the entire reason for
         offset tuning and it is why nothing here has to blank a DC bin.
      -> polyphase FIR lowpass + decimate by 10 -> 24 kHz complex
      -> envelope detect, carrier-normalised (audio = |x| / carrier - 1)
      -> soft limit, and hand it to the controller, which owns the gate

Three things are deliberately *not* here, because the station already owns them
above the hardware and having two of anything is how they drift apart:
squelch, hang, and the AUTO threshold. `receiver.RadioController` decides
whether anyone hears this.

## Why the numbers are the numbers

**240 ksps.** The RTL2832U's low range is 225–300 ksps and 240 000 divides the
28.8 MHz reference exactly (÷120), so the rate is the requested rate rather than
something near it. It is also the rate Remote-Radio settled on after flaky USB
ports dropped the dongle at higher ones, and 240 000 / 24 000 is a clean
decimation by 10 to the audio rate the console expects.

**+60 kHz offset.** 60 000 / 240 000 = 1/4, so the mixer is a four-entry table of
±1 and ±j and costs one complex multiply by a cached array — no transcendentals
in the hot path. 60 kHz also puts the DC spike 10 kHz clear of the outer edge of
the noise measurement window.

**A single-look spectrum.** `dsp.noise_floor_db` converts a *median* to a mean
with `MEDIAN_TO_MEAN_DB = 1.59`, and that constant is only true for
exponentially distributed bin powers — that is, one periodogram, not an average
of several. Averaging K periodograms would tighten each bin and make that
correction over-report the floor by most of 1.59 dB, which is a squelch
threshold sitting 1.6 dB high and a receiver quietly missing weak traffic. So:
one look, and statistical stability bought with a long FFT instead. At
N = 4096 the 15–50 kHz window either side holds about 1190 bins, and the median
over that many single-look bins is good to under 0.2 dB.

## What it costs

Measured on an x86_64 development box, per second of wall clock:

    spectrum + the pure-Python floor measurement    2.7 ms   (gate shut)
    the above plus a full second of demodulation   25.9 ms   (gate open)

So the squelch gate is not only a bandwidth decision — it is most of the CPU
too, and on a quiet channel this receiver costs almost nothing. **Neither figure
has been measured on a Pi**, and the Pi 2B is the one to check: it is ARMv7 at
900 MHz against a desktop core, so a factor of fifteen or twenty would not be a
surprise and would put an open gate near half a second per second. `python -m
gsu bench` on the target is the answer; guessing from this box is not.

**Noise-calibrated bins, not tone-calibrated.** `psd = |X|^2 / (N * sum(w^2))`
makes a band of white noise sum to its true power, at the cost of a coherent
carrier reading 1.76 dB low. That is the right trade here because
`radio/simulated.py` generates its spectrum as per-bin *noise power*: with this
normalisation a dBFS number means the same thing on the simulator and on the
dongle, so a manual squelch threshold an operator found against one is still
right against the other.
"""

from __future__ import annotations

import math

import numpy as np

#: Sub-block over which the carrier AGC is averaged, in audio samples. Remote-
#: Radio updated its AGC once per 34 ms IQ block; the station's blocks are a
#: whole second, so the recursion is run on sub-blocks instead to keep the time
#: constant in milliseconds rather than in blocks.
AGC_SUB_SAMPLES = 512

#: Carrier AGC time constant. Long enough not to chase the modulation (which
#: would flatten the audio into silence — AM demodulation needs the envelope to
#: vary), short enough to catch the start of an over.
AGC_TAU_S = 0.10

#: Soft-limit knee, from Remote-Radio: over-modulated peaks compress rather
#: than clip square.
LIMIT_GAIN = 1.5


def design_lowpass(numtaps: int, cutoff_hz: float, fs: float) -> np.ndarray:
    """Windowed-sinc lowpass, Hamming window, unity gain at DC.

    This is `scipy.signal.firwin(numtaps, cutoff_hz, fs=fs)` with its default
    window, written out rather than depended on. scipy is a large compiled
    dependency and the station needs exactly this one function from it; the
    equivalence is asserted in `tests/test_radio_am.py` against the closed-form
    response rather than against scipy, which is not installed here either.
    """
    if numtaps < 1:
        raise ValueError("numtaps must be positive")
    if numtaps % 2 == 0:
        # An even-length linear-phase lowpass has a half-sample delay and a
        # forced null at Nyquist. Neither is wanted and both are easy to trip
        # over, so odd only.
        raise ValueError("numtaps must be odd for a linear-phase lowpass")
    if not 0.0 < cutoff_hz < fs / 2:
        raise ValueError(f"cutoff {cutoff_hz} must be between 0 and {fs / 2}")

    # Cutoff normalised to Nyquist, which is the form the sinc identity takes.
    normalised = 2.0 * cutoff_hz / fs
    offsets = np.arange(numtaps) - (numtaps - 1) / 2.0
    taps = normalised * np.sinc(normalised * offsets)
    taps *= np.hamming(numtaps)
    taps /= taps.sum()  # unity at DC, so the filter does not change the level
    return taps.astype(np.float32)


class PolyphaseDecimator:
    """Streaming FIR lowpass and decimator, in polyphase form.

    The direct form — filter every input sample, then throw nine of every ten
    away — is what `Remote-Radio`'s `FirDecimator` does via `scipy.signal
    .lfilter`, and at 240 ksps with 251 taps it is 60 million complex
    multiply-accumulates a second. That is around a second of CPU per second of
    audio on a Pi 5 and hopeless on anything smaller, which would make the
    receiver a device that cannot keep up with itself.

    Computing only the outputs that survive costs a factor of `factor` less: the
    same filter becomes 6 million MACs a second, which is tens of milliseconds.
    The decomposition is exact, not an approximation, and
    `tests/test_radio_am.py` asserts it against a direct implementation sample
    for sample.

    Filter state and decimation phase are both carried across calls, so the
    block size may change from call to call without a discontinuity — which it
    does, because the station hands over whatever the dongle delivered in the
    last tick rather than a fixed count.
    """

    def __init__(self, factor: int, taps: np.ndarray) -> None:
        if factor < 1:
            raise ValueError("decimation factor must be positive")
        self.factor = int(factor)
        taps = np.asarray(taps, dtype=np.float32)
        # Pad to a whole number of phases. Zero taps contribute nothing, and it
        # makes the phase decomposition exact rather than a special case.
        pad = (-taps.size) % self.factor
        if pad:
            taps = np.concatenate([taps, np.zeros(pad, dtype=np.float32)])
        self.taps = taps
        self.length = taps.size
        self.phase_length = self.length // self.factor
        #: taps[q::factor] — the sub-filter that sees every `factor`-th sample.
        self._phases = [
            np.ascontiguousarray(taps[q :: self.factor]) for q in range(self.factor)
        ]
        self._state = np.zeros(max(self.length - 1, 0), dtype=np.complex64)
        self._consumed = 0  # input samples seen, mod factor

    def reset(self) -> None:
        """Forget the filter's memory. For a retune: the samples either side of
        one are from different frequencies and running the filter across the
        join smears one into the other."""
        self._state = np.zeros(max(self.length - 1, 0), dtype=np.complex64)
        self._consumed = 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        samples = np.asarray(samples, dtype=np.complex64)
        if samples.size == 0:
            return np.zeros(0, dtype=np.complex64)

        buffer = np.concatenate([self._state, samples])
        factor, taps_per_phase = self.factor, self.phase_length

        # Outputs land on global input indices divisible by `factor`; `start` is
        # where the first of them falls inside this block.
        start = (-self._consumed) % factor
        self._consumed = (self._consumed + samples.size) % factor
        if samples.size <= start:
            self._state = buffer[-(self.length - 1) :] if self.length > 1 else buffer[:0]
            return np.zeros(0, dtype=np.complex64)
        count = (samples.size - start + factor - 1) // factor

        base = (self.length - 1) + start
        out = np.zeros(count, dtype=np.complex64)
        for phase in range(factor):
            sub_taps = self._phases[phase]
            first = base - phase - (taps_per_phase - 1) * factor
            last = first + (count + taps_per_phase - 2) * factor
            strided = buffer[first : last + 1 : factor]
            # Real and imaginary separately: numpy would otherwise promote the
            # real taps to complex and do four real multiplies where two will
            # do, which on this box is the difference that matters.
            out += np.convolve(strided.real, sub_taps, "valid").astype(np.float32)
            out += 1j * np.convolve(strided.imag, sub_taps, "valid").astype(np.float32)

        self._state = buffer[-(self.length - 1) :] if self.length > 1 else buffer[:0]
        return out


class Mixer:
    """Shift a channel sitting at `-offset_hz` up to 0 Hz, phase-continuous.

    When the offset divides the sample rate the exponential is periodic over a
    handful of samples, so it is a cached table and a multiply rather than
    240 000 calls to `exp` a second. At the station's 60 kHz on 240 ksps the
    period is four samples.
    """

    #: Longest period worth tabulating. Beyond this the table costs more memory
    #: than the transcendentals cost time.
    MAX_PERIOD = 8192

    def __init__(self, sample_rate: float, offset_hz: float) -> None:
        self.sample_rate = float(sample_rate)
        self.offset_hz = float(offset_hz)
        self._table: np.ndarray | None = None
        self._position = 0
        if float(offset_hz).is_integer() and float(sample_rate).is_integer():
            divisor = math.gcd(int(abs(offset_hz)), int(sample_rate))
            period = int(sample_rate) // divisor if divisor else 0
            if 0 < period <= self.MAX_PERIOD:
                steps = np.arange(period)
                self._table = np.exp(
                    2j * np.pi * self.offset_hz * steps / self.sample_rate
                ).astype(np.complex64)
        self._phase_samples = 0  # for the non-tabulated path

    def reset(self) -> None:
        self._position = 0
        self._phase_samples = 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        samples = np.asarray(samples, dtype=np.complex64)
        if samples.size == 0:
            return samples
        if self._table is not None:
            period = self._table.size
            rolled = np.roll(self._table, -self._position)
            self._position = (self._position + samples.size) % period
            return samples * np.resize(rolled, samples.size)
        steps = np.arange(
            self._phase_samples, self._phase_samples + samples.size, dtype=np.float64
        )
        self._phase_samples = int(
            (self._phase_samples + samples.size) % max(1, int(self.sample_rate))
        )
        rotation = np.exp(2j * np.pi * self.offset_hz * steps / self.sample_rate)
        return samples * rotation.astype(np.complex64)


def spectrum_dbfs(
    samples: np.ndarray,
    n_fft: int,
    sample_rate: float,
    offset_hz: float = 0.0,
) -> np.ndarray:
    """One single-look periodogram in dBFS, tuned channel at the centre bin.

    `offset_hz` is the hardware's tuning offset: the channel sits at `-offset`
    in the raw IQ and this shifts it to the middle, so the array lines up with
    `dsp.bin_offsets` and the noise window falls where rule 3 says it does.
    The mix is done on the snapshot rather than reusing the audio path's mixer
    because 4096 samples of `exp` is nothing and sharing state between a
    measurement that runs every tick and a demodulator that runs only when the
    gate is open would couple two things that have no reason to be coupled.

    One periodogram, not an average — see the module docstring. This is the
    single most load-bearing line in the file.
    """
    samples = np.asarray(samples, dtype=np.complex64)
    if samples.size < n_fft:
        raise ValueError(f"need {n_fft} samples for the spectrum, got {samples.size}")
    snapshot = samples[:n_fft]
    if offset_hz:
        steps = np.arange(n_fft)
        snapshot = snapshot * np.exp(
            2j * np.pi * offset_hz * steps / sample_rate
        ).astype(np.complex64)

    window = np.hanning(n_fft)
    spectrum = np.fft.fft(snapshot * window)
    power = (spectrum.real.astype(np.float64) ** 2
             + spectrum.imag.astype(np.float64) ** 2)
    # Normalised so a band of white noise sums to its true power. See the
    # module docstring for why noise and not a tone.
    power /= n_fft * float((window ** 2).sum())
    return 10.0 * np.log10(np.fft.fftshift(power) + 1e-30)


def in_channel_power_db(spectrum_db: np.ndarray, bin_hz: float, half_hz: float) -> float:
    """`dsp.in_channel_power_db` for a numpy spectrum.

    Same definition, and `tests/test_radio_am.py` asserts they agree — the pure
    Python one stays the reference because it is the one the contract is written
    against. This exists only so the front end can rank several snapshots
    without converting each to a list first.
    """
    bins = spectrum_db.size
    offsets = (np.arange(bins) - bins // 2) * bin_hz
    inside = np.abs(offsets) <= half_hz
    total = float(np.sum(10.0 ** (spectrum_db[inside] / 10.0)))
    return 10.0 * math.log10(total) if total > 0 else -300.0


class AmDemodulator:
    """Envelope AM with carrier normalisation. No squelch: that lives above."""

    def __init__(
        self,
        sample_rate: int,
        audio_rate: int,
        offset_hz: float,
        cutoff_hz: float = 8000.0,
        numtaps: int = 251,
    ) -> None:
        if sample_rate % audio_rate:
            raise ValueError(
                f"sample rate {sample_rate} is not a whole multiple of the audio "
                f"rate {audio_rate}; the decimator cannot be exact"
            )
        self.sample_rate = int(sample_rate)
        self.audio_rate = int(audio_rate)
        self.decimation = self.sample_rate // self.audio_rate
        # 8 kHz: the occupied bandwidth of an AM airband channel is ±7.5 kHz, so
        # this is the channel filter and it matches `dsp.CHANNEL_HALF_HZ`. The
        # transition runs out to the 12 kHz alias edge, which 251 Hamming taps
        # at 240 ksps just about buys.
        self._mixer = Mixer(sample_rate, offset_hz)
        self._decimator = PolyphaseDecimator(
            self.decimation, design_lowpass(numtaps, cutoff_hz, sample_rate)
        )
        self._carrier = 0.0
        self._alpha = 1.0 - math.exp(
            -(AGC_SUB_SAMPLES / self.audio_rate) / AGC_TAU_S
        )

    def reset(self) -> None:
        """Retune, or a gap in demodulation. Forget the filter and the carrier:
        both describe a channel we are no longer listening to, and an AGC still
        holding the last channel's carrier would open on the new one at whatever
        gain the old one needed."""
        self._mixer.reset()
        self._decimator.reset()
        self._carrier = 0.0

    def process(self, samples: np.ndarray, fade_in: bool = False) -> np.ndarray:
        """One block of IQ to float audio in [-1, 1] at `audio_rate`.

        `fade_in` ramps the first few milliseconds up from zero. The controller
        starts and stops sending audio as the gate opens and closes, and a
        carrier-normalised block starting at full amplitude against the
        listener's silence is an audible click on every over.
        """
        baseband = self._mixer.process(samples)
        channel = self._decimator.process(baseband)
        if channel.size == 0:
            return np.zeros(0, dtype=np.float32)

        envelope = np.abs(channel)
        carrier = self._smooth_carrier(envelope)
        audio = envelope / np.maximum(carrier, 1e-9) - 1.0
        audio = np.tanh(LIMIT_GAIN * audio).astype(np.float32)

        if fade_in:
            ramp_samples = min(audio.size, max(1, self.audio_rate // 200))  # 5 ms
            audio[:ramp_samples] *= np.linspace(
                0.0, 1.0, ramp_samples, dtype=np.float32
            )
        return audio

    def _smooth_carrier(self, envelope: np.ndarray) -> np.ndarray:
        """The AM carrier level, tracked across sub-blocks and interpolated.

        Interpolated rather than held: a gain that steps every 21 ms is a buzz
        at 47 Hz whenever the level is moving, which is exactly when someone is
        talking.
        """
        size = AGC_SUB_SAMPLES
        blocks = envelope.size // size
        if blocks < 1:
            mean = float(envelope.mean())
            self._carrier = mean if self._carrier <= 0 else (
                self._carrier + self._alpha * (mean - self._carrier)
            )
            return np.full(envelope.size, self._carrier, dtype=np.float32)

        means = envelope[: blocks * size].reshape(blocks, size).mean(axis=1)
        tracked = np.empty(blocks, dtype=np.float32)
        carrier = self._carrier
        for index in range(blocks):
            mean = float(means[index])
            carrier = mean if carrier <= 0 else carrier + self._alpha * (mean - carrier)
            tracked[index] = carrier
        self._carrier = carrier

        centres = np.arange(blocks, dtype=np.float64) * size + size / 2.0
        return np.interp(
            np.arange(envelope.size, dtype=np.float64), centres, tracked
        ).astype(np.float32)
