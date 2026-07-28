"""The audio wire format, in one function, because it is going to change.

`contract/schemas/audio.schema.json` says what it is today — base64 PCM16 in
JSON — and says in the same breath that it is "wasteful and known to be so", and
to expect binary frames with Opus. `contract/transport.md` puts numbers on it:
~384 kbit/s uncompressed per listener against 16–24 kbit/s for Opus, on a
metered link.

So everything that turns samples into a payload is here, and the rest of the
station passes samples around. Moving to Opus is this file plus a schema change,
not a search for `b64encode`.
"""

from __future__ import annotations

import base64
import struct
from typing import Iterable

#: What the console expects today; it resamples to whatever the browser grants.
AUDIO_RATE = 24000


def to_pcm16(samples: Iterable[float]) -> bytes:
    """Float samples in [-1, 1] to mono signed 16-bit little-endian PCM."""
    clipped = [max(-1.0, min(1.0, value)) for value in samples]
    return struct.pack(f"<{len(clipped)}h", *[int(value * 32767) for value in clipped])


def audio_payload(pcm: bytes, rate: int = AUDIO_RATE) -> dict:
    """One `gsu/{station_id}/audio` message."""
    return {
        "kind": "audio",
        "rate": rate,
        "pcm": base64.b64encode(pcm).decode(),
    }
