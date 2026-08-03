"""The regression `contract/README.md` rule 3 exists for.

An in-channel noise tracker fails like this: a weak signal arrives while the
estimate is stale-high, gets treated as noise, the floor drifts up toward it,
the threshold follows, and the gate latches shut permanently. The platform's
simulator cannot exercise it — it is *told* the floor — so this is the only
place the behaviour is checked.
"""

import math
import time
import unittest
from unittest import mock

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

    def test_a_held_gate_releases_itself(self):
        """The console holding monitor can close, crash or be signed out.

        Nothing on the platform releases it, and a held gate reports
        squelch_open — so audio flows continuously up a metered link from an
        unattended site until somebody notices. It has to time out here,
        because here is the only side that is always running.
        """
        self.controller.set_monitor(True)
        payload, _ = self.controller.tick(1.0)
        self.assertTrue(payload["monitor"])

        # Just before the deadline it is still held: a real press must not be
        # cut short while somebody is genuinely listening to the noise.
        self.controller._monitor_until = time.monotonic() + 0.05
        payload, _ = self.controller.tick(1.0)
        self.assertTrue(payload["monitor"], "released early")

        self.controller._monitor_until = time.monotonic() - 0.01
        payload, _ = self.controller.tick(1.0)
        self.assertFalse(payload["monitor"], "the gate stayed held for ever")
        # Reported, not merely done: a console must never show a gate held open
        # that is not, and the platform only ever learns this from telemetry.
        self.assertFalse(payload["squelch_open"])

    def test_the_release_deadline_is_generous_but_finite(self):
        from gsu.radio.receiver import MONITOR_MAX_S

        # Long enough to set an audio level against the noise unhurried...
        self.assertGreaterEqual(MONITOR_MAX_S, 60)
        # ...and short enough that a forgotten press is not a month of uplink.
        self.assertLessEqual(MONITOR_MAX_S, 900)

    def test_releasing_monitor_by_hand_clears_the_deadline(self):
        self.controller.set_monitor(True)
        self.controller.set_monitor(False)
        self.assertEqual(self.controller._monitor_until, 0.0)
        payload, _ = self.controller.tick(1.0)
        self.assertFalse(payload["monitor"])

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


class SpectrumPublishingTests(unittest.TestCase):
    """The spectrum goes out only while somebody is looking at it.

    241 bins of float at 1 Hz is roughly 150 MB a day on a link that is metered
    and shared with video, for a display that is open for minutes at
    commissioning. So it is demand-driven, the same shape as the camera
    preview: a station nobody is watching sends nothing at all.
    """

    def controller(self):
        from gsu.radio.receiver import RadioController
        from gsu.radio.simulated import SimulatedFrontEnd
        return RadioController(SimulatedFrontEnd())

    def frame(self, radio, ticks=3):
        payload = None
        for _ in range(ticks):
            payload, _ = radio.tick(1.0)
        return payload

    def test_nobody_watching_costs_nothing(self):
        payload = self.frame(self.controller())
        self.assertNotIn("spectrum", payload)
        self.assertNotIn("span_hz", payload)

    def test_asking_for_it_produces_it(self):
        radio = self.controller()
        radio.want_spectrum(True)
        payload = self.frame(radio)
        self.assertIn("spectrum", payload)
        self.assertIn("span_hz", payload)
        self.assertTrue(payload["spectrum"])

    def test_it_stops_when_the_window_lapses(self):
        # Re-requested rather than held open: a console that crashes or a lid
        # that closes stops the traffic without sending a goodbye.
        from gsu.radio import receiver
        radio = self.controller()
        radio.want_spectrum(True)
        self.assertIn("spectrum", self.frame(radio))
        with mock.patch.object(
            receiver.time, "monotonic",
            return_value=time.monotonic() + receiver.SPECTRUM_WINDOW_S + 1,
        ):
            self.assertNotIn("spectrum", self.frame(radio, ticks=1))

    def test_turning_it_off_is_immediate(self):
        radio = self.controller()
        radio.want_spectrum(True)
        self.assertIn("spectrum", self.frame(radio))
        radio.want_spectrum(False)
        self.assertNotIn("spectrum", self.frame(radio, ticks=1))

    def test_it_is_bounded_and_integral(self):
        from gsu.radio.receiver import SPECTRUM_BINS
        radio = self.controller()
        radio.want_spectrum(True)
        spectrum = self.frame(radio)["spectrum"]
        self.assertLessEqual(len(spectrum), SPECTRUM_BINS)
        for value in spectrum:
            self.assertIsInstance(value, int)

    def test_decimation_keeps_peaks_not_averages(self):
        # An airband channel is 25 kHz — a handful of 500 Hz bins — and
        # averaging it with the quiet either side buries the one feature this
        # display exists to show.
        from gsu.radio.receiver import _decimate_db
        bins = [-100.0] * 40
        bins[17] = -30.0
        out = _decimate_db(bins, 8)
        self.assertEqual(len(out), 8)
        self.assertIn(-30, out, "the carrier must survive decimation")

    def test_decimation_leaves_a_short_spectrum_alone(self):
        from gsu.radio.receiver import _decimate_db
        self.assertEqual(_decimate_db([-1.4, -2.6], 128), [-1, -3])
        self.assertEqual(_decimate_db([], 128), [])
