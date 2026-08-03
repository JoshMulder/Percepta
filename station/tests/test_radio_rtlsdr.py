"""The RTL-SDR front end, against a fake dongle.

No hardware here, and none needed for what these check: that the front end
holds up its end of `receiver.RadioFrontEnd`, that it reports failures rather
than raising them, and — the one with teeth — that **exactly one second of
audio leaves the station per second of wall clock** however raggedly the
dongle delivers samples. The platform's player stutters on anything else, and
the simulator's 27 ms/s drift was a real bug found the hard way.

What is *not* checked here is anything about a real RTL2832U: that it
enumerates, tunes where it says it does, or streams without wedging. Those need
the bench and are listed as such in the handover notes.
"""

import math
import unittest

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

if np is not None:
    from gsu.radio import rtl2832, rtlsdr
    from gsu.radio.receiver import RadioController
    from gsu.radio.rtlsdr import N_FFT, OFFSET_HZ, SAMPLE_RATE, RtlSdrFrontEnd
else:  # pragma: no cover - the class bodies below are read at import time
    N_FFT = OFFSET_HZ = SAMPLE_RATE = 0


class FakeDongle:
    """An `rtl2832.RtlDevice` that makes up samples instead of reading USB."""

    def __init__(self, sample_rate=SAMPLE_RATE, offset_hz=OFFSET_HZ):
        self.sample_rate = sample_rate
        self.offset_hz = offset_hz
        self.serial_hint = ""
        self.model, self.tuner, self.serial = "RTL2838", "R820T", "00000001"
        self.gains = [0.0, 12.5, 25.4, 37.2, 49.6]
        self.dropped_blocks = 0
        self.read_error = ""
        self.is_open = False
        self.closed = False
        self.freq_hz = 0
        self.gain = None
        self.ppm = None
        self.flushed = 0
        self.streaming = False
        self.open_error: Exception | None = None
        #: Fraction of a second of IQ each `drain()` hands over. The dongle's
        #: crystal and the station's tick are not the same clock.
        self.seconds_per_drain = 1.0
        self.transmitting = False
        self.noise_power = 1e-6
        self._phase = 0

    def open(self, freq_hz, gain, ppm):
        if self.open_error is not None:
            raise self.open_error
        self.is_open = True
        self.freq_hz, self.gain, self.ppm = freq_hz, gain, ppm

    def start_stream(self):
        self.streaming = True

    def close(self):
        self.is_open = False
        self.streaming = False
        self.closed = True

    def set_freq(self, freq_hz):
        self.freq_hz = freq_hz
        self.flushed += 1

    def set_gain(self, gain):
        self.gain = gain

    def set_ppm(self, ppm):
        self.ppm = ppm

    def applied_gain(self):
        return 37.2 if self.gain != "auto" else None

    def drain(self):
        count = int(self.seconds_per_drain * self.sample_rate)
        rng = np.random.default_rng(abs(hash((self._phase, count))) % (2**32))
        self._phase += 1
        sigma = math.sqrt(self.noise_power / 2.0)
        samples = rng.normal(0, sigma, count) + 1j * rng.normal(0, sigma, count)
        if self.transmitting:
            in_channel = self.noise_power * 15_000.0 / self.sample_rate
            amplitude = math.sqrt(in_channel * 10 ** (25.0 / 10.0))
            steps = np.arange(count)
            envelope = amplitude * (
                1.0 + 0.6 * np.cos(2 * np.pi * 1000.0 * steps / self.sample_rate)
            )
            samples = samples + envelope * np.exp(
                2j * np.pi * -self.offset_hz * steps / self.sample_rate
            )
        return samples.astype(np.complex64)


def open_now(front_end):
    """Drive the background open to completion, for a deterministic test."""
    front_end._ensure_open()
    thread = front_end._opening
    if thread is not None:
        thread.join(timeout=5.0)


@unittest.skipIf(np is None, "numpy is not installed")
class FrontEndTests(unittest.TestCase):
    def setUp(self):
        self.dongles = []
        self.original = rtl2832.RtlDevice

        def factory(sample_rate=SAMPLE_RATE, offset_hz=OFFSET_HZ):
            dongle = FakeDongle(sample_rate, offset_hz)
            dongle.open_error = self.open_error
            self.dongles.append(dongle)
            return dongle

        self.open_error = None
        rtl2832.RtlDevice = factory
        self.addCleanup(setattr, rtl2832, "RtlDevice", self.original)

    def make(self, **kwargs):
        front_end = RtlSdrFrontEnd(**kwargs)
        self.addCleanup(front_end.shutdown)
        return front_end

    # --- the protocol ---------------------------------------------------

    def test_it_is_a_radio_front_end(self):
        from gsu.radio.receiver import RadioFrontEnd

        self.assertIsInstance(self.make(), RadioFrontEnd)

    def test_transmit_is_not_offered(self):
        self.assertFalse(self.make().tx_capable)

    def test_a_short_block_still_measures_the_channel(self):
        """One snapshot is a real measurement, not a degraded one."""
        from gsu.radio import dsp

        front_end = self.make()
        front_end.read(1.0)
        block = front_end.read(0.125)
        self.assertEqual(len(block.spectrum_db), 4096)
        # Refuses if the spectrum cannot show the measurement window, so this
        # passing is the check that a 125 ms block is still squelchable.
        self.assertIsInstance(
            dsp.noise_floor_db(block.spectrum_db, block.bin_hz), float)

    def test_the_spectrum_reaches_the_measurement_window(self):
        """`dsp.noise_floor_db` refuses a spectrum that cannot show 15–50 kHz
        either side, and rightly. This one must not be refused."""
        from gsu.radio import dsp

        front_end = self.make()
        open_now(front_end)
        block = front_end.read(1.0)
        self.assertEqual(len(block.spectrum_db), N_FFT)
        self.assertAlmostEqual(block.bin_hz, SAMPLE_RATE / N_FFT)
        dsp.noise_floor_db(block.spectrum_db, block.bin_hz)  # must not raise

    # --- pacing, which is the one that matters --------------------------

    def test_exactly_one_second_of_audio_per_second(self):
        front_end = self.make()
        open_now(front_end)
        for _ in range(30):
            front_end.read(1.0)
            self.assertEqual(len(front_end.demodulate(24_000)), 24_000)

    def test_a_fast_dongle_does_not_build_a_backlog(self):
        """The dongle's clock runs a little fast. Audio must stay current: the
        tail is capped and the oldest dropped, never queued up into a growing
        delay."""
        front_end = self.make()
        open_now(front_end)
        self.dongles[0].seconds_per_drain = 1.05
        for _ in range(40):
            front_end.read(1.0)
            self.assertEqual(len(front_end.demodulate(24_000)), 24_000)
        pending = front_end._pending
        self.assertIsNotNone(pending)
        self.assertLessEqual(
            pending.size, rtlsdr.MAX_PENDING_AUDIO_S * 24_000,
            "held-over audio must be capped, not accumulated",
        )

    def test_a_slow_dongle_still_yields_a_full_second(self):
        front_end = self.make()
        open_now(front_end)
        self.dongles[0].seconds_per_drain = 0.94
        for _ in range(20):
            front_end.read(1.0)
            self.assertEqual(len(front_end.demodulate(24_000)), 24_000)
        self.assertGreater(
            front_end._underruns, 0, "a short block should be counted, not hidden"
        )

    def test_audio_before_anything_has_been_read_is_silence(self):
        front_end = self.make()
        self.assertEqual(front_end.demodulate(24_000), [0.0] * 24_000)

    # --- squelch, end to end through the controller ---------------------

    def test_a_quiet_channel_sends_no_audio(self):
        front_end = self.make()
        open_now(front_end)
        controller = RadioController(front_end)
        payload, audio = controller.tick(1.0)
        self.assertFalse(payload["squelch_open"])
        self.assertIsNone(audio, "silence must not be uplinked")

    def test_a_transmission_opens_the_gate_and_sends_audio(self):
        front_end = self.make()
        open_now(front_end)
        controller = RadioController(front_end)
        controller.tick(1.0)
        self.dongles[0].transmitting = True
        payload, audio = controller.tick(1.0)
        self.assertTrue(payload["squelch_open"])
        self.assertIsNotNone(audio)
        self.assertEqual(payload["kind"], "radio")
        self.assertFalse(payload["tx_capable"])

    def test_the_gate_shuts_again_when_the_over_ends(self):
        front_end = self.make()
        open_now(front_end)
        controller = RadioController(front_end)
        self.dongles[0].transmitting = True
        controller.tick(1.0)
        self.dongles[0].transmitting = False
        for _ in range(4):  # ride out the hang
            payload, audio = controller.tick(1.0)
        self.assertFalse(payload["squelch_open"])
        self.assertIsNone(audio)

    def test_the_gain_table_comes_from_the_tuner(self):
        front_end = self.make()
        open_now(front_end)
        self.assertEqual(front_end.available_gains, [0.0, 12.5, 25.4, 37.2, 49.6])

    # --- tuning ----------------------------------------------------------

    def test_tuning_flushes_what_was_buffered(self):
        """Samples from the old frequency measured against the new channel
        would report one tick of somewhere else."""
        front_end = self.make()
        open_now(front_end)
        front_end.read(1.0)
        before = self.dongles[0].flushed
        front_end.tune(121_500_000)
        self.assertEqual(self.dongles[0].freq_hz, 121_500_000)
        self.assertGreater(self.dongles[0].flushed, before)
        self.assertIsNone(front_end._samples)

    def test_tuning_before_the_dongle_opens_is_remembered(self):
        front_end = self.make(gain=25.4, ppm=12)
        front_end.tune(119_100_000)
        open_now(front_end)
        self.assertEqual(self.dongles[0].freq_hz, 119_100_000)
        self.assertEqual(self.dongles[0].ppm, 12)

    def test_gain_is_reported_as_the_tuner_applied_it(self):
        front_end = self.make(gain=26.0)
        open_now(front_end)
        self.assertEqual(front_end.gain, 37.2)  # the fake snaps to its own step

    def test_auto_gain_is_accepted_but_not_the_default(self):
        self.assertEqual(self.make().gain, 37.2)
        front_end = self.make(gain="auto")
        open_now(front_end)
        self.assertEqual(front_end.gain, "auto")

    def test_a_nonsense_gain_falls_back_rather_than_raising(self):
        self.assertEqual(self.make(gain="rather a lot").gain, 37.2)

    def test_the_allocated_tuner_is_the_one_opened(self):
        """The registry allocates a dongle by serial because airband and
        1090 MHz cannot share one."""
        front_end = self.make(resource="rtlsdr:00000123")
        open_now(front_end)
        self.assertEqual(self.dongles[0].serial_hint, "00000123")

    def test_an_unprogrammed_dongle_has_no_serial_to_open_by(self):
        front_end = self.make(resource="rtlsdr:unprogrammed@1-1.3")
        open_now(front_end)
        self.assertEqual(self.dongles[0].serial_hint, "")

    # --- failure ---------------------------------------------------------

    def test_a_dongle_that_will_not_open_is_reported_not_raised(self):
        self.open_error = rtl2832.RtlError("librtlsdr is not installed")
        front_end = self.make()
        block = front_end.read(1.0)  # must not raise
        open_now(front_end)
        front_end.read(1.0)
        self.assertIn("librtlsdr", front_end.unavailable_reason)
        self.assertEqual(len(block.spectrum_db), N_FFT)

    def test_a_dead_receiver_keeps_the_gate_shut(self):
        """No audio may be uplinked from a receiver that is not receiving."""
        self.open_error = rtl2832.RtlError("no RTL-SDR device found")
        front_end = self.make()
        controller = RadioController(front_end)
        for _ in range(3):
            payload, audio = controller.tick(1.0)
        self.assertFalse(payload["squelch_open"])
        self.assertIsNone(audio)

    def test_a_receiver_that_never_opened_is_absent_not_silent(self):
        # `silent` is a device that is open and hearing nothing — a quiet
        # channel. With no device open at all the status used to return `silent`
        # too, so a disconnected dongle read as present and the station
        # published a dead noise floor as a live reading. `absent` is the honest
        # answer, and it is what makes the slot report the radio unavailable.
        self.open_error = rtl2832.RtlError("no RTL-SDR device found")
        front_end = self.make()
        front_end._next_attempt = 0.0
        open_now(front_end)                     # one attempt, below the fail count
        self.assertLess(front_end._failures, rtlsdr.FAILURES_BEFORE_FAILED)
        self.assertEqual(front_end.status, "absent")
        self.assertFalse(front_end.describe().present)

    def test_repeated_failures_report_the_slot_as_failed(self):
        self.open_error = rtl2832.RtlError("no RTL-SDR device found")
        front_end = self.make()
        for _ in range(rtlsdr.FAILURES_BEFORE_FAILED):
            front_end._next_attempt = 0.0
            open_now(front_end)
        self.assertEqual(front_end.status, "failed")
        self.assertFalse(front_end.describe().present)

    def test_a_failed_open_backs_off(self):
        """An unattended box must not hammer a USB device once a second."""
        self.open_error = rtl2832.RtlError("no RTL-SDR device found")
        front_end = self.make()
        open_now(front_end)
        attempts = len(self.dongles)
        for _ in range(5):
            front_end.read(1.0)
        self.assertEqual(len(self.dongles), attempts, "should be backing off")

    def test_a_stream_that_dies_is_torn_down_and_reported(self):
        front_end = self.make()
        open_now(front_end)
        front_end.read(1.0)
        self.dongles[0].read_error = "the dongle stopped delivering samples"
        block = front_end.read(1.0)
        self.assertTrue(self.dongles[0].closed)
        self.assertIn("stopped delivering", front_end.unavailable_reason)
        self.assertEqual(len(block.spectrum_db), N_FFT)

    def test_describe_names_the_hardware_once_it_is_open(self):
        front_end = self.make()
        open_now(front_end)
        front_end.read(1.0)
        device = front_end.describe()
        self.assertTrue(device.present)
        self.assertFalse(device.simulated)
        self.assertIn("R820T", device.detail)
        self.assertLessEqual(len(device.detail), 200)

    # --- shutdown --------------------------------------------------------

    def test_shutdown_closes_the_dongle(self):
        """The obligation: a dongle killed mid-transfer needs a physical
        replug, so every shutdown path has to reach `close()`."""
        front_end = self.make()
        open_now(front_end)
        front_end.shutdown()
        self.assertTrue(self.dongles[0].closed)

    def test_shutdown_during_an_open_does_not_leak_the_dongle(self):
        front_end = self.make()
        front_end._ensure_open()
        front_end.shutdown()
        thread = front_end._opening
        if thread is not None:
            thread.join(timeout=5.0)
        self.assertTrue(all(dongle.closed or not dongle.is_open
                            for dongle in self.dongles))

    def test_the_controller_reaches_the_hardware_on_the_way_out(self):
        front_end = self.make()
        open_now(front_end)
        RadioController(front_end).shutdown()
        self.assertTrue(self.dongles[0].closed)


@unittest.skipIf(np is None, "numpy is not installed")
class BenchCommandTests(unittest.TestCase):
    """`python -m gsu radio`, which is the tool the bench session runs on.

    Worth testing precisely because it is the thing nobody can debug while
    standing at the hardware: a crash here costs a trip, and the WAV it writes
    is the artefact the whole exercise produces.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        from gsu.agent import Agent
        from gsu.config import AgentConfig
        from gsu.devices.inventory import Resource

        self.dongle = FakeDongle()
        self.original = rtl2832.RtlDevice
        rtl2832.RtlDevice = lambda *a, **k: self.dongle
        self.addCleanup(setattr, rtl2832, "RtlDevice", self.original)

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.out = str(Path(self._dir.name) / "radio.wav")
        self.agent = Agent(AgentConfig(
            home=Path(self._dir.name), setup_enabled=False, single_instance=False, demo=True))
        self.agent.inventory.resources = lambda: [
            Resource(id="rtlsdr:00000001", kind="rtlsdr", serial="00000001",
                     model="RTL2838", detail="")
        ]

    def run_command(self, **kwargs):
        import contextlib
        import io

        from gsu.__main__ import _radio

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = _radio(self.agent, kwargs.pop("freq", 118.7),
                          kwargs.pop("seconds", 2.0), self.out,
                          kwargs.pop("monitor", False), kwargs.pop("gain", None),
                          kwargs.pop("ppm", None))
        return code, buffer.getvalue()

    def test_it_refuses_to_answer_with_the_simulator(self):
        """The default inventory fits the simulated receiver. Using it here
        would produce synthesised speech and a moving meter — the most
        misleading possible answer to "does the dongle work"."""
        code, output = self.run_command(monitor=True)
        self.assertEqual(code, 0, output)
        self.assertIn("the configured receiver is the simulator", output)
        self.assertTrue(self.dongle.is_open or self.dongle.closed)

    def test_it_records_a_playable_wav_with_monitor_held(self):
        import wave

        code, output = self.run_command(monitor=True, seconds=3.0)
        self.assertEqual(code, 0, output)
        with wave.open(self.out, "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 24_000)
            frames = handle.getnframes()
        # Exactly one second of audio per open tick, which is the contract the
        # command exists to check. Nothing else in the suite checks it from the
        # WAV that a person will actually listen to.
        self.assertEqual(frames % 24_000, 0, "audio must be whole seconds")
        self.assertGreater(frames, 0)
        self.assertNotIn("WRONG", output)

    def test_it_tunes_where_it_was_told(self):
        code, output = self.run_command(freq=121.5, seconds=2.0)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.dongle.freq_hz, 121_500_000)

    def test_a_quiet_channel_says_so_rather_than_writing_nothing(self):
        code, output = self.run_command(seconds=2.0)
        self.assertEqual(code, 0, output)
        self.assertIn("gate never opened", output)
        self.assertIn("--monitor", output)

    def test_no_dongle_on_the_bus_stops_before_anything_else(self):
        self.agent.inventory.resources = lambda: []
        code, output = self.run_command()
        self.assertEqual(code, 1)
        self.assertIn("dmesg", output)

    def test_the_dongle_is_released_at_the_end(self):
        code, output = self.run_command(monitor=True)
        self.assertEqual(code, 0, output)
        self.assertTrue(self.dongle.closed, "a dongle left open wedges on exit")


@unittest.skipIf(np is None, "numpy is not installed")
class RegistryTests(unittest.TestCase):
    def test_the_airband_entry_names_this_driver(self):
        from gsu.devices import registry

        entry = registry.get("rtlsdr-airband")
        self.assertEqual(entry.driver, "gsu.radio.rtlsdr:RtlSdrFrontEnd")

    def test_the_driver_takes_the_parameters_the_registry_declares(self):
        """`_instantiate` filters by signature, so a parameter the constructor
        does not accept is silently dropped rather than applied."""
        import inspect

        from gsu.devices import registry

        entry = registry.get("rtlsdr-airband")
        accepted = inspect.signature(RtlSdrFrontEnd).parameters
        for parameter in entry.parameters:
            self.assertIn(parameter.name, accepted)

    def test_transmit_is_declared_absent(self):
        from gsu.devices import registry

        self.assertIn("tx", registry.get("rtlsdr-airband").absent)


if __name__ == "__main__":
    unittest.main()


class SnapshotCadenceTests(unittest.TestCase):
    """Deliberately outside the numpy-gated class above.

    `snapshots_for` is arithmetic over a float and the fix hangs off it, so it
    has to be checked on every box the suite runs on rather than only where a
    dongle library happens to be installed.
    """

    def test_snapshots_follow_the_block_length(self):
        """A fixed count was right at 1 Hz and wrong at the deployed 125 ms.

        `read()` used to take four periodograms whatever `dt` was, and the agent
        started calling it eight times a second — so all four landed inside one
        125 ms window, covering ground the first already covered, at 32 FFTs a
        second. The count has to follow the interval, because what it is for is
        covering the interval.
        """
        from gsu.radio.rtlsdr import SNAPSHOT_SPAN_S, snapshots_for

        self.assertEqual(snapshots_for(1.0), 4)          # unchanged at 1 Hz
        self.assertEqual(snapshots_for(0.125), 1)        # the deployed sub-tick
        self.assertEqual(snapshots_for(0.5), 2)
        self.assertEqual(snapshots_for(2.0), 8)
        # Never zero, however short the block: a tick with no measurement at
        # all would leave the squelch deciding on a stale spectrum.
        for tiny in (0.0, 0.001, 0.01):
            self.assertEqual(snapshots_for(tiny), 1, tiny)
        # The rate per *second* never drops below the original design intent.
        for dt in (0.125, 0.25, 0.5, 1.0):
            self.assertGreaterEqual(snapshots_for(dt) / dt, 1 / SNAPSHOT_SPAN_S, dt)
