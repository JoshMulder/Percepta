"""The RtlDevice async reader, against a fake librtlsdr.

The front-end tests (test_radio_rtlsdr) swap in a FakeDongle and never touch
this layer, so the `rtlsdr_read_async` reader — the one that replaced the
sync read to stop the Pi 2B shedding ~1-2% of samples — needs its own cover:
that it banks buffers the callback delivers, that a control transfer never runs
while the stream is live (the wedge), and that it stops without hanging.

No hardware and no real library: `load_library` is patched to a fake that calls
the callback in a loop and records whether the stream was live when a control
transfer landed.
"""

import ctypes
import threading
import time
import unittest

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

if np is not None:
    from gsu.radio import rtl2832


class FakeLib:
    """Enough of librtlsdr for the async reader: a read loop that calls the
    callback until cancelled, and control calls that note if they ran mid-stream."""

    def __init__(self):
        self._cancel = threading.Event()
        self.in_async = threading.Event()
        self.gain = 0
        self.gain_set_while_streaming = None
        self.reset_calls = 0
        self.closed = False

    def rtlsdr_reset_buffer(self, handle):
        self.reset_calls += 1
        return 0

    def rtlsdr_read_async(self, handle, cb, ctx, num, length):
        self._cancel.clear()
        self.in_async.set()
        buf = (ctypes.c_ubyte * length)()
        for i in range(length):
            buf[i] = i & 0xFF
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        while not self._cancel.is_set():
            cb(ptr, length, ctx)          # invokes RtlDevice._on_samples
            time.sleep(0.002)
        self.in_async.clear()
        return 0

    def rtlsdr_cancel_async(self, handle):
        self._cancel.set()
        return 0

    def rtlsdr_set_tuner_gain_mode(self, handle, mode):
        return 0

    def rtlsdr_set_tuner_gain(self, handle, gain):
        # The whole safety property: this control transfer must not overlap a
        # bulk one, so the stream must be paused (not in read_async) right now.
        self.gain_set_while_streaming = self.in_async.is_set()
        self.gain = gain
        return 0

    def rtlsdr_get_tuner_gain(self, handle):
        return self.gain

    def rtlsdr_close(self, handle):
        self.closed = True
        return 0


@unittest.skipIf(np is None, "numpy is not installed")
class AsyncReaderTests(unittest.TestCase):
    def make(self):
        self.lib = FakeLib()
        original = rtl2832.load_library
        rtl2832.load_library = lambda: self.lib
        self.addCleanup(setattr, rtl2832, "load_library", original)
        device = rtl2832.RtlDevice()
        device._open = True
        device._handle = ctypes.c_void_p(1)
        self.addCleanup(device.stop_stream)
        return device

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_the_async_reader_banks_what_the_callback_delivers(self):
        device = self.make()
        device.start_stream()
        self.assertTrue(self._wait(lambda: device.buffered_samples() > 0),
                        "the reader banked nothing from read_async")
        samples = device.drain()
        self.assertGreater(samples.size, 0)
        self.assertEqual(samples.dtype, np.complex64)

    def test_a_control_transfer_never_overlaps_the_stream(self):
        device = self.make()
        device.start_stream()
        self.assertTrue(self._wait(lambda: self.lib.in_async.is_set()),
                        "the stream never went live")
        device.set_gain(25.4)             # a mid-stream gain change
        self.assertEqual(self.lib.gain, 254)
        self.assertIs(self.lib.gain_set_while_streaming, False,
                      "gain was set while a bulk transfer was in flight — the wedge")
        # And the stream comes back after the pause.
        self.assertTrue(self._wait(lambda: self.lib.in_async.is_set()),
                        "the reader did not resume after the control transfer")

    def test_stop_stream_cancels_cleanly(self):
        device = self.make()
        device.start_stream()
        self.assertTrue(self._wait(lambda: self.lib.in_async.is_set()))
        device.stop_stream()
        self.assertTrue(self._wait(lambda: not self.lib.in_async.is_set()),
                        "read_async did not return on cancel")
        self.assertIsNone(device._thread)
        self.assertEqual(device.read_error, "", "a clean stop is not an error")
