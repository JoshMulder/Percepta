"""Build a synthetic MPEG-TS stream for the reader tests.

The RTSP remux now reads the camera over MPEG-TS to recover its PES timestamps
(`gsu/media/mpegts.py`), and the reader has never met a real camera — so it is
proven against transport streams built here, the same way the synthetic H.264
source proves the encoder path. This is the muxing side of that: real access-unit
bytes plus a chosen PTS in, byte-exact 188-byte transport packets out.

Shared by `test_video.py` (the reader in isolation) and `test_rtsp.py` (the whole
pump → reader → muxer path over a pipe), so the two cannot drift on what a valid
transport packet looks like.
"""

from __future__ import annotations


def pes_packet(stream_id: int, pts: int | None, payload: bytes) -> bytes:
    """One PES packet carrying `payload`, as ffmpeg's mpegts muxer writes for a
    video access unit: a start code, the stream id, an unbounded length, and an
    optional header whose PTS (when present) is the five-byte, split-by-marker-
    bits form of ISO/IEC 13818-1 §2.4.3.7. `pts=None` writes the header with no
    timestamp, which is how a frame comes back with `pts` None."""
    if pts is None:
        header = bytes((0x80, 0x00, 0x00))      # '10' marker, no PTS/DTS, len 0
        return b"\x00\x00\x01" + bytes((stream_id,)) + b"\x00\x00" + header + payload
    pts &= (1 << 33) - 1
    timestamp = bytes((
        0x21 | ((pts >> 29) & 0x0E),            # 0010 + PTS[32:30] + marker
        (pts >> 22) & 0xFF,                     # PTS[29:22]
        0x01 | ((pts >> 14) & 0xFE),            # PTS[21:15] + marker
        (pts >> 7) & 0xFF,                      # PTS[14:7]
        0x01 | ((pts << 1) & 0xFE),             # PTS[6:0] + marker
    ))
    header = bytes((0x80, 0x80, len(timestamp))) + timestamp
    return b"\x00\x00\x01" + bytes((stream_id,)) + b"\x00\x00" + header + payload


def ts_packets(pid: int, payload: bytes) -> bytes:
    """`payload` split across 188-byte transport packets on `pid`, the first
    flagged as a payload-unit start and the last padded with an adaptation field
    so every packet is exactly 188 bytes — the shape the reader consumes."""
    out = bytearray()
    view = memoryview(payload)
    first = True
    while view:
        pusi = 0x40 if first else 0x00
        chunk = view[:184]
        pad = 184 - len(chunk)
        header = bytes((
            0x47,
            pusi | ((pid >> 8) & 0x1F),
            pid & 0xFF,
            (0x30 if pad else 0x10),            # adaptation+payload, or payload
        ))
        if pad:
            af_len = pad - 1
            field = bytes((af_len,)) + (
                bytes((0x00,)) + b"\xFF" * (af_len - 1) if af_len else b"")
            out += header + field + bytes(chunk)
        else:
            out += header + bytes(chunk)
        view = view[len(chunk):]
        first = False
    return bytes(out)


def build_ts(payloads, pts, pid: int = 0x100, stream_id: int = 0xE0) -> bytes:
    """A whole transport stream from `(access-unit-bytes, pts)` pairs."""
    return b"".join(
        ts_packets(pid, pes_packet(stream_id, p, data))
        for data, p in zip(payloads, pts)
    )
