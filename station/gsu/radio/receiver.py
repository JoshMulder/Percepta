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
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..sensors import Device
from . import dsp
from .audio import AUDIO_RATE, Encoder, OpusUnavailable, audio_payload, to_pcm16

log = logging.getLogger("gsu.radio")

#: Bins sent to the console. The receiver measures 241 across a 120 kHz span;
#: a canvas a few hundred pixels wide cannot show that and a metered link
#: should not carry it. Decimated by taking the strongest bin in each group,
#: never the mean: a 25 kHz channel is a handful of bins wide and averaging
#: buries a real carrier in the noise either side of it, which is the one thing
#: this display exists to show.
SPECTRUM_BINS = 128

#: How long one request keeps the spectrum coming. Longer than the console's
#: refresh interval so an open page never flickers, short enough that closing
#: the tab stops the traffic within a few seconds.
SPECTRUM_WINDOW_S = 12.0

#: How long one request keeps audio coming, when the platform does not say.
#:
#: Audio is the largest thing this station sends — 24 kHz of 16-bit mono is
#: 384 kbit/s, and base64 in a JSON envelope makes it 512 — and it used to go
#: up whenever the squelch opened, listener or not. The spectrum has been
#: demand-driven since it was written, for a cost two orders of magnitude
#: smaller; audio was the one that mattered and it was the one left open.
#:
#: The platform states a lease with each request and this is only the
#: fallback. Longer than the spectrum's window because an over lasts seconds
#: and a gap mid-transmission is what a listener actually notices.
AUDIO_WINDOW_S = 30.0

#: Longest the gate stays defeated by monitor before releasing itself.
#:
#: Monitor is the MON button on a handheld: momentary, expected to be held, and
#: the contract's command schema says so. But the thing holding it is a console
#: on the other side of a link that drops, and nothing releases it if that
#: console closes, crashes or is signed out — the same "most listeners never say
#: goodbye" problem the audio and video leases exist for. A gate held open is
#: not a cosmetic state: it reports squelch_open, so audio flows continuously
#: at ~512 kbit/s from an unattended site on a metered link, for as long as
#: nobody notices.
#:
#: Five minutes is far longer than any real use — setting a level against the
#: noise takes seconds — and short enough that a forgotten press costs a few
#: pounds rather than a month of uplink. The release is reported like any other
#: state: telemetry says monitor false, so a console never shows a gate held
#: open that is not.
MONITOR_MAX_S = 300.0

FREQ_MIN_HZ = 108_000_000
FREQ_MAX_HZ = 137_000_000

#: The gate stays open briefly after a signal drops, so a transmission with a
#: gap in it does not arrive as two clipped fragments. Every receiver does this.
HANG_SECONDS = 0.6


def _decimate_db(bins: list[float], target: int) -> list[int]:
    """Fewer bins, rounded to whole dB, keeping the peaks.

    Max rather than mean per group: an airband channel is 25 kHz — a handful of
    500 Hz bins — and averaging it with the quiet either side flattens the one
    feature the display exists to show. Whole dB because the canvas cannot
    render a tenth and the link should not carry one.
    """
    if not bins:
        return []
    if len(bins) <= target:
        return [round(value) for value in bins]
    out: list[int] = []
    for index in range(target):
        start = index * len(bins) // target
        end = max(start + 1, (index + 1) * len(bins) // target)
        out.append(round(max(bins[start:end])))
    return out


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
        #: When a held-open gate releases itself. See MONITOR_MAX_S.
        self._monitor_until = 0.0
        #: The Opus encoder, built on first use and held. See `_encoder`.
        self._opus: Encoder | None = None
        self._opus_failed = False
        #: The last block of PCM, kept for the local recording whatever
        #: happened to the payload — a transmission is written to disk whether
        #: or not anybody is listening and whether or not the link is up.
        self.last_pcm = b""
        #: Last threshold actually applied, so leaving AUTO can freeze at it
        #: rather than jumping.
        self.last_threshold_db = -70.0

        #: Local PCM listeners, for `/audio.wav` on the setup page. Each is a
        #: bounded queue of the same blocks the recorder gets — never a second
        #: demodulation, for the same reason the setup page shares the camera's
        #: encoder rather than starting its own.
        self._listeners: set[queue.Queue] = set()
        self._listeners_lock = threading.Lock()

        self._hang = 0.0
        self._rssi_db = -100.0
        self._floor_db = -100.0
        self._open = False
        #: The most recent per-bin power, kept so the spectrum can be published
        #: without recomputing it — the receiver already measures this every
        #: block for RSSI and the noise floor.
        self._last_spectrum: list[float] = []
        #: Monotonic time until which somebody is watching the spectrum. Zero
        #: means nobody, and nobody is the normal case.
        self._spectrum_until = 0.0
        #: The same, for audio. Also zero by default: a station that has never
        #: been asked sends no audio, which is the whole point — a box at a
        #: quiet site with nobody logged in should cost nothing on the link.
        self._audio_until = 0.0
        # The last few measurement lines for the setup page's datastream
        # field: what the receiver is hearing, in the terms the console's own
        # radio panel uses. Bounded — a tap, never a history.
        self._raw: deque[str] = deque(maxlen=4)

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
        self._monitor_until = (
            time.monotonic() + MONITOR_MAX_S if self.monitor else 0.0
        )

    def set_gain(self, gain: float | str) -> None:
        self.gain = gain if gain == "auto" else float(gain)
        self.front_end.set_gain(self.gain)
        self._save()

    def spectrum_for_display(self) -> list[int]:
        """The spectrum for the station's own setup page.

        No demand window: that exists because the telemetry frame crosses a
        metered link, and this is served to a laptop on the same bench.
        """
        return _decimate_db(self._last_spectrum, SPECTRUM_BINS)

    def spectrum_span_hz(self) -> int:
        return round(len(self._last_spectrum) * dsp.BIN_HZ)

    def want_spectrum(self, on: bool = True) -> None:
        """Somebody is looking at the spectrum, or has stopped.

        Re-requested periodically rather than held open by a connection: a
        console that crashes or a laptop lid that closes should stop the
        traffic on its own, and neither sends a goodbye.
        """
        self._spectrum_until = (
            time.monotonic() + SPECTRUM_WINDOW_S if on else 0.0
        )

    def want_audio(self, on: bool = True, lease_seconds: float | None = None) -> None:
        """Somebody is listening, or has stopped.

        Leased and re-requested rather than held open by a connection, exactly
        like `want_spectrum` and like the camera: a console that crashes, a
        laptop lid that closes or a platform that goes away should stop the
        traffic on its own, and none of them sends a goodbye. **Silence is the
        stop signal**, which is the only version of this that survives the
        platform failing rather than merely closing.
        """
        if not on:
            self._audio_until = 0.0
            return
        window = AUDIO_WINDOW_S if lease_seconds is None else float(lease_seconds)
        # A platform asking for an absurd lease must not be able to pin the
        # uplink open; a tiny one must not make audio stutter.
        window = max(5.0, min(300.0, window))
        self._audio_until = time.monotonic() + window

    @property
    def audio_wanted(self) -> bool:
        """Whether audio should go up the link right now.

        Local recording does not consult this. A transmission during an
        outage, or one nobody happened to be listening to, is not simply gone
        — `store.write_audio` keeps it either way, and that was true before
        this existed.
        """
        return time.monotonic() < self._audio_until

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
        if self.monitor and time.monotonic() >= self._monitor_until:
            # Nobody released it. Whoever pressed it is not necessarily still
            # there, and this gate costs bandwidth for as long as it is open.
            self.monitor = False
            self._monitor_until = 0.0
            log.warning(
                "Monitor released after %.0fs: the gate was held open and "
                "nothing turned it off. Audio stops unless a real signal "
                "opens the squelch.", MONITOR_MAX_S,
            )

        block = self.front_end.read(dt)
        self._last_spectrum = block.spectrum_db
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

        # The spectrum rides the same frame, but only while somebody is looking.
        #
        # 241 bins of float at 1 Hz is roughly 150 MB a day on a link that is
        # metered and shared with video, for a display that is open for a few
        # minutes at commissioning. So it is demand-driven, the same shape as
        # the camera preview: the console asks, the answer lasts a few seconds,
        # and a station nobody is watching sends nothing at all.
        if time.monotonic() < self._spectrum_until:
            spectrum = self._last_spectrum
            if spectrum:
                telemetry["spectrum"] = _decimate_db(spectrum, SPECTRUM_BINS)
                telemetry["span_hz"] = round(len(spectrum) * dsp.BIN_HZ)

        self._raw.append(
            f"{self.freq_hz / 1e6:.3f} MHz  rssi {self._rssi_db:.1f} dB  "
            f"floor {self._floor_db:.1f} dB  squelch "
            f"{'open' if self._open else 'closed'}"
        )

        audio = None
        self.last_pcm = b""
        if self._open:
            samples = self.front_end.demodulate(int(AUDIO_RATE * dt))
            # PCM is kept whatever happens to the payload: it is what goes to
            # local storage, and a transmission is recorded whether or not
            # anybody is listening and whether or not the link is up.
            self.last_pcm = to_pcm16(samples)
            encoder = self._encoder()
            if encoder is not None:
                # None means this tick produced fewer than the four packets the
                # contract requires — 80 ms of speech is not worth an envelope
                # of its own — so the recording still happens and nothing goes
                # on the wire yet.
                audio = audio_payload(self.last_pcm, encoder, AUDIO_RATE)
        self._feed_listeners(self.last_pcm, dt)
        return telemetry, audio

    # --- listening on the box itself --------------------------------------

    def attach_listener(self) -> "queue.Queue[bytes]":
        """A queue of PCM blocks, for `/audio.wav` on the setup page.

        The same blocks the recorder and the Opus encoder are handed, never a
        second demodulation — the front end is one device with one reader, and
        the setup page shares the camera's encoder for exactly this reason.

        Bounded and dropping-oldest: somebody who stops reading must not grow
        this without bound or stall the sensing loop, and for a diagnostic the
        newest audio is the audio worth having.
        """
        listener: "queue.Queue[bytes]" = queue.Queue(maxsize=64)
        with self._listeners_lock:
            self._listeners.add(listener)
        return listener

    def detach_listener(self, listener: "queue.Queue[bytes]") -> None:
        with self._listeners_lock:
            self._listeners.discard(listener)

    def _feed_listeners(self, pcm: bytes, dt: float) -> None:
        """Every block, open gate or shut.

        Silence is sent while the squelch is closed rather than nothing at all.
        A listener that simply stalls between transmissions cannot be told
        apart from one that is dropping audio, which is the entire question
        this exists to answer — so the timeline stays real and a gap arrives as
        a gap rather than as a pause in the download.
        """
        with self._listeners_lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        if not pcm:
            pcm = b"\x00\x00" * max(0, int(AUDIO_RATE * dt))
        if not pcm:
            return
        for listener in listeners:
            try:
                listener.put_nowait(pcm)
            except queue.Full:
                try:
                    listener.get_nowait()
                    listener.put_nowait(pcm)
                except (queue.Empty, queue.Full):
                    pass

    def _encoder(self) -> Encoder | None:
        """The Opus encoder, built once and held.

        Held rather than rebuilt because an encoder carries prediction state
        between frames: constructing a fresh one per transmission throws that
        away and costs bitrate for nothing.

        A box without libopus reports it once and then publishes no audio. It
        goes on receiving, squelching and recording — the local WAV is PCM and
        needs no codec — because a missing library is not a reason to stop
        doing the part of the job that still works.
        """
        if self._opus is not None:
            return self._opus
        if self._opus_failed:
            return None
        try:
            self._opus = Encoder(AUDIO_RATE)
        except OpusUnavailable as exc:
            self._opus_failed = True
            log.error("No airband audio will be published: %s", exc)
            return None
        return self._opus

    # --- state ----------------------------------------------------------

    @property
    def squelch_open(self) -> bool:
        return self._open

    def raw_sample(self) -> list[str]:
        return list(self._raw)

    def describe(self) -> Device:
        return self.front_end.describe()

    def shutdown(self) -> None:
        self.front_end.shutdown()
