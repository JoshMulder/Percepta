"""The audio wire format, in one function, because it changed.

`contract/schemas/audio.schema.json` fixes it as Opus: raw packets, no
container, base64'd into an array, with the stream's parameters stated in the
same frame. The numbers behind that are in `contract/transport.md` — about
384 kbit/s for 24 kHz PCM16 against 16–24 kbit/s for Opus, per listener, on a
link somebody pays for by the gigabyte.

This file used to say "everything that turns samples into a payload is here, so
moving to Opus is this file plus a schema change, not a search for
`b64encode`". That turned out to be true, which is the only reason it is worth
repeating: the rest of the station passes samples around and knows nothing
about the wire.

**Both gates still apply above this.** A packet is built only when the squelch
is open *and* a lease is live; this file does not know about either, and a
caller that forgets one produces perfectly-formed audio nobody asked for.
"""

from __future__ import annotations

import base64
import struct
from typing import Iterable

from .opus import FRAME_MS, Encoder, OpusUnavailable

#: What the console expects; it resamples to whatever the browser grants.
AUDIO_RATE = 24000

#: The contract requires at least four packets in a frame. Fewer would be a
#: JSON envelope per 20 ms of speech, which is most of a frame spent on
#: punctuation — the schema states the floor so a station cannot accidentally
#: turn a bandwidth saving back into an overhead.
MIN_PACKETS = 4

__all__ = ["AUDIO_RATE", "MIN_PACKETS", "Encoder", "OpusUnavailable",
           "to_pcm16", "audio_payload"]


def to_pcm16(samples: Iterable[float]) -> bytes:
    """Float samples in [-1, 1] to mono signed 16-bit little-endian PCM.

    Still here, and still the recording format: what goes to local storage is
    PCM because a WAV on disk can be opened by anything, and what goes on the
    wire is Opus because the wire is metered. Those are different problems and
    they get different answers.
    """
    clipped = [max(-1.0, min(1.0, value)) for value in samples]
    return struct.pack(f"<{len(clipped)}h", *[int(value * 32767) for value in clipped])


def audio_payload(pcm: bytes, encoder: Encoder,
                  rate: int = AUDIO_RATE) -> dict | None:
    """One audio frame, or None if there is not yet enough to send.

    None is not a failure. It means this tick produced fewer than the four
    packets the contract requires, so the caller keeps the samples and tries
    again — 80 ms of speech is not worth an envelope of its own.
    """
    packets = encoder.encode(pcm)
    if len(packets) < MIN_PACKETS:
        return None
    return {
        "kind": "audio",
        "codec": "opus",
        "rate": rate,
        "channels": 1,
        "frame_ms": FRAME_MS,
        "packets": [base64.b64encode(packet).decode() for packet in packets],
    }
