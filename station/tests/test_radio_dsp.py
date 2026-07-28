"""The regression `contract/README.md` rule 3 exists for.

An in-channel noise tracker fails like this: a weak signal arrives while the
estimate is stale-high, gets treated as noise, the floor drifts up toward it,
the threshold follows, and the gate latches shut permanently. The platform's
simulator cannot exercise it — it is *told* the floor — so this is the only
place the behaviour is checked.
"""

import math
import unittest

from gsu.radio import dsp
from gsu.radio.receiver import RadioController
from gsu.radio.simulated import SimulatedFrontEnd


def flat_spectrum(per_bin_db: float, bins: int = 241) -> list[float]:
    return [per_bin_db] * bins


def with_carrier(per_bin_db: float, carrier_db: float, bins: int = 241) -> list[float]:
    """A strong carrier in the channel and nothing outside it."""
    spectrum = flat_spectrum(per_bin_db, bins)
    centre = bins // 2
    for offset in range(-3, 4):
        spectrum[centre + offset] = carrier_db
    return spectrum


class NoiseFloorTests(unittest.TestCase):
    def test_measured_outside_the_channel(self):
        floor = dsp.noise_floor_db(flat_spectrum(-100.0))
        # Flat noise: median is the per-bin value, plus the median-to-mean
        # correction and the width of the channel.
        expected = -100.0 + dsp.MEDIAN_TO_MEAN_DB + 10 * math.log10(dsp.CHANNEL_BINS)
        self.assertAlmostEqual(floor, expected, places=6)

    def test_a_carrier_cannot_bias_the_floor(self):
        quiet = dsp.noise_floor_db(flat_spectrum(-100.0))
        loud = dsp.noise_floor_db(with_carrier(-100.0, -20.0))
        self.assertEqual(quiet, loud)

    def test_the_gate_does_not_latch_shut(self):
        """The failure itself: a weak signal must still open the gate after a
        long strong one, because the floor never moved."""
        controller = RadioController(SimulatedFrontEnd(traffic="off"))
        strong = with_carrier(-100.0, -30.0)
        weak = with_carrier(-100.0, -70.0)

        for _ in range(300):
            floor = dsp.noise_floor_db(strong)
            threshold = dsp.auto_threshold_db(floor)
        self.assertLess(threshold, dsp.noise_floor_db(weak) + 20)

        rssi = dsp.in_channel_power_db(weak)
        self.assertGreater(
            rssi, threshold,
            "a weak signal after a long strong one must still break squelch",
        )
        controller.shutdown()

    def test_correction_matches_the_measured_constant(self):
        # Remote-Radio measured 14.81 dB on its own bin plan; this arrives at
        # the same number from ours, which is the agreement worth asserting.
        self.assertAlmostEqual(dsp.IN_CHANNEL_CORRECTION_DB, 14.81, delta=0.1)

    def test_a_spectrum_that_is_too_narrow_is_refused(self):
        # Better to fail loudly than to quietly measure inside the channel.
        with self.assertRaises(ValueError):
            dsp.noise_floor_db([-100.0] * 21)


class SquelchTests(unittest.TestCase):
    def setUp(self):
        self.controller = RadioController(SimulatedFrontEnd(traffic="off", seed=1))

    def tearDown(self):
        self.controller.shutdown()

    def test_auto_rides_the_floor(self):
        payload, _ = self.controller.tick(1.0)
        self.assertAlmostEqual(
            payload["threshold_db"],
            round(payload["noise_floor_db"] + dsp.AUTO_SQUELCH_MARGIN_DB, 1),
            delta=0.11,
        )

    def test_turning_auto_off_freezes_the_threshold(self):
        payload, _ = self.controller.tick(1.0)
        frozen = payload["threshold_db"]
        self.controller.set_auto_squelch(False)
        for _ in range(5):
            payload, _ = self.controller.tick(1.0)
        self.assertAlmostEqual(payload["threshold_db"], frozen, delta=0.2)
        self.assertFalse(payload["auto_squelch"])

    def test_setting_a_threshold_leaves_auto(self):
        self.controller.set_squelch(-55.0)
        payload, _ = self.controller.tick(1.0)
        self.assertEqual(payload["threshold_db"], -55.0)
        self.assertFalse(payload["auto_squelch"])

    def test_monitor_opens_the_gate_without_moving_the_threshold(self):
        payload, audio = self.controller.tick(1.0)
        self.assertIsNone(audio)
        before = payload["threshold_db"]
        self.controller.set_monitor(True)
        payload, audio = self.controller.tick(1.0)
        self.assertTrue(payload["squelch_open"])
        self.assertIsNotNone(audio, "monitor must push audio, hiss included")
        # AUTO keeps riding the measured floor while monitor is held, so the
        # threshold moves with the noise — what it must not do is jump.
        self.assertAlmostEqual(payload["threshold_db"], before, delta=1.5)

    def test_audio_only_while_the_gate_is_open(self):
        front_end = SimulatedFrontEnd(traffic="busy", seed=7)
        controller = RadioController(front_end)
        opens = audio_frames = 0
        for _ in range(200):
            payload, audio = controller.tick(1.0)
            opens += payload["squelch_open"]
            audio_frames += audio is not None
            self.assertEqual(
                audio is not None, payload["squelch_open"],
                "audio and squelch_open must never disagree",
            )
        self.assertGreater(audio_frames, 0, "the busy profile should transmit")
        self.assertLess(audio_frames, 200)
        controller.shutdown()

    def test_frequency_is_clamped_to_the_airband(self):
        self.controller.tune(900_000_000)
        self.assertEqual(self.controller.freq_hz, 137_000_000)
        self.controller.tune(1_000)
        self.assertEqual(self.controller.freq_hz, 108_000_000)

    def test_no_transmit_capability_is_ever_reported(self):
        payload, _ = self.controller.tick(1.0)
        self.assertFalse(payload["tx_capable"])


if __name__ == "__main__":
    unittest.main()
