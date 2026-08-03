"""The RTL-SDR airband front end: the `radio` slot's hardware.

This is the `receiver.RadioFrontEnd` the `rtlsdr-airband` registry entry has been
promising since the first deployment. It tunes, it hands over a spectrum, and it
demodulates. It does **not** decide whether anyone hears anything —
`receiver.RadioController` owns the gate, the AUTO threshold and the hang, and
it owns them for both this and `simulated.SimulatedFrontEnd` so the two cannot
drift apart.

Where the work happens, and why there:

    reader thread   `rtl2832.RtlDevice` streams continuously into a bounded
    (rtl2832.py)    buffer, dropping the oldest when the tick loop falls behind.
                    Continuous because `read()` is called every tick whether or
                    not anyone is listening, and because starting and stopping
                    a USB transfer at 1 Hz is how a dongle gets wedged.

    read()          Four 4096-point periodograms out of the last second, the
    every tick      busiest handed over. Cheap: four FFTs, some tens of
                    milliseconds even on a Pi 2B.

    demodulate()    Mix, filter, decimate, envelope-detect. Around six million
    only when the   multiply-accumulates a second, and the only expensive thing
    gate is open    in the receiver. Airband is quiet most of the time, so most
                    ticks never run it at all — which is the same property that
                    keeps the uplink cheap, arrived at from the CPU side.

**What goes up when the gate is shut: nothing.** No audio message at all, not a
frame of silence. `radio` telemetry continues at 1 Hz with `squelch_open: false`
so the console's meter stays live, and that is roughly 200 bytes a second
against the ~48 kB/s an open gate costs. On a metered satellite link that
difference is the reason this receiver is built the way it is.

**Opening the dongle happens on its own thread.** `librtlsdr`'s open enumerates
USB and initialises the tuner, which takes a good fraction of a second and takes
longer when it fails. The sensing loop must not block, so `read()` starts an
open in the background and reports a dead-quiet spectrum until it succeeds.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import time

from ..sensors import Device
from . import dsp
from .audio import AUDIO_RATE
from .receiver import Block

log = logging.getLogger("gsu.radio")

#: 240 000 divides the 28.8 MHz reference exactly and decimates to 24 kHz by a
#: round 10. `am.py` has the full argument.
SAMPLE_RATE = 240_000

#: Tune this far above the channel so the dongle's DC spike lands outside both
#: the channel and the 15–50 kHz noise window. 60 kHz is a quarter of the sample
#: rate, which makes the mixer a four-entry table.
OFFSET_HZ = 60_000

#: 4096 bins over 240 kHz is 58.59 Hz each, which puts about 1190 bins in the
#: noise measurement window. The floor is a median over those, single-look, and
#: that many samples makes it good to under 0.2 dB. See `am.spectrum_dbfs`.
N_FFT = 4096
BIN_HZ = SAMPLE_RATE / N_FFT

#: How much of a block one periodogram is taken to cover. The one with the most
#: in-channel power is the one handed over: a single snapshot of the last 17 ms
#: would miss a transmission that ended earlier in the block and close the gate
#: on a live over. A look every 250 ms is enough for airband, where an over
#: lasts seconds, and the upward bias it puts on a noise-only reading is a few
#: tenths of a dB against an 8 dB AUTO margin.
#:
#: **Derived from the block length rather than fixed, because the block length
#: changed underneath it.** This was `SNAPSHOTS = 4`, written when `read()` was
#: called once a second. The 125 ms audio sub-tick then started calling it eight
#: times a second and the four were never revisited — so the station took 32
#: periodograms a second, all four of each set landing inside the same 125 ms
#: window, to cover ground one of them already covered. Measured: 15.9 ms/s of
#: FFT against 3.6 ms/s for this, and that is on a desktop, with the squelch
#: shut and nobody listening.
SNAPSHOT_SPAN_S = 0.25


def snapshots_for(seconds: float) -> int:
    """Periodograms worth taking for a block of this length. At least one."""
    return max(1, round(seconds / SNAPSHOT_SPAN_S))

#: What the console is shown before the tuner has been asked what it can do.
#: An R820T2's table; the fitted tuner replaces it at open.
NOMINAL_GAINS = [0.0, 9.0, 14.4, 27.7, 37.2, 42.1, 43.4, 49.6]

#: Per-bin level reported while there is no working receiver. Quiet enough that
#: the gate cannot open on it, and finite so the arithmetic above stays sane.
#: The reason the receiver is not working travels separately, in `describe()`
#: and the slot report — a number cannot carry it and must not try.
DEAD_BIN_DBFS = -140.0

#: How long to leave a dongle that would not open before trying again. An
#: unattended box must not hammer a USB device once a second for a week.
RETRY_SECONDS = 15.0

#: Failures in a row before the slot reports failed rather than merely silent.
FAILURES_BEFORE_FAILED = 3

#: Audio held over between ticks before the oldest is dropped. The link wants
#: the current over, not a complete one — `contract/transport.md`.
MAX_PENDING_AUDIO_S = 0.5


class RtlSdrFrontEnd:
    """An RTL2832U tuned to the airband, receiving only."""

    #: Receive only, structurally. There is no transmit path in this station and
    #: `radio.transmit` is not a registered command — see `radio/__init__.py`.
    tx_capable = False

    def __init__(
        self,
        gain: float | str = 37.2,
        ppm: int = 0,
        resource: str = "",
    ) -> None:
        self.gain = self._coerce_gain(gain)
        self.ppm = self._coerce_int(ppm)
        #: The inventory allocates a tuner by serial number, as `rtlsdr:<serial>`.
        self.serial_hint = resource.split(":", 1)[1] if ":" in resource else ""
        if self.serial_hint.startswith("unprogrammed@"):
            # No serial to open by. The setup page already warns that such a
            # dongle is indistinguishable from another identical one.
            self.serial_hint = ""

        self.available_gains = list(NOMINAL_GAINS)
        self.freq_hz = 118_700_000

        # Cheap checks only: this constructor runs inside a sensing tick.
        self._numpy = importlib.util.find_spec("numpy") is not None
        self._reason = "" if self._numpy else (
            "numpy is not installed, and the receiver cannot demodulate without "
            "it — 240 000 samples a second is not something CPython can filter "
            "in real time. `pip install numpy` in the station's virtual "
            "environment, or `apt install python3-numpy` and recreate it with "
            "--system-site-packages."
        )

        self._device = None            # rtl2832.RtlDevice, once one is open
        self._demod = None             # am.AmDemodulator
        self._am = None                # the am module, imported lazily
        self._lock = threading.Lock()
        self._opening: threading.Thread | None = None
        self._closing = False
        self._failures = 0
        self._next_attempt = 0.0

        self._samples = None           # this tick's IQ, awaiting demodulation
        self._pending = None           # audio carried over between ticks
        self._demodulated_last_tick = False
        self._blocks = 0
        self._underruns = 0
        self._last_spectrum: list[float] | None = None

    # --- reporting --------------------------------------------------------

    @property
    def status(self) -> str:
        """`absent`, `failed`, `streaming` or `silent`, for the slot report.

        **`silent` means a device that is open and hearing nothing; `absent`
        means no device at all.** They read the same in dead air and are
        completely different faults, and conflating them is how a disconnected
        dongle reported itself present: with no device open this returned
        `silent` — the quiet-channel state — so the slot showed as fitted and
        the station published a dead noise floor as if it were a live reading.
        A receiver on a quiet channel is silent; an unplugged one is absent.
        """
        if not self._numpy:
            return "absent"
        if self._failures >= FAILURES_BEFORE_FAILED:
            # Tried repeatedly and given up — distinct from not-yet-tried, and
            # the slot report reads them the same ("configured_absent"), but a
            # human wants to know which. Checked before the device below because
            # a failed open leaves the device None too.
            return "failed"
        if self._device is None or not self._device.is_open:
            return "absent"
        return "streaming" if self._blocks else "silent"

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    def describe(self) -> Device:
        device = self._device
        if device is None or not device.is_open:
            return Device(
                id="radio", kind="airband-receiver", present=False,
                detail=(self._reason or "opening the RTL-SDR…")[:200],
                simulated=False,
            )
        detail = (
            f"{device.model or 'RTL2832U'} ({device.tuner} tuner"
            f"{', serial ' + device.serial if device.serial else ''}) on "
            f"{self.freq_hz / 1e6:.3f} MHz, {SAMPLE_RATE // 1000} ksps, "
            f"gain {self.gain}, ppm {self.ppm}"
        )
        if device.dropped_blocks or self._underruns:
            # Said on every line rather than logged once: a receiver that is
            # dropping samples still sounds like a receiver, and the number is
            # what tells somebody the box is not keeping up.
            detail += (
                f" — {device.dropped_blocks} block(s) dropped, "
                f"{self._underruns} audio underrun(s)"
            )
        elif self._reason:
            detail += f" — {self._reason}"
        return Device(
            id="radio", kind="airband-receiver", present=True,
            detail=detail[:200], simulated=False,
        )

    # --- tuning -----------------------------------------------------------

    def tune(self, freq_hz: int) -> None:
        self.freq_hz = int(freq_hz)
        device = self._device
        if device is not None and device.is_open:
            try:
                device.set_freq(self.freq_hz)
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                self._reason = f"could not retune: {exc}"[:200]
                log.warning("Retune to %.3f MHz failed.", freq_hz / 1e6, exc_info=True)
        # Whether or not the hardware took it: the filter's memory and the AGC's
        # carrier both describe the channel we just left.
        self._samples = None
        self._pending = None
        if self._demod is not None:
            self._demod.reset()

    def set_gain(self, gain: float | str) -> None:
        self.gain = self._coerce_gain(gain)
        device = self._device
        if device is not None and device.is_open:
            try:
                device.set_gain(self.gain)
                applied = device.applied_gain()
                if applied is not None and self.gain != "auto":
                    # The tuner snaps to a step in its own table. Report what it
                    # did, not what it was asked for.
                    self.gain = applied
            except Exception as exc:  # noqa: BLE001
                self._reason = f"could not set gain: {exc}"[:200]

    def set_ppm(self, ppm: int) -> None:
        self.ppm = self._coerce_int(ppm)
        device = self._device
        if device is not None and device.is_open:
            try:
                device.set_ppm(self.ppm)
                device.set_freq(self.freq_hz)  # so the correction takes now
            except Exception as exc:  # noqa: BLE001
                self._reason = f"could not set ppm: {exc}"[:200]

    # --- measurement ------------------------------------------------------

    def read(self, seconds: float) -> Block:
        """The spectrum for this tick, centred on the tuned channel."""
        self._ensure_open()
        device = self._device
        if device is None or not device.is_open:
            return self._dead_block(seconds)

        if device.read_error:
            self._fail(device.read_error)
            self._teardown()
            return self._dead_block(seconds)

        samples = device.drain()
        if samples.size < N_FFT:
            # The stream has not delivered a full snapshot yet — normal for the
            # first tick after opening, and not a failure. Repeat the last
            # measurement rather than inventing a quiet one, which would drop
            # the gate in the middle of an over.
            self._samples = None
            return Block(
                spectrum_db=list(self._last_spectrum or self._dead_spectrum()),
                bin_hz=BIN_HZ, seconds=seconds,
            )

        self._samples = samples
        self._blocks += 1
        self._failures = 0
        self._reason = ""
        spectrum = self._best_snapshot(samples, snapshots_for(seconds))
        self._last_spectrum = spectrum
        return Block(spectrum_db=spectrum, bin_hz=BIN_HZ, seconds=seconds)

    def _best_snapshot(self, samples, snapshots: int) -> list[float]:
        """Of several periodograms across the block, the one with the most
        in-channel power.

        Selecting on in-channel power does not bias the noise floor: the floor
        is a median of bins 15–50 kHz out, and those are statistically
        independent of the bins inside the channel. So this catches a
        transmission anywhere in the second without touching the measurement
        rule 3 is about.
        """
        am = self._am
        span = max(1, samples.size - N_FFT)
        best: list[float] | None = None
        best_power = float("-inf")
        for index in range(snapshots):
            start = (span * index) // snapshots
            spectrum = am.spectrum_dbfs(
                samples[start : start + N_FFT], N_FFT, SAMPLE_RATE, OFFSET_HZ
            )
            power = am.in_channel_power_db(spectrum, BIN_HZ, dsp.CHANNEL_HALF_HZ)
            if power > best_power:
                best_power, best = power, spectrum.tolist()
        return best or self._dead_spectrum()

    # --- audio ------------------------------------------------------------

    def demodulate(self, samples: int) -> list[float]:
        """Exactly `samples` of audio for the block just read.

        Exactly, because the controller asks for one second's worth every second
        and the platform's player stutters on anything else — the simulator's
        27 ms/s drift was a real bug. The dongle's clock and the station's tick
        are not the same clock, so the difference has to go somewhere: a short
        block is padded and counted as an underrun, a long one leaves its tail
        for the next tick, and a tail that grows past half a second is dropped
        from the front. Current beats complete on this link.
        """
        if samples <= 0:
            return []
        demod = self._demod
        if demod is None or self._samples is None:
            self._demodulated_last_tick = False
            return [0.0] * samples

        try:
            fade_in = not self._demodulated_last_tick
            if fade_in:
                # The gate was shut last tick, so the filter holds samples from
                # before the gap and the AGC holds a carrier that has gone.
                demod.reset()
            audio = demod.process(self._samples, fade_in=fade_in)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            log.exception("Demodulation failed.")
            self._reason = f"demodulation failed: {exc}"[:200]
            self._demodulated_last_tick = False
            return [0.0] * samples
        finally:
            self._samples = None

        return self._paced(audio, samples)

    def _paced(self, audio, wanted: int) -> list[float]:
        numpy_module = self._am.np
        if self._pending is not None and self._pending.size:
            audio = numpy_module.concatenate([self._pending, audio])
        if audio.size < wanted:
            self._underruns += 1
            block = numpy_module.zeros(wanted, dtype=audio.dtype)
            block[: audio.size] = audio
            self._pending = None
        else:
            block = audio[:wanted]
            tail = audio[wanted:]
            limit = int(MAX_PENDING_AUDIO_S * AUDIO_RATE)
            self._pending = tail[-limit:] if tail.size > limit else tail
        self._demodulated_last_tick = True
        return block.tolist()

    # --- lifecycle --------------------------------------------------------

    def _ensure_open(self) -> None:
        """Start an open in the background if one is due. Never blocks."""
        if self._closing or not self._numpy:
            return
        with self._lock:
            device = self._device
            if device is not None and device.is_open:
                return
            if self._opening is not None and self._opening.is_alive():
                return
            if time.monotonic() < self._next_attempt:
                return
            self._opening = threading.Thread(
                target=self._open, name="gsu-rtlsdr-open", daemon=True
            )
            self._opening.start()

    def _open(self) -> None:
        try:
            from . import am, rtl2832  # noqa: PLC0415 - deliberately lazy

            device = rtl2832.RtlDevice(SAMPLE_RATE, OFFSET_HZ)
            device.serial_hint = self.serial_hint
            device.open(self.freq_hz, self.gain, self.ppm)
            applied = device.applied_gain()
            if applied is not None and self.gain != "auto":
                self.gain = applied
            if device.gains:
                self.available_gains = device.gains
            device.start_stream()
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            self._fail(str(exc))
            return

        with self._lock:
            if self._closing:
                # shutdown() ran while this was opening. Hand the dongle back
                # rather than leaving a stream nobody will ever stop.
                device.close()
                return
            self._am = am
            self._demod = am.AmDemodulator(SAMPLE_RATE, AUDIO_RATE, OFFSET_HZ)
            self._device = device
            self._failures = 0
            self._reason = ""

    def _fail(self, reason: str) -> None:
        self._failures += 1
        self._reason = reason[:200]
        self._next_attempt = time.monotonic() + RETRY_SECONDS
        if self._failures == FAILURES_BEFORE_FAILED:
            log.error(
                "The RTL-SDR has failed %d times in a row: %s Retrying every "
                "%.0fs; the radio slot is publishing a closed squelch meanwhile.",
                self._failures, self._reason, RETRY_SECONDS,
            )

    def _teardown(self) -> None:
        with self._lock:
            device, self._device = self._device, None
        if device is not None:
            device.close()
        self._demod = None
        self._samples = None
        self._pending = None

    def shutdown(self) -> None:
        """Stop the receiver gracefully. Never with a signal.

        The obligation `receiver.RadioFrontEnd.shutdown` states: a dongle killed
        mid-transfer needs a physical replug, and on a site hours away that is
        not a recoverable fault. `rtl2832.RtlDevice.close` stops the reader and
        joins it before releasing the device; all this has to do is not race an
        open that is still in flight.

        **The wait is short on purpose, and this is load-bearing.** `shutdown`
        is called from `Agent.build_devices`, which runs on the sensing loop —
        the same loop that reads the power sensor and the floodlight. When a
        dongle is unplugged its open thread is stuck inside librtlsdr probing a
        device that is not there, and rediscovery calls `shutdown` every thirty
        seconds. A five-second join there froze the whole loop for five seconds,
        so power and floodlight — publishing on that loop and working perfectly
        — went stale on the console and flashed a fault every half minute. The
        failing radio was crossing wires into panels that had nothing to do
        with it.

        The join does not need to be long. `_open` acquires the lock and checks
        `_closing` before it installs anything (see there), so an open still in
        flight cannot resurrect a torn-down device however late it finishes —
        the daemon thread simply closes the dongle it opened and returns. The
        wait is a brief settle, not the safety mechanism; the `_closing` check
        is.
        """
        self._closing = True
        opening = self._opening
        if opening is not None and opening.is_alive():
            opening.join(timeout=0.5)
        self._teardown()

    # --- plumbing ---------------------------------------------------------

    def _dead_block(self, seconds: float) -> Block:
        return Block(
            spectrum_db=self._dead_spectrum(), bin_hz=BIN_HZ, seconds=seconds
        )

    @staticmethod
    def _dead_spectrum() -> list[float]:
        return [DEAD_BIN_DBFS] * N_FFT

    @staticmethod
    def _coerce_gain(gain: float | str) -> float | str:
        if isinstance(gain, str):
            text = gain.strip().lower()
            if text in ("auto", ""):
                return "auto" if text == "auto" else 37.2
            try:
                return float(text)
            except ValueError:
                log.warning("Gain %r is not a number; using 37.2 dB.", gain)
                return 37.2
        return float(gain)

    @staticmethod
    def _coerce_int(value: object) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0
