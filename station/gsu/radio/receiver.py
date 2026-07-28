"""The receiver interface, and the squelch gate that sits on top of it.

The front end is whatever hardware is attached: it tunes, it hands over a
spectrum and it demodulates. Everything that decides *whether anyone hears
anything* is here, above the hardware, because that is where the contract puts
the correctness (`contract/README.md` rule 3) and because it has to behave
identically whichever receiver is fitted.

What the state machine has to get right, all of it learned the hard way in
`server/docs/05-radio-integration.md`:

* **AUTO rides the measured floor at +8 dB.**
* **Turning AUTO off freezes the threshold where it was.** Leaving the manual
  threshold unset means the gate carries on riding the floor with AUTO showing
  off, and the control appears to do nothing.
* **Setting a threshold by hand leaves AUTO**, exactly as moving the slider does
  on a real receiver.
* **Monitor defeats the gate** without changing the threshold, and is expected
  to be held rather than latched.
* **Gain defaults to a fixed value, not `auto`.** The tuner's own AGC desenses
  near strong transmitters badly enough that a stronger signal reads *lower*,
  and a mast-mounted antenna is exactly where that bites.
* **Frequency, gain and ppm survive a restart.**
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..sensors import Device
from . import dsp
from .audio import AUDIO_RATE, audio_payload, to_pcm16

log = logging.getLogger("gsu.radio")

FREQ_MIN_HZ = 108_000_000
FREQ_MAX_HZ = 137_000_000

#: The gate stays open briefly after a signal drops, so a transmission with a
#: gap in it does not arrive as two clipped fragments. Every receiver does this.
HANG_SECONDS = 0.6


@dataclass(frozen=True)
class Block:
    """One block of samples' worth of measurement.

    `spectrum_db` is per-bin power in dBFS, centred on the tuned frequency, and
    must extend to at least ±50 kHz — the floor cannot be measured outside the
    channel if the front end never shows outside the channel.
    """

    spectrum_db: list[float]
    bin_hz: float
    seconds: float


@runtime_checkable
class RadioFrontEnd(Protocol):
    """A tuner. Receive only, deliberately: see the package docstring."""

    tx_capable: bool
    available_gains: list[float]

    def tune(self, freq_hz: int) -> None: ...
    def set_gain(self, gain: float | str) -> None: ...
    def set_ppm(self, ppm: int) -> None: ...

    def read(self, seconds: float) -> Block:
        """Advance by `seconds` and return the measurement for that block.
        Called every tick whether or not anyone is listening — a receiver that
        only runs when someone is attached has no idea what it missed."""

    def demodulate(self, samples: int) -> list[float]:
        """AM-demodulated audio for the block just read, in [-1, 1]. Called only
        when the gate is open, which is what keeps the uplink cheap."""

    def shutdown(self) -> None:
        """Stop the receiver **gracefully**.

        `server/docs/05-radio-integration.md` obligation 2: the dongle wedges if
        hard-killed mid-transfer and needs a physical replug — a USB reset is not
        enough. On a site hours away that is not a recoverable fault. Any
        implementation that supervises a radio process must call its `/shutdown`
        endpoint here and must never SIGKILL it, and the agent calls this on the
        way out of every shutdown path it has.
        """

    def describe(self) -> Device: ...


class RadioController:
    def __init__(self, front_end: RadioFrontEnd, state_path: Path | None = None) -> None:
        self.front_end = front_end
        self.state_path = state_path

        self.freq_hz = 118_700_000
        # Fixed, not "auto" — see the class docstring.
        self.gain: float | str = 37.2
        self.ppm = 0
        self.auto_squelch = True
        #: An absolute dBFS threshold the operator set. None means AUTO has
        #: never been left, so there is nothing frozen to fall back to.
        self.manual_threshold_db: float | None = None
        self.monitor = False
        #: Last threshold actually applied, so leaving AUTO can freeze at it
        #: rather than jumping.
        self.last_threshold_db = -70.0

        self._hang = 0.0
        self._rssi_db = -100.0
        self._floor_db = -100.0
        self._open = False

        self._load()
        self.front_end.tune(self.freq_hz)
        self.front_end.set_gain(self.gain)
        self.front_end.set_ppm(self.ppm)

    # --- persistence ----------------------------------------------------

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            log.warning("Receiver state unreadable; starting from defaults.")
            return
        self.freq_hz = int(state.get("freq_hz", self.freq_hz))
        self.gain = state.get("gain", self.gain)
        self.ppm = int(state.get("ppm", self.ppm))
        self.auto_squelch = bool(state.get("auto_squelch", self.auto_squelch))
        threshold = state.get("manual_threshold_db")
        self.manual_threshold_db = None if threshold is None else float(threshold)
        log.info("Receiver state restored: %.3f MHz, gain %s, ppm %s.",
                 self.freq_hz / 1e6, self.gain, self.ppm)

    def _save(self) -> None:
        if not self.state_path:
            return
        state = {
            "freq_hz": self.freq_hz,
            "gain": self.gain,
            "ppm": self.ppm,
            "auto_squelch": self.auto_squelch,
            "manual_threshold_db": self.manual_threshold_db,
        }
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
            tmp.replace(self.state_path)
        except OSError:
            log.warning("Could not persist receiver state to %s.", self.state_path)

    # --- commands -------------------------------------------------------

    def tune(self, freq_hz: int) -> None:
        # The console clamps to the airband and so does the station: a command
        # is a request, and a request for 900 MHz is a bug somewhere upstream
        # that must not become a retune here.
        self.freq_hz = max(FREQ_MIN_HZ, min(FREQ_MAX_HZ, int(freq_hz)))
        self.front_end.tune(self.freq_hz)
        self._save()

    def set_squelch(self, db: float) -> None:
        self.manual_threshold_db = float(db)
        self.auto_squelch = False
        self._save()

    def set_auto_squelch(self, on: bool) -> None:
        want = bool(on)
        if not want and self.auto_squelch:
            # Freeze where AUTO had it.
            self.manual_threshold_db = self.last_threshold_db
        self.auto_squelch = want
        self._save()

    def set_monitor(self, on: bool) -> None:
        # Not persisted: monitor is momentary and station-wide, and a box that
        # rebooted with the gate held open would push hiss to every listener
        # until someone noticed.
        self.monitor = bool(on)

    def set_gain(self, gain: float | str) -> None:
        self.gain = gain if gain == "auto" else float(gain)
        self.front_end.set_gain(self.gain)
        self._save()

    def set_ppm(self, ppm: int) -> None:
        self.ppm = int(ppm)
        self.front_end.set_ppm(self.ppm)
        self._save()

    # --- the gate -------------------------------------------------------

    def tick(self, dt: float) -> tuple[dict, dict | None]:
        """One block: measure, decide, and demodulate only if the gate is open.

        Returns the `radio` telemetry payload and, when the gate is open, an
        audio payload. Audio while the gate is shut is the single most expensive
        mistake available on a metered link, so there is exactly one place that
        decides and it is this one.
        """
        block = self.front_end.read(dt)
        self._rssi_db = dsp.in_channel_power_db(block.spectrum_db, block.bin_hz)
        self._floor_db = dsp.noise_floor_db(block.spectrum_db, block.bin_hz)

        if self.auto_squelch or self.manual_threshold_db is None:
            threshold = dsp.auto_threshold_db(self._floor_db)
        else:
            threshold = self.manual_threshold_db
        self.last_threshold_db = threshold

        above = self._rssi_db > threshold
        if above:
            self._hang = HANG_SECONDS
        elif self._hang > 0:
            self._hang = max(0.0, self._hang - dt)
        self._open = bool(above or self._hang > 0 or self.monitor)

        telemetry = {
            "kind": "radio",
            "freq_hz": self.freq_hz,
            "rssi_db": round(self._rssi_db, 1),
            "noise_floor_db": round(self._floor_db, 1),
            "threshold_db": round(threshold, 1),
            "squelch_open": self._open,
            "auto_squelch": self.auto_squelch,
            "monitor": self.monitor,
            "gain": self.gain,
            "gains": list(self.front_end.available_gains),
            "ppm": self.ppm,
            # Receive-only hardware. The console disables PTT from this, and
            # there is no transmit path in this station to enable.
            "tx_capable": False,
        }

        audio = None
        if self._open:
            samples = self.front_end.demodulate(int(AUDIO_RATE * dt))
            audio = audio_payload(to_pcm16(samples), AUDIO_RATE)
        return telemetry, audio

    # --- state ----------------------------------------------------------

    @property
    def squelch_open(self) -> bool:
        return self._open

    def describe(self) -> Device:
        return self.front_end.describe()

    def shutdown(self) -> None:
        self.front_end.shutdown()
