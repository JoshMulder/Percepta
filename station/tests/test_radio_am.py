"""The DSP, against IQ this file generates and therefore knows the answer for.

Three things are being checked, and only the first is arithmetic:

**The polyphase decimator computes the same numbers as the obvious filter.** It
is a factor-of-ten optimisation of a loop that would otherwise not run in real
time, so it is exactly the kind of code that can be plausibly wrong. There is a
direct implementation here and the two are compared sample for sample.

**The dBFS scale means what the squelch thinks it means.** White noise of a
known power must come back out of `am.spectrum_dbfs` and `dsp.noise_floor_db`
as that power. If this drifts, every absolute threshold in the receiver drifts
with it and nothing else in the suite would notice.

**A tone in gives that tone out.** The end-to-end AM check: modulate a carrier
at a known audio frequency, put it at a known offset, and require the
demodulator to produce that frequency and roughly that depth.

Skipped where numpy is absent — see `requirements.txt` on why it is optional.
"""

import math
import unittest

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised by not having numpy
    np = None

from gsu.radio import dsp

if np is not None:
    from gsu.radio import am
    from gsu.radio.rtlsdr import BIN_HZ, N_FFT, OFFSET_HZ, SAMPLE_RATE
else:  # pragma: no cover
    BIN_HZ = N_FFT = OFFSET_HZ = SAMPLE_RATE = 0


def airband_iq(
    seconds: float,
    audio_hz: float = 1000.0,
    depth: float = 0.5,
    carrier: float = 0.05,
    noise: float = 0.0,
    channel_offset_hz: float = 0.0,
    seed: int = 1,
):
    """IQ as the dongle would deliver it: the channel sits at `-OFFSET_HZ`.

    The station tunes `OFFSET_HZ` above the target, so a transmission on the
    target frequency arrives that far *below* centre in the raw IQ. Getting this
    sign wrong is the mistake worth having a fixture for.
    """
    count = int(seconds * SAMPLE_RATE)
    steps = np.arange(count)
    envelope = carrier * (1.0 + depth * np.cos(2 * np.pi * audio_hz * steps / SAMPLE_RATE))
    position = channel_offset_hz - OFFSET_HZ
    signal = envelope * np.exp(2j * np.pi * position * steps / SAMPLE_RATE)
    if noise:
        rng = np.random.default_rng(seed)
        signal = signal + (
            rng.normal(0.0, noise / math.sqrt(2), count)
            + 1j * rng.normal(0.0, noise / math.sqrt(2), count)
        )
    return signal.astype(np.complex64)


def white_iq(power: float, count: int, seed: int = 7):
    """Complex white noise of total power `power` (so `E|x|^2 == power`)."""
    rng = np.random.default_rng(seed)
    sigma = math.sqrt(power / 2.0)
    return (
        rng.normal(0.0, sigma, count) + 1j * rng.normal(0.0, sigma, count)
    ).astype(np.complex64)


@unittest.skipIf(np is None, "numpy is not installed")
class FilterDesignTests(unittest.TestCase):
    def test_unity_at_dc(self):
        taps = am.design_lowpass(251, 8000.0, SAMPLE_RATE)
        self.assertAlmostEqual(float(taps.sum()), 1.0, places=5)

    def test_passband_flat_and_stopband_rejected(self):
        taps = am.design_lowpass(251, 8000.0, SAMPLE_RATE).astype(np.float64)
        size = 8192
        response = np.abs(np.fft.rfft(taps, size))
        freqs = np.fft.rfftfreq(size, 1.0 / SAMPLE_RATE)

        passband = response[freqs <= 6000.0]
        self.assertLess(
            20 * math.log10(float(passband.max() / passband.min())), 0.5,
            "the voice band must be flat to well under a dB",
        )
        # Everything that would alias into the 24 kHz output has to be gone.
        stopband = response[freqs >= 13_000.0]
        self.assertLess(
            20 * math.log10(float(stopband.max())), -45.0,
            "out-of-channel energy must not fold back into the audio",
        )

    def test_even_lengths_are_refused(self):
        with self.assertRaises(ValueError):
            am.design_lowpass(250, 8000.0, SAMPLE_RATE)

    def test_the_bandpass_passes_voice_and_rejects_the_rest(self):
        taps = am.design_bandpass(am.VOICE_TAPS, 300.0, 3400.0, 24_000).astype(np.float64)
        size = 8192
        response = np.abs(np.fft.rfft(taps, size))
        freqs = np.fft.rfftfreq(size, 1.0 / 24_000)

        def at(hz: float) -> float:
            return float(response[int(np.argmin(np.abs(freqs - hz)))])

        # Flat across the voice band.
        self.assertGreater(at(1000.0), 0.9)
        self.assertGreater(at(2500.0), 0.85)
        # The rumble below and the hiss above are gone.
        self.assertLess(at(0.0), 0.05, "DC must be blocked")
        self.assertLess(at(60.0), 0.3, "low rumble rejected")
        self.assertLess(at(6000.0), 0.1, "the hiss band above voice is gone")

    def test_the_bandpass_refuses_corners_it_cannot_meet(self):
        with self.assertRaises(ValueError):
            am.design_bandpass(am.VOICE_TAPS, 3400.0, 300.0, 24_000)  # low > high
        with self.assertRaises(ValueError):
            am.design_bandpass(am.VOICE_TAPS, 300.0, 13_000.0, 24_000)  # above Nyquist


def direct_decimate(samples, taps, factor):
    """The obvious implementation: filter everything, keep every Nth.

    Deliberately slow and deliberately dumb. It is the reference the polyphase
    decomposition is checked against.
    """
    padded = np.concatenate([np.zeros(len(taps) - 1, dtype=np.complex128), samples])
    filtered = np.convolve(padded, taps, "valid")
    return filtered[::factor]


@unittest.skipIf(np is None, "numpy is not installed")
class PolyphaseTests(unittest.TestCase):
    def test_matches_the_direct_implementation(self):
        taps = am.design_lowpass(251, 8000.0, SAMPLE_RATE)
        rng = np.random.default_rng(3)
        samples = (
            rng.normal(0, 1, 9600) + 1j * rng.normal(0, 1, 9600)
        ).astype(np.complex64)

        decimator = am.PolyphaseDecimator(10, taps)
        got = decimator.process(samples)
        want = direct_decimate(samples.astype(np.complex128), taps.astype(np.float64), 10)

        self.assertEqual(got.size, want.size)
        np.testing.assert_allclose(got, want, atol=2e-5)

    def test_state_survives_ragged_blocks(self):
        """The station hands over whatever the dongle delivered, which is never
        the same count twice. Chopped arbitrarily, the output must be identical
        to the same input processed whole."""
        taps = am.design_lowpass(63, 8000.0, SAMPLE_RATE)
        rng = np.random.default_rng(11)
        samples = (
            rng.normal(0, 1, 5000) + 1j * rng.normal(0, 1, 5000)
        ).astype(np.complex64)

        whole = am.PolyphaseDecimator(10, taps).process(samples)

        chunked = am.PolyphaseDecimator(10, taps)
        pieces, cursor = [], 0
        for size in (7, 993, 1, 1500, 12, 2487):
            pieces.append(chunked.process(samples[cursor : cursor + size]))
            cursor += size
        pieces.append(chunked.process(samples[cursor:]))
        joined = np.concatenate(pieces)

        self.assertEqual(joined.size, whole.size)
        np.testing.assert_allclose(joined, whole, atol=1e-6)


@unittest.skipIf(np is None, "numpy is not installed")
class FirFilterTests(unittest.TestCase):
    def test_state_survives_ragged_blocks(self):
        """Same as the decimator: the audio filter runs on whatever the last
        tick produced, so a chopped stream must equal the whole one."""
        taps = am.design_bandpass(101, 300.0, 3400.0, 24_000)
        rng = np.random.default_rng(5)
        signal = rng.normal(0, 1, 4000).astype(np.float32)

        whole = am.FirFilter(taps).process(signal)

        chunked = am.FirFilter(taps)
        pieces, cursor = [], 0
        for size in (3, 777, 1, 1200, 19):
            pieces.append(chunked.process(signal[cursor : cursor + size]))
            cursor += size
        pieces.append(chunked.process(signal[cursor:]))
        joined = np.concatenate(pieces)

        self.assertEqual(joined.size, whole.size)
        np.testing.assert_allclose(joined, whole, atol=1e-4)


@unittest.skipIf(np is None, "numpy is not installed")
class MixerTests(unittest.TestCase):
    def test_moves_the_channel_to_zero(self):
        mixer = am.Mixer(SAMPLE_RATE, OFFSET_HZ)
        samples = airband_iq(0.05, depth=0.0, noise=0.0)
        mixed = mixer.process(samples)
        # A channel that was at -OFFSET is now at DC, so the mixed signal is
        # (almost) a constant.
        self.assertLess(
            float(np.std(np.abs(np.angle(mixed[1:] / mixed[:-1])))), 1e-3,
            "the residual rotation should be nil once the offset is removed",
        )

    def test_phase_is_continuous_across_blocks(self):
        samples = airband_iq(0.02, depth=0.0)
        whole = am.Mixer(SAMPLE_RATE, OFFSET_HZ).process(samples)
        split = am.Mixer(SAMPLE_RATE, OFFSET_HZ)
        # 4801 is deliberately not a multiple of the mixer's four-sample period.
        joined = np.concatenate(
            [split.process(samples[:4801]), split.process(samples[4801:])]
        )
        np.testing.assert_allclose(joined, whole, atol=1e-6)


@unittest.skipIf(np is None, "numpy is not installed")
class SpectrumCalibrationTests(unittest.TestCase):
    """The scale every absolute threshold in the receiver is expressed on."""

    def test_white_noise_reads_its_true_power(self):
        power = 1e-6  # -60 dBFS spread across the whole 240 kHz
        samples = white_iq(power, N_FFT * 4)
        spectrum = am.spectrum_dbfs(samples, N_FFT, SAMPLE_RATE, OFFSET_HZ)

        # Summed across every bin, the spectrum must come back to that power.
        total = float(np.sum(10.0 ** (spectrum / 10.0)))
        self.assertAlmostEqual(10 * math.log10(total), 10 * math.log10(power), delta=0.2)

    def test_the_measured_floor_is_the_true_in_channel_noise(self):
        """End to end: `am.spectrum_dbfs` into `dsp.noise_floor_db`.

        The channel is 15 kHz of a 240 kHz span, so in-channel noise is a
        sixteenth of the total — 12.04 dB down. Every constant in the chain has
        to be right for this to land, which is the point of asserting it.
        """
        power = 1e-6
        expected = 10 * math.log10(power) - 10 * math.log10(SAMPLE_RATE / 15_000.0)

        floors = []
        for seed in range(8):
            samples = white_iq(power, N_FFT, seed=seed)
            spectrum = am.spectrum_dbfs(samples, N_FFT, SAMPLE_RATE, OFFSET_HZ)
            floors.append(dsp.noise_floor_db(spectrum.tolist(), BIN_HZ))
        self.assertAlmostEqual(sum(floors) / len(floors), expected, delta=0.4)

    def test_rssi_and_the_floor_agree_on_an_empty_channel(self):
        """With nothing transmitting, the in-channel power *is* the noise floor.

        This is the pairing the squelch rests on: the floor is measured 15–50 kHz
        out and the RSSI inside the channel, and on a quiet channel they have to
        come out the same or the AUTO margin is not 8 dB of anything.
        """
        gaps = []
        for seed in range(8):
            samples = white_iq(1e-6, N_FFT, seed=seed)
            spectrum = am.spectrum_dbfs(samples, N_FFT, SAMPLE_RATE, OFFSET_HZ).tolist()
            gaps.append(
                dsp.in_channel_power_db(spectrum, BIN_HZ)
                - dsp.noise_floor_db(spectrum, BIN_HZ)
            )
        self.assertAlmostEqual(sum(gaps) / len(gaps), 0.0, delta=0.5)

    def test_the_numpy_and_python_channel_power_agree(self):
        samples = airband_iq(0.05, noise=1e-3)
        spectrum = am.spectrum_dbfs(samples, N_FFT, SAMPLE_RATE, OFFSET_HZ)
        self.assertAlmostEqual(
            am.in_channel_power_db(spectrum, BIN_HZ, dsp.CHANNEL_HALF_HZ),
            dsp.in_channel_power_db(spectrum.tolist(), BIN_HZ),
            places=6,
        )

    def test_the_dc_spike_lands_outside_every_measurement(self):
        """Offset tuning's whole job, asserted rather than assumed.

        A large DC bias in the raw IQ — which is what an RTL2832 has — must not
        touch either the channel or the noise window.
        """
        samples = white_iq(1e-8, N_FFT) + np.complex64(0.5)  # a whopping DC term
        spectrum = am.spectrum_dbfs(samples, N_FFT, SAMPLE_RATE, OFFSET_HZ).tolist()
        offsets = dsp.bin_offsets(len(spectrum), BIN_HZ)
        spike = max(range(len(spectrum)), key=lambda index: spectrum[index])
        self.assertAlmostEqual(abs(offsets[spike]), OFFSET_HZ, delta=BIN_HZ * 2)
        self.assertGreater(
            abs(offsets[spike]), dsp.NOISE_OUTER_HZ,
            "the spike must be clear of the 15-50 kHz noise window",
        )


@unittest.skipIf(np is None, "numpy is not installed")
class DemodulationTests(unittest.TestCase):
    """A known tone at a known offset must come out as that tone."""

    def demodulate(self, samples, **kwargs):
        demod = am.AmDemodulator(SAMPLE_RATE, 24_000, OFFSET_HZ, **kwargs)
        return demod.process(samples)

    @staticmethod
    def dominant_tone(audio, rate=24_000):
        """Frequency of the strongest component, and how far it stands above
        the rest of the spectrum in dB."""
        windowed = audio.astype(np.float64) * np.hanning(audio.size)
        spectrum = np.abs(np.fft.rfft(windowed))
        spectrum[0] = 0.0  # the AGC leaves no DC worth looking at
        peak = int(np.argmax(spectrum))
        freqs = np.fft.rfftfreq(audio.size, 1.0 / rate)
        others = np.delete(spectrum, [peak - 1, peak, peak + 1][: spectrum.size])
        return float(freqs[peak]), 20 * math.log10(
            float(spectrum[peak]) / max(float(others.max()), 1e-30)
        )

    def test_a_tone_demodulates_to_that_tone(self):
        audio = self.demodulate(airband_iq(0.5, audio_hz=1000.0, depth=0.5))
        self.assertEqual(audio.size, 12_000)  # half a second at 24 kHz
        # Skip the AGC's settling and the fade, then look at what is left.
        tone, purity = self.dominant_tone(audio[4000:])
        self.assertAlmostEqual(tone, 1000.0, delta=10.0)
        self.assertGreater(purity, 20.0, "the recovered tone should dominate")

    def test_several_tones_land_where_they_were_put(self):
        for wanted in (400.0, 1000.0, 2500.0):
            with self.subTest(audio_hz=wanted):
                audio = self.demodulate(airband_iq(0.5, audio_hz=wanted, depth=0.4))
                tone, _ = self.dominant_tone(audio[4000:])
                self.assertAlmostEqual(tone, wanted, delta=15.0)

    def test_modulation_depth_is_recovered(self):
        """Shallow modulation, where the soft limiter is still linear.

        The expected figure is `LIMIT_GAIN` times the depth, not the depth:
        `tanh(1.5 x)` is a make-up gain of 1.5 for small signals and a limiter
        for large ones, so 10% modulation lands at 0.15 full scale. Ported from
        Remote-Radio as-is — at full modulation it puts the peak at 0.905,
        which is a healthy level with headroom left before `to_pcm16` clips.
        """
        # Voice filter off: this measures the AGC and limiter gain, and a
        # band-pass at unity in the middle of its passband would only add ripple
        # to a figure asserted to a hundredth.
        audio = self.demodulate(
            airband_iq(1.0, audio_hz=1000.0, depth=0.1), voice_filter=False)
        recovered = float(np.sqrt(2.0) * np.std(audio[12_000:]))
        self.assertAlmostEqual(recovered, 0.1 * am.LIMIT_GAIN, delta=0.01)

    def test_the_voice_filter_removes_out_of_band_audio(self):
        """A 7 kHz tone is inside the channel but above the voice band, so the
        voice filter should all but erase it while leaving speech alone."""
        iq = airband_iq(0.5, audio_hz=7000.0, depth=0.5)
        on = self.demodulate(iq, voice_filter=True)
        off = self.demodulate(iq, voice_filter=False)
        on_rms = float(np.sqrt(np.mean(on[4000:] ** 2)))
        off_rms = float(np.sqrt(np.mean(off[4000:] ** 2)))
        self.assertGreater(off_rms, 0.05, "the tone is there without the filter")
        self.assertLess(on_rms, off_rms * 0.2, "and mostly gone with it")

    def test_the_voice_filter_keeps_speech(self):
        audio = self.demodulate(
            airband_iq(0.5, audio_hz=1000.0, depth=0.5), voice_filter=True)
        tone, purity = self.dominant_tone(audio[4000:])
        self.assertAlmostEqual(tone, 1000.0, delta=15.0)
        self.assertGreater(purity, 20.0, "a voice-band tone must survive the filter")

    def test_a_narrow_channel_filter_rejects_high_audio(self):
        """The RF side: a 6 kHz tone's sidebands sit at ±6 kHz, inside the 8 kHz
        channel but outside a 4 kHz one, so narrowing the channel filter removes
        it before the envelope detector. Voice filter off, to isolate the RF one.
        """
        iq = airband_iq(0.5, audio_hz=6000.0, depth=0.5)
        wide = self.demodulate(iq, cutoff_hz=8000.0, voice_filter=False)
        narrow = self.demodulate(iq, cutoff_hz=4000.0, voice_filter=False)
        wide_rms = float(np.sqrt(np.mean(wide[4000:] ** 2)))
        narrow_rms = float(np.sqrt(np.mean(narrow[4000:] ** 2)))
        self.assertGreater(wide_rms, 0.05, "the 8 kHz channel passes it")
        self.assertLess(narrow_rms, wide_rms * 0.3, "the 4 kHz channel rejects it")

    def test_the_channel_filter_rejects_an_adjacent_channel(self):
        """Measured at the filter, before the AGC.

        It has to be measured there. The carrier normalisation downstream
        divides by whatever level it finds, so on a channel holding nothing but
        the leakage of a neighbour it will happily amplify that leakage back up
        to full scale — the rejection is real but invisible at the output. What
        the filter does is the fact worth pinning down.
        """
        levels = {}
        for offset in (0.0, 30_000.0):
            mixer = am.Mixer(SAMPLE_RATE, OFFSET_HZ)
            decimator = am.PolyphaseDecimator(
                10, am.design_lowpass(251, 8000.0, SAMPLE_RATE)
            )
            filtered = decimator.process(
                mixer.process(airband_iq(0.5, depth=0.5, channel_offset_hz=offset))
            )
            levels[offset] = float(np.abs(filtered[2000:]).mean())
        rejection = 20 * math.log10(levels[0.0] / max(levels[30_000.0], 1e-30))
        self.assertGreater(
            rejection, 45.0,
            "a transmission 30 kHz away is a different channel and must be gone",
        )

    def test_an_adjacent_channel_does_not_bleed_into_the_audio(self):
        """The same thing from the listener's seat: a strong neighbour talking
        over a weak local signal must not be what comes out of the speaker."""
        wanted = airband_iq(0.5, audio_hz=1000.0, depth=0.5, carrier=0.05)
        interferer = airband_iq(
            0.5, audio_hz=3000.0, depth=0.5, carrier=0.5, channel_offset_hz=30_000.0
        )
        audio = self.demodulate((wanted + interferer).astype(np.complex64))
        tone, _ = self.dominant_tone(audio[4000:])
        self.assertAlmostEqual(
            tone, 1000.0, delta=15.0,
            msg="the on-channel tone must win against a neighbour ten times stronger",
        )

    def test_a_slightly_mistuned_signal_still_demodulates(self):
        """Airband channels are 25 kHz apart and a dongle's reference can be
        hundreds of ppm out; a couple of kHz of error must still be audible."""
        audio = self.demodulate(
            airband_iq(0.5, audio_hz=1000.0, depth=0.5, channel_offset_hz=2000.0)
        )
        tone, _ = self.dominant_tone(audio[4000:])
        self.assertAlmostEqual(tone, 1000.0, delta=15.0)

    def test_output_stays_inside_the_pcm_range(self):
        """`audio.to_pcm16` clips, and clipping is audible. Heavy modulation
        must be soft-limited before it gets there."""
        audio = self.demodulate(airband_iq(0.5, audio_hz=1000.0, depth=1.4))
        self.assertLessEqual(float(np.abs(audio).max()), 1.0)

    def test_a_fade_in_starts_from_silence(self):
        """Every over begins against the listener's silence, and a
        carrier-normalised block starting at full amplitude clicks."""
        demod = am.AmDemodulator(SAMPLE_RATE, 24_000, OFFSET_HZ)
        audio = demod.process(airband_iq(0.2, depth=0.8), fade_in=True)
        self.assertLess(abs(float(audio[0])), 1e-6)
        self.assertGreater(float(np.abs(audio[2000:]).max()), 0.05)

    def test_the_fade_in_is_02s_and_carries_across_blocks(self):
        """The ramp is 0.2 s — longer than one demod block — so it continues
        over the blocks that follow the one that opened the over, rather than
        reaching full amplitude a block in."""
        demod = am.AmDemodulator(SAMPLE_RATE, 24_000, OFFSET_HZ)
        self.assertEqual(demod._fade_samples, int(am.FADE_IN_S * 24_000))
        # A single 50 ms block cannot finish a 0.2 s fade: it carries on.
        demod.process(airband_iq(0.05, depth=0.8), fade_in=True)
        self.assertGreater(demod._fade_pos, 0)
        self.assertLess(demod._fade_pos, demod._fade_samples)
        # Feeding past 0.2 s of audio completes it, and it does not re-arm
        # without another over opening.
        for _ in range(5):
            demod.process(airband_iq(0.05, depth=0.8))
        self.assertGreaterEqual(demod._fade_pos, demod._fade_samples)


@unittest.skipIf(np is None, "numpy is not installed")
class SquelchThresholdTests(unittest.TestCase):
    """Where the gate actually opens, measured rather than claimed.

    `dsp.auto_threshold_db` puts AUTO 8 dB above the measured floor, so a
    transmission whose in-channel power is more than 8 dB above the noise must
    open the gate and one below it must not. This drives the real measurement
    chain — synthetic IQ through `am.spectrum_dbfs` into `dsp` — rather than
    handing `dsp` a spectrum with the answer already in it.
    """

    NOISE_POWER = 1e-6

    def rssi_and_threshold(self, snr_db, seed=0):
        noise = white_iq(self.NOISE_POWER, N_FFT, seed=seed)
        samples = noise
        if snr_db is not None:
            # In-channel noise is a sixteenth of the total; put the carrier
            # `snr_db` above that.
            in_channel = self.NOISE_POWER * 15_000.0 / SAMPLE_RATE
            amplitude = math.sqrt(in_channel * 10 ** (snr_db / 10.0))
            steps = np.arange(N_FFT)
            samples = noise + (
                amplitude * np.exp(2j * np.pi * -OFFSET_HZ * steps / SAMPLE_RATE)
            ).astype(np.complex64)
        spectrum = am.spectrum_dbfs(samples, N_FFT, SAMPLE_RATE, OFFSET_HZ).tolist()
        return (
            dsp.in_channel_power_db(spectrum, BIN_HZ),
            dsp.auto_threshold_db(dsp.noise_floor_db(spectrum, BIN_HZ)),
        )

    def test_an_empty_channel_keeps_the_gate_shut(self):
        for seed in range(6):
            rssi, threshold = self.rssi_and_threshold(None, seed=seed)
            self.assertLess(rssi, threshold, "noise alone must not break squelch")

    def test_a_signal_well_above_the_floor_opens_it(self):
        for seed in range(6):
            rssi, threshold = self.rssi_and_threshold(14.0, seed=seed)
            self.assertGreater(rssi, threshold, "14 dB SNR must break squelch")

    def test_the_crossing_is_near_the_eight_dB_margin(self):
        """Not exactly 8 dB: a carrier reads 1.76 dB low on the noise-calibrated
        scale (`am.py`), and in-channel noise adds to the carrier's own power.
        What matters is that the knee is where the design says, not several dB
        away from it."""
        below = [self.rssi_and_threshold(4.0, seed=s) for s in range(6)]
        above = [self.rssi_and_threshold(12.0, seed=s) for s in range(6)]
        self.assertTrue(all(rssi < threshold for rssi, threshold in below))
        self.assertTrue(all(rssi > threshold for rssi, threshold in above))

    def test_a_long_carrier_cannot_close_the_gate_behind_itself(self):
        """Rule 3's regression, driven from real IQ rather than a hand-built
        spectrum: a strong carrier held for a long time must not drag the
        measured floor up, because the floor never looks inside the channel."""
        quiet = self.rssi_and_threshold(None, seed=1)[1]
        loud = self.rssi_and_threshold(40.0, seed=1)[1]
        self.assertAlmostEqual(quiet, loud, delta=0.3)


if __name__ == "__main__":
    unittest.main()
