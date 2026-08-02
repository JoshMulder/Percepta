"""Opus, through ctypes, because the encoder API is four functions.

`contract/schemas/audio.schema.json` fixes airband audio as Opus packets — raw,
no container, base64'd into an array. The numbers behind that are in
`contract/transport.md`: about 384 kbit/s for 24 kHz PCM16 against 16–24 kbit/s
for Opus, per listener, on a metered link somebody is paying for by the gigabyte.

WHY A BINDING AND NOT A PACKAGE
-------------------------------
The alternatives were `opuslib` — itself a thin ctypes wrapper, so the same
`libopus0` from apt plus a pip package to add nothing — and PyAV, which is all
of ffmpeg's API surface to reach four C functions.

The station's stated property is that it boots with what is in its image and
never installs anything, and it already drives the SDR through libusb this way
(`rtl2832.py`). A protocol this small is less to carry than a library that
speaks it.

WHAT IT DOES NOT DO
-------------------
**No decoding.** Nothing on a station listens to airband; it captures, records
and forwards. The console decodes, in the browser, with `AudioDecoder`.

**No container.** Opus is normally wrapped in Ogg or WebM, and the contract
deliberately carries raw packets instead: a container exists to make a stream
seekable and self-describing on disk, and this is neither — it is a live feed
whose parameters are stated in the same JSON frame as the packets.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

log = logging.getLogger("gsu.radio.opus")

#: `OPUS_APPLICATION_VOIP`. The other choices are AUDIO (music, more delay) and
#: RESTRICTED_LOWDELAY. Airband is speech through a noisy AM channel, which is
#: what VOIP mode is tuned for.
_APPLICATION_VOIP = 2048

#: Frame length. 20 ms is Opus's default and the contract's stated value; it is
#: also the point where the codec's own framing overhead stops mattering.
FRAME_MS = 20

#: A generous ceiling for one encoded frame. The schema caps a base64 packet at
#: 2048 characters, which is 1536 bytes — this is under that with room, and
#: `opus_encode` never needs anything close at speech bitrates.
_MAX_PACKET = 1024


class OpusUnavailable(RuntimeError):
    """libopus is not on this box, so audio cannot be published.

    Raised rather than falling back to PCM. A station that quietly published a
    format the contract does not define would be refused by the platform's
    schema check and look, from the field, exactly like a station with a quiet
    channel — which is the failure mode this contract exists to delete.
    """


def _load() -> ctypes.CDLL:
    name = ctypes.util.find_library("opus") or "libopus.so.0"
    library = ctypes.CDLL(name)

    library.opus_encoder_create.argtypes = [
        ctypes.c_int32, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    library.opus_encoder_create.restype = ctypes.c_void_p
    library.opus_encode.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
    ]
    library.opus_encode.restype = ctypes.c_int32
    library.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    library.opus_encoder_destroy.restype = None
    return library


class Encoder:
    """One Opus encoder. Stateful, and that is the point.

    An encoder carries prediction state between frames, so the same PCM
    produces different bytes depending on what came before it — which is why
    this is an object held for the life of the receiver rather than a function
    called per frame. Constructing a fresh one per transmission would throw
    that away and cost bitrate for nothing.
    """

    def __init__(self, rate: int, channels: int = 1) -> None:
        try:
            self._lib = _load()
        except OSError as exc:
            raise OpusUnavailable(
                "libopus is not installed. Contract 2.0 carries airband audio "
                "as Opus and this station will not publish anything else; "
                "install libopus0 (it is in deploy/Dockerfile) or fit no "
                "receiver."
            ) from exc

        self.rate = rate
        self.channels = channels
        self.frame_samples = rate * FRAME_MS // 1000

        error = ctypes.c_int(0)
        self._state = self._lib.opus_encoder_create(
            rate, channels, _APPLICATION_VOIP, ctypes.byref(error))
        if not self._state or error.value != 0:
            raise OpusUnavailable(
                f"opus_encoder_create failed for {rate} Hz "
                f"({channels} channel(s)): error {error.value}")
        # **The bitrate is left at Opus's own default, deliberately.**
        #
        # Setting it means `opus_encoder_ctl`, which is variadic — and ctypes
        # cannot call a variadic function correctly without knowing which
        # arguments are variadic. On x86_64 that is survivable, because fixed
        # and variadic arguments share a calling convention. On aarch64 they do
        # not, and the call corrupts the stack: the agent segfaulted (exit 139)
        # about twenty seconds into every run on a Pi 5, restarting forever,
        # while passing every test on an x86_64 development machine.
        #
        # The default is what the measurement was taken against anyway —
        # 400 ms of speech encoded to 21.7 kbit/s, inside the contract's stated
        # 16-24. Nothing is bought by setting it, and a variadic ctypes call is
        # a hazard on every architecture this has not been run on.

    def encode(self, pcm: bytes) -> list[bytes]:
        """PCM16 mono to a list of Opus packets, one per 20 ms.

        A trailing partial frame is **dropped, not padded**. Padding invents
        silence that was never on the air, and at 20 ms the loss is inaudible —
        whereas a station that pads is a station whose recordings and whose
        stream disagree about what happened.
        """
        if self._state is None:
            raise OpusUnavailable("this encoder has been closed")

        frame_bytes = self.frame_samples * 2 * self.channels
        packets: list[bytes] = []
        buffer = (ctypes.c_ubyte * _MAX_PACKET)()

        for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            chunk = pcm[start:start + frame_bytes]
            samples = (ctypes.c_int16 * (len(chunk) // 2)).from_buffer_copy(chunk)
            written = self._lib.opus_encode(
                self._state, samples, self.frame_samples, buffer, _MAX_PACKET)
            if written < 0:
                # Negative is an Opus error code. Dropping the frame is right:
                # audio is a stream, the next one is 20 ms away, and raising
                # here would take down the sensing loop over one bad packet.
                log.warning("opus_encode returned %d; dropping a frame.", written)
                continue
            packets.append(bytes(buffer[:written]))
        return packets

    def close(self) -> None:
        state, self._state = self._state, None
        if state:
            self._lib.opus_encoder_destroy(state)

    def __del__(self) -> None:          # pragma: no cover - interpreter teardown
        try:
            self.close()
        except Exception:               # noqa: BLE001
            pass
