"""The dongle: an RTL2832U tuner, driven through `librtlsdr` by ctypes.

## Why ctypes and not a pip package

`pyrtlsdr` is what Remote-Radio uses and it is a thin wrapper over the same C
library. Depending on it would add a pip package *and* still require the C
library, so it buys nothing the station wants: `requirements.txt` exists to keep
the number of things that must be installed on an unattended box as small as it
can be. `ctypes.CDLL("librtlsdr.so.0")` is the standard library, and the
apt package `librtlsdr0` is the only new thing on the image.

## The licence question, which is not settled

**`librtlsdr` is GPL-2.0.** Remote-Radio's `rtl/README.md` names this as one of
three reasons it started a clean-room driver, and the reason still stands: a
commercial product that ships the library carries source-offer obligations.
This module keeps the obligation as small as it can be and no smaller —

* Nothing here is derived from `librtlsdr`'s source. The declarations below are
  the published C API (the function names, argument types and return
  conventions of `rtl-sdr.h`), which is the interface a caller must use, not the
  implementation.
* The library is **not vendored**. It is `apt install librtlsdr0`, so on a
  Debian-derived image it is Debian's package under Debian's terms and the
  station merely calls it.
* `RtlDevice` is behind the same seam a native driver would sit behind, so
  replacing it later is one class, not a rewrite of the receiver.

If the station is ever shipped as an appliance image with the library baked in,
that is a decision to take deliberately. `DECISIONS.md` is where it belongs, and
this docstring is the flag.

## The dongle's two ways of failing, both inherited from Remote-Radio

**It wedges if the process is hard-killed.** Killed mid-transfer the tuner's i2c
goes unresponsive and only a physical replug recovers it — a USB port reset is
not enough, because it does not power-cycle the tuner. On a site hours away that
is not a recoverable fault. So `close()` stops the reader thread and *joins* it
before calling `rtlsdr_close`, and the reader reads in 34 ms blocks precisely so
that join is bounded. `receiver.RadioFrontEnd.shutdown` is the contract that
gets us here and `agent.py` calls it on every shutdown path it has. Nothing in
this station may SIGKILL the process that holds the dongle.

**Its tuning error is set at each initialisation, not fixed.** Remote-Radio
measured +640, +24 and +599 ppm across three inits of the same device — the
tuner coming up mis-programmed, silently, with no error reported. A 25 kHz
channel tolerates roughly ±100 ppm, so a bad init is not subtle: it is silence.
This driver cannot detect it (reading the PLL back is exactly what the native
driver was for), so it does the one thing it can: `describe()` reports the
centre frequency the hardware says it is on, and `open()` logs it. If the
receiver is deaf after a restart, power-cycle the dongle before touching ppm.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections import deque

import numpy as np

log = logging.getLogger("gsu.radio")

#: Tried in order. Debian ships `.so.0`; some builds carry `.so.2`.
SONAMES = ("librtlsdr.so.0", "librtlsdr.so.2", "librtlsdr.so")

#: Bytes per synchronous read. 16384 bytes is 8192 IQ pairs, ~34 ms at
#: 240 ksps. It must be a multiple of 512 for the USB transfer, and small
#: because it bounds how long `close()` waits for the reader to notice it should
#: stop — see the module docstring on wedging.
READ_BYTES = 16384

#: How much IQ the reader may hold before it starts dropping the oldest.
#: `contract/transport.md`: favour dropping data over queueing it. A listener
#: wants the last second, not a second from ten seconds ago.
MAX_BUFFERED_BLOCKS = 44  # ~1.5 s at 240 ksps

#: How far the achieved sample rate may sit from the requested one. 240 000
#: divides a nominal 28.8 MHz reference exactly, so this should be zero; the
#: tolerance is for a dongle with a different crystal, where a fraction of a
#: percent is harmless and anything more means the divider landed elsewhere.
SAMPLE_RATE_TOLERANCE = 0.001  # 0.1%

#: librtlsdr's tuner type enumeration, for `describe()`.
TUNER_NAMES = {
    0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013",
    4: "FC2580", 5: "R820T", 6: "R828D",
}


class RtlError(RuntimeError):
    """A librtlsdr call failed. Caught at the front end and turned into a
    reason; nothing above this module raises."""


def load_library() -> ctypes.CDLL:
    """dlopen librtlsdr, or say plainly what to install.

    Cheap — a dlopen of an already-present shared object, no subprocess — so it
    is safe to call from a constructor that runs inside a sensing tick.
    """
    attempts = []
    for soname in SONAMES:
        try:
            return _declare(ctypes.CDLL(soname))
        except OSError as exc:
            attempts.append(f"{soname}: {exc}")
    raise RtlError(
        "librtlsdr is not installed (tried " + ", ".join(SONAMES) + "). "
        "`sudo apt install librtlsdr0` provides it; `rtl-sdr` adds the "
        "command-line tools that are useful for proving the dongle works. "
        "**An RTL-SDR Blog V4 needs the rtl-sdr-blog fork of the driver, not "
        "Debian's mainline package** — the V4's R828D tuner will not initialise "
        "on mainline librtlsdr, so build it from github.com/rtlsdrblog/rtl-sdr-blog "
        "(a V3 is fine on either)."
    )


def _declare(lib: ctypes.CDLL) -> ctypes.CDLL:
    """Argument and return types for the calls this station makes.

    Declared rather than left to ctypes' defaults: a 64-bit pointer returned
    through a default `int` return type is truncated, which presents as a
    segfault a long way from here.
    """
    void_p = ctypes.c_void_p
    lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
    lib.rtlsdr_get_device_count.argtypes = []
    lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
    lib.rtlsdr_get_device_name.argtypes = [ctypes.c_uint32]
    lib.rtlsdr_get_device_usb_strings.restype = ctypes.c_int
    lib.rtlsdr_get_device_usb_strings.argtypes = [
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
    ]
    lib.rtlsdr_get_index_by_serial.restype = ctypes.c_int
    lib.rtlsdr_get_index_by_serial.argtypes = [ctypes.c_char_p]
    lib.rtlsdr_open.restype = ctypes.c_int
    lib.rtlsdr_open.argtypes = [ctypes.POINTER(void_p), ctypes.c_uint32]
    lib.rtlsdr_close.restype = ctypes.c_int
    lib.rtlsdr_close.argtypes = [void_p]
    lib.rtlsdr_set_sample_rate.restype = ctypes.c_int
    lib.rtlsdr_set_sample_rate.argtypes = [void_p, ctypes.c_uint32]
    lib.rtlsdr_get_sample_rate.restype = ctypes.c_uint32
    lib.rtlsdr_get_sample_rate.argtypes = [void_p]
    lib.rtlsdr_set_center_freq.restype = ctypes.c_int
    lib.rtlsdr_set_center_freq.argtypes = [void_p, ctypes.c_uint32]
    lib.rtlsdr_get_center_freq.restype = ctypes.c_uint32
    lib.rtlsdr_get_center_freq.argtypes = [void_p]
    lib.rtlsdr_set_freq_correction.restype = ctypes.c_int
    lib.rtlsdr_set_freq_correction.argtypes = [void_p, ctypes.c_int]
    lib.rtlsdr_set_tuner_gain_mode.restype = ctypes.c_int
    lib.rtlsdr_set_tuner_gain_mode.argtypes = [void_p, ctypes.c_int]
    lib.rtlsdr_set_tuner_gain.restype = ctypes.c_int
    lib.rtlsdr_set_tuner_gain.argtypes = [void_p, ctypes.c_int]
    lib.rtlsdr_get_tuner_gain.restype = ctypes.c_int
    lib.rtlsdr_get_tuner_gain.argtypes = [void_p]
    lib.rtlsdr_get_tuner_gains.restype = ctypes.c_int
    lib.rtlsdr_get_tuner_gains.argtypes = [void_p, ctypes.POINTER(ctypes.c_int)]
    lib.rtlsdr_set_agc_mode.restype = ctypes.c_int
    lib.rtlsdr_set_agc_mode.argtypes = [void_p, ctypes.c_int]
    lib.rtlsdr_reset_buffer.restype = ctypes.c_int
    lib.rtlsdr_reset_buffer.argtypes = [void_p]
    lib.rtlsdr_read_sync.restype = ctypes.c_int
    lib.rtlsdr_read_sync.argtypes = [
        void_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
    ]
    lib.rtlsdr_get_tuner_type.restype = ctypes.c_int
    lib.rtlsdr_get_tuner_type.argtypes = [void_p]
    # The bias tee that powers an antenna LNA down the coax. Present in the
    # RTL-SDR Blog fork (what the V4 needs) and in recent mainline librtlsdr;
    # declared only if the symbol is there, because an older library simply
    # cannot switch it and that is not a fault — see `set_bias_tee`.
    if hasattr(lib, "rtlsdr_set_bias_tee"):
        lib.rtlsdr_set_bias_tee.restype = ctypes.c_int
        lib.rtlsdr_set_bias_tee.argtypes = [void_p, ctypes.c_int]
    return lib


def to_complex(raw: np.ndarray) -> np.ndarray:
    """Interleaved unsigned bytes from the ADC to complex64 in roughly [-1, 1].

    127.5 rather than 127: the RTL2832's 8-bit output is offset binary with no
    exact zero, and centring on 127 leaves a half-LSB DC bias. It would land at
    0 Hz in the raw IQ, which offset tuning already puts 60 kHz away from the
    channel — but a bias that costs nothing to remove is not worth leaving in a
    measurement the squelch threshold is derived from.
    """
    if raw.size % 2:
        raw = raw[:-1]
    samples = raw.astype(np.float32)
    samples -= 127.5
    samples *= 1.0 / 127.5
    return samples.view(np.complex64)


class RtlDevice:
    """One dongle, streaming continuously into a bounded buffer.

    Continuously, because `receiver.RadioFrontEnd.read` is called every tick
    whether or not anyone is listening — a receiver that only runs when someone
    is attached has no idea what it missed — and because starting and stopping
    the USB transfer at 1 Hz is how a dongle gets wedged.
    """

    def __init__(self, sample_rate: int = 240_000, offset_hz: int = 60_000) -> None:
        self.sample_rate = int(sample_rate)
        self.offset_hz = int(offset_hz)
        self.lib = load_library()
        self._handle = ctypes.c_void_p()
        self._open = False
        #: One lock over every libusb call. librtlsdr keeps per-device state
        #: that is not safe against a control transfer landing in the middle of
        #: a bulk read, and the tick thread retunes while the reader reads.
        self._io_lock = threading.Lock()
        self._buffer: deque[np.ndarray] = deque(maxlen=MAX_BUFFERED_BLOCKS)
        self._buffer_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped_blocks = 0
        self.read_error = ""
        self.serial = ""
        self.model = ""
        self.tuner = "unknown"
        self.gains: list[float] = []
        self.bias_tee = False

    # --- lifecycle --------------------------------------------------------

    def open(
        self, freq_hz: int, gain: float | str, ppm: int = 0, bias_tee: bool = False
    ) -> None:
        """Claim the dongle and program it. Raises; the front end reports."""
        count = int(self.lib.rtlsdr_get_device_count())
        if count == 0:
            raise RtlError(
                "no RTL-SDR device found by librtlsdr. It is on the USB bus if "
                "the setup page lists it, so this is usually permissions: the "
                "station's user needs udev access to the device (the `rtl-sdr` "
                "package installs the rule), or the kernel's dvb_usb_rtl28xxu "
                "module has claimed it and must be blacklisted."
            )

        index = self._index_for_serial(count)
        result = self.lib.rtlsdr_open(ctypes.byref(self._handle), index)
        if result < 0 or not self._handle:
            raise RtlError(
                f"rtlsdr_open({index}) failed with {result}. Another process "
                "may hold the dongle — dump1090 and rtl_tcp both claim it "
                "exclusively — or the kernel DVB driver has it."
            )
        self._open = True

        try:
            self._identify(index)
            self._program(freq_hz, gain, ppm, bias_tee)
        except Exception:
            self.close()
            raise

    def _index_for_serial(self, count: int) -> int:
        """Honour the tuner allocation the installer made.

        `devices/registry.py` allocates a tuner by serial number and says why:
        airband and 1090 MHz cannot share one, so on a two-dongle box opening
        "the first one" is a coin toss that puts the receiver on the ADS-B
        tuner. A dongle with no serial programmed cannot be told from another
        one, which the setup page already says; in that case index 0 is the only
        thing available and this says so rather than pretending.
        """
        if not self.serial_hint:
            return 0
        index = int(self.lib.rtlsdr_get_index_by_serial(self.serial_hint.encode()))
        if index >= 0:
            return index
        log.warning(
            "No RTL-SDR with serial %r; opening device 0 of %d instead. If a "
            "second dongle is fitted this may be the wrong one.",
            self.serial_hint, count,
        )
        return 0

    #: Serial number the inventory allocated to this slot, if any. Set before
    #: `open()`; empty means "whatever is there".
    serial_hint: str = ""

    def _identify(self, index: int) -> None:
        manufacturer = ctypes.create_string_buffer(256)
        product = ctypes.create_string_buffer(256)
        serial = ctypes.create_string_buffer(256)
        if self.lib.rtlsdr_get_device_usb_strings(
            index, manufacturer, product, serial
        ) == 0:
            self.serial = serial.value.decode("utf-8", "replace")
            self.model = product.value.decode("utf-8", "replace")
        name = self.lib.rtlsdr_get_device_name(index)
        if not self.model and name:
            self.model = name.decode("utf-8", "replace")
        self.tuner = TUNER_NAMES.get(
            int(self.lib.rtlsdr_get_tuner_type(self._handle)), "unknown"
        )
        self.gains = self._read_gain_table()

    def _read_gain_table(self) -> list[float]:
        """The gain steps this tuner actually has, in dB.

        Read from the device rather than tabulated: `radio/simulated.py` carries
        an R820T2 table for the console to have something to render, but the
        fitted tuner is the authority on what it can do, and an R828D has a
        different set.
        """
        count = int(self.lib.rtlsdr_get_tuner_gains(self._handle, None))
        if count <= 0:
            return []
        buffer = (ctypes.c_int * count)()
        if int(self.lib.rtlsdr_get_tuner_gains(self._handle, buffer)) != count:
            return []
        return [value / 10.0 for value in buffer]  # librtlsdr reports tenths

    def _program(
        self, freq_hz: int, gain: float | str, ppm: int, bias_tee: bool = False
    ) -> None:
        self._check(
            self.lib.rtlsdr_set_sample_rate(self._handle, self.sample_rate),
            f"set sample rate to {self.sample_rate}",
        )
        actual = int(self.lib.rtlsdr_get_sample_rate(self._handle))
        error = abs(actual - self.sample_rate) / self.sample_rate
        if error > SAMPLE_RATE_TOLERANCE:
            # The rate comes from a divider off the reference crystal. A rate
            # that silently snapped somewhere else makes the decimator's
            # 240k/24k ratio a lie and the audio the wrong speed — which sounds
            # like a working receiver, only slightly wrong, for ever.
            raise RtlError(
                f"asked for {self.sample_rate} sps and the device reports "
                f"{actual}; the audio would run {error * 100:.2f}% fast or slow"
            )
        if actual != self.sample_rate:
            # Small deviations are survivable and are survived by design: the
            # front end's audio buffer pads a short second and drops the tail of
            # a long one, so the drift is bounded rather than accumulated. Worth
            # a line in the log all the same — an unexpected crystal is
            # something to know about before it is something to debug.
            log.warning(
                "The dongle's sample rate is %d, not the %d requested (%.3f%% "
                "out). Audio will be paced to the station's clock and the "
                "difference dropped a fraction of a second at a time.",
                actual, self.sample_rate, error * 100,
            )

        # The RTL2832's own digital AGC, off, and separately from the tuner's.
        # Every level in this receiver is an absolute dBFS number — the operator
        # sets a squelch threshold in dBFS and the floor is measured in dBFS —
        # and any automatic gain in front of that measurement makes the scale
        # float, so a threshold found yesterday means something else today.
        self._check(self.lib.rtlsdr_set_agc_mode(self._handle, 0),
                    "disable the RTL2832 digital AGC")
        self.set_ppm(ppm)
        self.set_gain(gain)
        self.set_bias_tee(bias_tee)
        self.set_freq(freq_hz)

        log.info(
            "RTL-SDR open: %s (%s tuner, serial %r) on %.4f MHz "
            "(+%d kHz offset), %d ksps, gain %s, ppm %d, bias tee %s.",
            self.model or "RTL2832U", self.tuner, self.serial or "unprogrammed",
            freq_hz / 1e6, self.offset_hz // 1000, self.sample_rate // 1000,
            gain, ppm, "on" if self.bias_tee else "off",
        )

    def set_bias_tee(self, on: bool) -> None:
        """Power (or unpower) an antenna LNA down the coax.

        A switch on the antenna port, not a receiver setting: off unless an
        active antenna is fitted, because volts into a passive antenna do nothing
        and into a short are worse. The RTL-SDR Blog V4 has one, and this is
        where it earns "V4 support" beyond the tuner the driver already handles.

        Needs the rtl-sdr-blog driver or a recent librtlsdr for the symbol. On an
        older library it is unavailable rather than a fault — a receiver with no
        bias tee still receives, so this logs and carries on rather than refusing
        to open.
        """
        want = bool(on)
        if not hasattr(self.lib, "rtlsdr_set_bias_tee"):
            self.bias_tee = False
            if want:
                log.warning(
                    "Bias tee asked for but this librtlsdr has no "
                    "rtlsdr_set_bias_tee — the RTL-SDR Blog driver provides it. "
                    "Leaving the antenna port unpowered."
                )
            return
        with self._io_lock:
            self._check(
                self.lib.rtlsdr_set_bias_tee(self._handle, 1 if want else 0),
                f"{'enable' if want else 'disable'} the bias tee",
            )
        self.bias_tee = want

    def close(self) -> None:
        """Stop the reader, wait for it, then release the dongle.

        The order is the whole point. See the module docstring: a transfer
        interrupted by the process dying wedges the tuner until someone walks to
        the site and unplugs it.
        """
        self.stop_stream()
        with self._io_lock:
            if self._open and self._handle:
                try:
                    self.lib.rtlsdr_close(self._handle)
                except Exception:  # noqa: BLE001 - shutting down; say so and go on
                    log.warning("rtlsdr_close failed.", exc_info=True)
            self._open = False
            self._handle = ctypes.c_void_p()
        log.info("RTL-SDR closed gracefully.")

    @property
    def is_open(self) -> bool:
        return self._open

    # --- tuning -----------------------------------------------------------

    def set_freq(self, freq_hz: int) -> None:
        """Tune, offset included, and throw away what was already buffered.

        The buffer holds up to a second and a half of samples from the *old*
        frequency. Measuring those against the new channel would report one
        tick of a spectrum belonging to somewhere else — and if the old channel
        was busy, would open the gate on a transmission that is not there.
        """
        with self._io_lock:
            self._check(
                self.lib.rtlsdr_set_center_freq(
                    self._handle, int(freq_hz) + self.offset_hz
                ),
                f"tune to {freq_hz / 1e6:.4f} MHz",
            )
        self.flush()

    def set_gain(self, gain: float | str) -> None:
        with self._io_lock:
            if gain == "auto":
                # Allowed, because the contract allows it and an operator may
                # want it. Not the default, and it is logged every time: the
                # tuner AGC desenses near strong transmitters badly enough that
                # a stronger signal can read *lower*, and a mast-mounted
                # antenna is exactly where that bites.
                self._check(self.lib.rtlsdr_set_tuner_gain_mode(self._handle, 0),
                            "hand gain to the tuner AGC")
                log.warning(
                    "Tuner gain set to auto. Levels are no longer comparable "
                    "between ticks, so the squelch threshold and the noise "
                    "floor both lose their meaning; a fixed gain is what this "
                    "receiver is designed around."
                )
                return
            self._check(self.lib.rtlsdr_set_tuner_gain_mode(self._handle, 1),
                        "take manual control of the tuner gain")
            self._check(
                self.lib.rtlsdr_set_tuner_gain(self._handle, int(round(float(gain) * 10))),
                f"set tuner gain to {gain} dB",
            )

    def applied_gain(self) -> float | None:
        """What the tuner snapped to, which is a step in its table and not
        necessarily what was asked for."""
        with self._io_lock:
            if not self._open:
                return None
            return int(self.lib.rtlsdr_get_tuner_gain(self._handle)) / 10.0

    def set_ppm(self, ppm: int) -> None:
        with self._io_lock:
            result = int(self.lib.rtlsdr_set_freq_correction(self._handle, int(ppm)))
            # -2 is librtlsdr for "that is already the value", which is a
            # success. Treating it as a failure is a classic way to make a
            # working receiver report a fault on every restart.
            if result < 0 and result != -2:
                raise RtlError(f"could not set ppm to {ppm} (error {result})")

    def centre_freq(self) -> int:
        with self._io_lock:
            if not self._open:
                return 0
            return int(self.lib.rtlsdr_get_center_freq(self._handle))

    # --- streaming --------------------------------------------------------

    def start_stream(self) -> None:
        if self._thread is not None:
            return
        with self._io_lock:
            self._check(self.lib.rtlsdr_reset_buffer(self._handle),
                        "reset the device's USB buffer")
        self._stop.clear()
        self.read_error = ""
        self._thread = threading.Thread(
            target=self._reader, name="gsu-rtlsdr", daemon=True
        )
        self._thread.start()

    def stop_stream(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # Generous against a 34 ms read: if this times out something is
            # badly wrong and closing under it risks the wedge, so it is logged
            # rather than forced.
            thread.join(timeout=3.0)
            if thread.is_alive():
                log.error(
                    "The RTL-SDR reader did not stop within 3s. Not closing the "
                    "device under it: a transfer interrupted mid-flight wedges "
                    "the tuner until it is physically unplugged."
                )

    def _reader(self) -> None:
        buffer = (ctypes.c_ubyte * READ_BYTES)()
        read = ctypes.c_int(0)
        failures = 0
        while not self._stop.is_set():
            with self._io_lock:
                if not self._open:
                    return
                result = int(self.lib.rtlsdr_read_sync(
                    self._handle, ctypes.byref(buffer), READ_BYTES, ctypes.byref(read)
                ))
            if result < 0 or read.value <= 0:
                failures += 1
                if failures >= 3:
                    self.read_error = (
                        f"the dongle stopped delivering samples (rtlsdr_read_sync "
                        f"returned {result}). It runs hot and often refuses to "
                        "stream after a long session; unplug it, let it cool, "
                        "and plug it back in."
                    )
                    log.error("RTL-SDR reader stopping: %s", self.read_error)
                    return
                time.sleep(0.05)
                continue
            failures = 0
            # Bank the RAW bytes — a memcpy of a few microseconds — and defer
            # the uint8->complex64 conversion to drain() on the consumer thread.
            # On a slow host that conversion used to run in the gap between one
            # read_sync and the next, with no USB transfer in flight, and the
            # dongle's small FIFO overflowed into it: a couple percent of samples
            # lost, straight into audio underruns. A copy keeps the gap to a
            # memcpy; the arithmetic happens where there is budget for it (the
            # demod runs at ~10x real time). See drain().
            raw = np.frombuffer(buffer, dtype=np.uint8, count=read.value).copy()
            with self._buffer_lock:
                if len(self._buffer) == self._buffer.maxlen:
                    # deque drops the oldest for us; count it so the front end
                    # can report a tick loop that is not keeping up rather than
                    # letting the audio quietly fall behind.
                    self.dropped_blocks += 1
                self._buffer.append(raw)

    def drain(self) -> np.ndarray:
        """Everything buffered since the last call, oldest first, as complex64.

        The reader banks raw ADC bytes; the uint8->complex64 conversion happens
        here, off the read loop, so the loop never stalls the USB pipe long
        enough to drop samples into the gap. See `_reader`.
        """
        with self._buffer_lock:
            blocks = list(self._buffer)
            self._buffer.clear()
        if not blocks:
            return np.zeros(0, dtype=np.complex64)
        return to_complex(np.concatenate(blocks))

    def flush(self) -> None:
        with self._buffer_lock:
            self._buffer.clear()

    def buffered_samples(self) -> int:
        # Blocks are raw ADC bytes now (two per complex sample), converted only
        # in drain(); report complex-sample counts so callers still see IQ.
        with self._buffer_lock:
            return sum(block.size for block in self._buffer) // 2

    # --- plumbing ---------------------------------------------------------

    def _check(self, result: int, what: str) -> None:
        if int(result) < 0:
            raise RtlError(f"could not {what} (librtlsdr returned {result})")
