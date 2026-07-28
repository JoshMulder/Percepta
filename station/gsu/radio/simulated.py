"""A simulated airband front end: spectrum in, audio out.

There is no dongle on this machine. What matters is that this simulates the
*hardware*, not the measurement: it produces a spectrum with noise in it and
occasional transmissions, and the station's own DSP measures the floor and
decides the gate from that. The platform's simulator is told its noise floor,
which is why `contract/README.md` rule 3 says the regression it guards against
cannot be exercised there — here it can, and `tests/test_radio_dsp.py` does.

The traffic model matters for one practical reason: **airband is silent the vast
majority of the time**, which is the entire argument for squelch-gating the
audio uplink. A rural channel might carry a transmission every few minutes. The
default reflects that; `GSU_AIRBAND_TRAFFIC=busy` makes it chatty for exercising
the audio path, and `off` silences it for a quiet demonstration.
"""

from __future__ import annotations

import logging
import math
import random

from ..sensors import Device
from . import dsp
from .audio import AUDIO_RATE
from .receiver import Block

log = logging.getLogger("gsu.radio")

#: How wide a spectrum the front end reports, either side of centre. Must cover
#: the measurement window at 50 kHz with room to spare.
SPAN_HZ = 60_000.0

#: What an RTL2832U actually offers, from Remote-Radio's gain table.
AVAILABLE_GAINS = [0.0, 9.0, 14.4, 27.7, 37.2, 42.1, 43.4, 49.6]

TRAFFIC = {
    # (gap seconds min, max), (transmission seconds min, max)
    "off": ((10**9, 10**9), (0.0, 0.0)),
    "low": ((70.0, 220.0), (3.0, 8.0)),
    "busy": ((6.0, 25.0), (3.0, 9.0)),
}


def channel_noise_dbfs(freq_hz: int) -> float:
    """Per-bin noise, dBFS, and different per channel.

    A quiet rural channel and one next to a pager transmitter do not have the
    same floor, and a squelch that behaves identically on both is a squelch that
    has never met a real site.
    """
    rng = random.Random(int(freq_hz) // 25_000)
    return -95.0 + rng.uniform(-3.0, 7.0)


class SimulatedFrontEnd:
    tx_capable = False
    available_gains = AVAILABLE_GAINS

    def __init__(self, traffic: str = "low", seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._traffic = traffic if traffic in TRAFFIC else "low"
        self._freq_hz = 118_700_000
        self._gain: float | str = 37.2
        self._ppm = 0

        self._transmitting = False
        self._remaining = self._rng.uniform(*TRAFFIC[self._traffic][0])
        self._snr_db = 0.0
        self._phase = 0.0
        self._syllable = 0.0
        self._f0 = 140.0

    # --- tuner ----------------------------------------------------------

    def tune(self, freq_hz: int) -> None:
        if freq_hz != self._freq_hz:
            self._freq_hz = int(freq_hz)
            # A retune does not reset the traffic on the channel: broadcasts
            # advance whether or not anyone is listening, so tuning in joins a
            # transmission already in progress.

    def set_gain(self, gain: float | str) -> None:
        self._gain = gain

    def set_traffic(self, level: str, transmitting: bool | None = None) -> None:
        """How busy the channel is. Used by `gsu bench` to measure the gate-open
        and gate-closed paths separately rather than averaging a run that
        happened to contain a transmission."""
        if level in TRAFFIC:
            self._traffic = level
        if transmitting is not None:
            self._transmitting = bool(transmitting)
            self._remaining = 10.0 if transmitting else self._rng.uniform(*TRAFFIC[self._traffic][0])
            if transmitting and self._snr_db <= 0:
                self._snr_db = 20.0

    def set_ppm(self, ppm: int) -> None:
        self._ppm = int(ppm)

    # --- measurement ----------------------------------------------------

    def read(self, seconds: float) -> Block:
        self._advance_traffic(seconds)

        bins = int(2 * SPAN_HZ / dsp.BIN_HZ) + 1
        base = channel_noise_dbfs(self._freq_hz)
        # Gain shifts the whole picture, signal and noise together, which is why
        # turning it up does not improve the signal-to-noise ratio and why the
        # AGC's desensing is invisible in a single number.
        gain_db = 0.0 if self._gain == "auto" else (float(self._gain) - 37.2) * 0.5

        offsets = dsp.bin_offsets(bins, dsp.BIN_HZ)
        spectrum: list[float] = []
        for offset in offsets:
            # Exponentially distributed bin power — the reason the floor
            # estimator takes a median rather than a mean.
            noise_db = base + gain_db + 10 * math.log10(-math.log(self._rng.random()))
            spectrum.append(noise_db)

        if self._transmitting:
            # Total in-channel signal power sits `snr` above the in-channel
            # noise, spread over the emission with a carrier at the centre.
            floor_in_channel = base + gain_db + dsp.IN_CHANNEL_CORRECTION_DB
            signal_total = 10 ** ((floor_in_channel + self._snr_db) / 10)
            weights = []
            for offset in offsets:
                magnitude = abs(offset)
                if magnitude <= 500:
                    weights.append(6.0)          # carrier
                elif magnitude <= 4000:
                    weights.append(1.0)          # sidebands
                elif magnitude <= 7500:
                    weights.append(0.3)
                elif magnitude <= 12000:
                    weights.append(0.02)         # skirts, well short of 15 kHz
                else:
                    weights.append(0.0)
            total_weight = sum(weights) or 1.0
            for index, weight in enumerate(weights):
                if weight <= 0:
                    continue
                spectrum[index] = 10 * math.log10(
                    10 ** (spectrum[index] / 10) + signal_total * weight / total_weight
                )

        return Block(spectrum_db=spectrum, bin_hz=dsp.BIN_HZ, seconds=seconds)

    def _advance_traffic(self, seconds: float) -> None:
        gap, length = TRAFFIC[self._traffic]
        self._remaining -= seconds
        if self._remaining > 0:
            if self._transmitting:
                # Slow fading within a transmission, so the meter moves and a
                # marginal signal genuinely flutters around the threshold.
                self._snr_db = max(4.0, self._snr_db + self._rng.uniform(-1.5, 1.5))
            return
        if self._transmitting:
            self._transmitting = False
            self._remaining = self._rng.uniform(*gap)
        else:
            self._transmitting = True
            self._remaining = self._rng.uniform(*length)
            # Aircraft at range are weak; the tower down the valley is not.
            self._snr_db = self._rng.choice([9.0, 14.0, 20.0, 28.0]) + self._rng.uniform(-2, 2)
            self._f0 = self._rng.uniform(95.0, 190.0)

    # --- audio ----------------------------------------------------------

    def demodulate(self, samples: int) -> list[float]:
        """AM audio for the block just read.

        Voice when there is a transmission, hiss when there is not — which is
        what an operator holding MON on an empty channel expects to hear, and
        what tells them the receiver is alive.
        """
        if samples <= 0:
            return []
        rate = AUDIO_RATE
        out: list[float] = []
        clarity = 0.0 if not self._transmitting else min(1.0, max(0.0, (self._snr_db - 4) / 24))
        hiss = 0.12 * (1.0 - clarity) + 0.02
        step = 2 * math.pi * self._f0 / max(1, rate)
        for index in range(samples):
            value = 0.0
            if self._transmitting:
                self._phase += step
                # Syllabic envelope: speech is not a steady tone, and a steady
                # tone is instantly recognisable as a fake on a headset.
                self._syllable += 1.0 / max(1, rate)
                envelope = 0.35 + 0.65 * abs(math.sin(self._syllable * 3.1))
                value = (
                    math.sin(self._phase) * 0.55
                    + math.sin(self._phase * 2.1) * 0.25
                    + math.sin(self._phase * 3.7) * 0.12
                ) * envelope * clarity
            out.append(value + self._rng.gauss(0.0, hiss))
        return out

    # --- lifecycle ------------------------------------------------------

    def shutdown(self) -> None:
        # Nothing to stop here, but this is the call site that matters: a real
        # front end stops its radio process through the process's own shutdown
        # endpoint and never with a signal (05-radio-integration.md §2).
        log.info("Receiver stopped gracefully.")

    def describe(self) -> Device:
        return Device(
            id="radio",
            kind="airband-receiver",
            present=True,
            detail=f"simulated airband receiver, traffic={self._traffic}, receive only",
            simulated=True,
        )
