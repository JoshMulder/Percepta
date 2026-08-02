"""Opus, through ctypes, for the station simulator.

The station has its own binding (`station/gsu/radio/opus.py`) and this is
deliberately a second one rather than an import. `contract/README.md` splits
ownership at the contract: neither side reads the other's code, and two
independent implementations of the same four C functions is the *point* — it
is how the simulator stays an honest reference for what a platform will
receive rather than a mirror of what one station happens to send.

Encode only. Nothing on this side decodes Opus; the console does that in the
browser with WebCodecs, from the same raw packets.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

log = logging.getLogger(__name__)

_APPLICATION_VOIP = 2048
_SET_BITRATE = 4002
_MAX_PACKET = 1024

#: 20 ms, which is Opus's default and the value the contract states.
FRAME_MS = 20


class OpusUnavailable(RuntimeError):
    """libopus is missing, so this simulator cannot produce contract audio."""


class Encoder:
    """One Opus encoder, held for the life of the stream.

    Held rather than rebuilt per frame because Opus carries prediction state
    between packets: a fresh encoder each time decodes as if every frame
    followed silence, which costs bitrate and sounds worse.
    """

    def __init__(self, rate: int, channels: int = 1, bitrate: int = 24000) -> None:
        name = ctypes.util.find_library("opus") or "libopus.so.0"
        try:
            self._lib = ctypes.CDLL(name)
        except OSError as exc:
            raise OpusUnavailable(
                "libopus is not installed. Contract 2.0 carries audio as Opus, "
                "so this simulator would otherwise publish a format the "
                "platform's own schema rejects — which is worse than silence, "
                "because it is the designated reference implementation."
            ) from exc

        self._lib.opus_encoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self._lib.opus_encoder_create.restype = ctypes.c_void_p
        self._lib.opus_encode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
        ]
        self._lib.opus_encode.restype = ctypes.c_int32
        self._lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        self._lib.opus_encoder_destroy.restype = None

        self.rate = rate
        self.channels = channels
        self.frame_samples = rate * FRAME_MS // 1000

        error = ctypes.c_int(0)
        self._state = self._lib.opus_encoder_create(
            rate, channels, _APPLICATION_VOIP, ctypes.byref(error))
        if not self._state or error.value != 0:
            raise OpusUnavailable(
                f"opus_encoder_create failed at {rate} Hz: error {error.value}")
        self._lib.opus_encoder_ctl(self._state, _SET_BITRATE,
                                   ctypes.c_int32(bitrate))

    def encode(self, pcm: bytes) -> list[bytes]:
        """PCM16 mono to Opus packets, one per 20 ms.

        A trailing partial frame is dropped rather than padded: padding invents
        silence that was never on the air.
        """
        frame_bytes = self.frame_samples * 2 * self.channels
        packets: list[bytes] = []
        buffer = (ctypes.c_ubyte * _MAX_PACKET)()
        for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            chunk = pcm[start:start + frame_bytes]
            samples = (ctypes.c_int16 * (len(chunk) // 2)).from_buffer_copy(chunk)
            written = self._lib.opus_encode(
                self._state, samples, self.frame_samples, buffer, _MAX_PACKET)
            if written < 0:
                log.warning("opus_encode returned %d; dropping a frame.", written)
                continue
            packets.append(bytes(buffer[:written]))
        return packets

    def close(self) -> None:
        state, self._state = getattr(self, "_state", None), None
        if state:
            self._lib.opus_encoder_destroy(state)
