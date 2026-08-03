"""Listen to what a station actually published, decoded on the platform.

**A diagnostic, not a feature.** It exists to cut a specific argument in half.

Airband audio that chops in the console has four suspects between the
demodulator and the speaker: the station's Opus encoder, the broker relay, this
platform's fan-out, and the browser's player. The station's own `/audio.wav`
takes the demodulator's PCM before any of them, so a stream that is clean there
and chopped in a console proves the fault is downstream — but not *which*
downstream.

This is the next cut. It subscribes to the same internal channel the console's
fan-out reads, decodes the Opus packets the station published, and writes them
out as PCM. Everything up to and including the relay is therefore in the path,
and everything after it is not:

    clean here  -> the encoder and the relay are fine; look at the fan-out to
                   the console websocket, or at the browser's player
    chopped here -> the fault is at or before the relay, and the station's own
                   /audio.wav already exonerated the demodulator, which leaves
                   the Opus encoder

Piped rather than written to a file, so it can go straight into a player:

    ssh percepta@<platform> 'cd ~/percepta/server && \
        docker compose exec -T app python -m backend.scripts.audio_tap' \
        | ffplay -autoexit -

    ...or with --seconds and a redirect, for something to keep and compare:

    docker compose exec -T app python -m backend.scripts.audio_tap \
        --seconds 30 > /tmp/tap.wav

Gaps are written as silence rather than skipped. A capture that simply stalls
between transmissions cannot be told apart from one that is dropping audio,
which is the whole question being asked — so the timeline stays real and a gap
arrives as a gap, exactly as it does on the station's own tap.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import json
import sys
import time

import redis

from backend.core.config import settings

#: The station states its own rate in every frame; this is only the fallback
#: for a capture that starts before the first frame arrives.
DEFAULT_RATE = 24000

#: Enough for one decoded frame at any rate the contract allows. 120 ms at
#: 48 kHz stereo is the theoretical worst case for a single Opus packet.
_MAX_FRAME_SAMPLES = 5760


class _Decoder:
    """`opus_decode` through ctypes, mirroring the station's encoder.

    Same four-function surface and the same reason for it: the contract carries
    raw Opus packets with no container, so there is nothing to demux and a
    decoder is `create`, `decode`, `destroy`.
    """

    def __init__(self, rate: int, channels: int = 1) -> None:
        name = ctypes.util.find_library("opus") or "libopus.so.0"
        self._lib = ctypes.CDLL(name)
        self._lib.opus_decoder_create.restype = ctypes.c_void_p
        self._lib.opus_decoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self._lib.opus_decode.restype = ctypes.c_int
        self._lib.opus_decode.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        self._lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]

        self.rate, self.channels = rate, channels
        error = ctypes.c_int(0)
        self._state = self._lib.opus_decoder_create(
            rate, channels, ctypes.byref(error))
        if not self._state or error.value != 0:
            raise RuntimeError(
                f"opus_decoder_create failed for {rate} Hz: {error.value}")

    def decode(self, packet: bytes) -> bytes:
        buffer = (ctypes.c_int16 * (_MAX_FRAME_SAMPLES * self.channels))()
        written = self._lib.opus_decode(
            self._state, packet, len(packet), buffer, _MAX_FRAME_SAMPLES, 0)
        if written < 0:
            # Negative is an Opus error code. Dropped rather than raised: this
            # is a listening tool, and one bad packet is information about the
            # stream rather than a reason to stop reporting on it.
            print(f"opus_decode returned {written}; dropping a packet.",
                  file=sys.stderr)
            return b""
        return bytes(buffer)[:written * self.channels * 2]

    def close(self) -> None:
        state, self._state = self._state, None
        if state:
            self._lib.opus_decoder_destroy(state)


def wav_header(rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """RIFF for a stream whose length nobody knows yet — see the station's."""
    block = channels * bits // 8
    return b"".join((
        b"RIFF", b"\xff\xff\xff\xff", b"WAVE",
        b"fmt ", (16).to_bytes(4, "little"),
        (1).to_bytes(2, "little"),
        channels.to_bytes(2, "little"),
        rate.to_bytes(4, "little"),
        (rate * block).to_bytes(4, "little"),
        block.to_bytes(2, "little"),
        bits.to_bytes(2, "little"),
        b"data", b"\xff\xff\xff\xff",
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", default="*",
                        help="station id, or * for whichever is publishing")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop after this long; 0 runs until interrupted")
    parser.add_argument("--quiet", action="store_true",
                        help="no running commentary on stderr")
    args = parser.parse_args()

    client = redis.Redis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    pubsub.psubscribe(f"gsu/{args.station}/audio")

    out = sys.stdout.buffer
    decoder: _Decoder | None = None
    started = time.monotonic()
    deadline = started + args.seconds if args.seconds else None
    # Wall-clock position of the audio already written, so a gap between
    # transmissions can be filled rather than closed up.
    written_s = 0.0
    frames = packets = 0

    try:
        while deadline is None or time.monotonic() < deadline:
            message = pubsub.get_message(timeout=1.0)
            if not message or message.get("type") != "pmessage":
                continue
            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                continue
            rate = int(payload.get("rate") or DEFAULT_RATE)
            channels = int(payload.get("channels") or 1)

            if decoder is None:
                decoder = _Decoder(rate, channels)
                out.write(wav_header(rate, channels))
                out.flush()
                if not args.quiet:
                    print(f"tapping {message['channel'].decode()} at {rate} Hz",
                          file=sys.stderr)

            # Silence for the time nothing was published, so what comes out is
            # as long as the wall clock says it should be. Without this a tap
            # of a quiet channel plays back as continuous speech and every gap
            # the operator complained about disappears from the recording.
            elapsed = time.monotonic() - started
            missing = elapsed - written_s
            if missing > 0.04:
                out.write(b"\x00\x00" * int(rate * missing) * channels)
                written_s += missing

            for encoded in payload.get("packets") or []:
                try:
                    pcm = decoder.decode(base64.b64decode(encoded))
                except Exception as exc:  # noqa: BLE001 - keep listening
                    print(f"packet dropped: {exc}", file=sys.stderr)
                    continue
                out.write(pcm)
                written_s += len(pcm) / 2 / channels / rate
                packets += 1
            out.flush()
            frames += 1
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if decoder is not None:
            decoder.close()
        with_ = time.monotonic() - started
        if not args.quiet:
            print(f"\n{frames} frames, {packets} packets, "
                  f"{written_s:.1f}s of audio in {with_:.1f}s of wall clock "
                  f"(ratio {written_s / with_ if with_ else 0:.4f})",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
