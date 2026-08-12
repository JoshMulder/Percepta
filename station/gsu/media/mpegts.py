"""MPEG-TS, read here, only to recover the timestamps a raw Annex B stream threw away.

This is the reading half of the night-stutter fix, and it exists because of one
fact about a camera after dark: it drops to a few frames a second with long
exposures, but it stamps every frame with a real presentation time. `ffprobe`
on the Kennels Rd camera showed those stamps landing at exactly 0.2, 0.4, 0.6 s
— uniform, correct, and the timeline the picture should play on.

The remux used to read the camera as raw Annex B (`ffmpeg -c copy -f h264`),
which is a byte stream with no timestamps in it at all. The fMP4 muxer then had
to *invent* a timeline, and every invention failed on the real station: a fixed
nominal rate raced 5x fast, a per-frame arrival gap amplified jitter, a smoothed
delivery rate wobbled. None of them could have worked, because the information
was being discarded one stage upstream — the camera knew the timing and nobody
was reading it.

So the remux now asks ffmpeg for `-f mpegts` instead. MPEG-TS wraps the same
copied elementary stream in PES packets that carry the RTP timestamps forward as
PTS, and this reader unwraps them: 188-byte transport packets, filtered to the
one video PID, reassembled into PES packets, each PES yielding one access unit
and its PTS. The PTS is in 90 kHz, which is the fMP4 muxer's own timescale, so
the gap between two frames' timestamps is a sample duration in muxer ticks with
nothing to scale or estimate. See `gsu/stream.py` for where that gap is taken,
and `gsu/media/fmp4.py:Fmp4Muxer.feed` for the `duration` it feeds.

**One PES packet is one access unit.** That is the assumption this reader is
built on, and it is what ffmpeg's mpegts muxer produces for a video stream: each
coded picture is packetised into its own PES with the picture's PTS on it, and a
new PES is marked by the transport packet's `payload_unit_start_indicator`. The
same shape of assumption `AnnexBReader` makes about one slice per picture, and
stated the same way rather than hidden.

**The video PID is found, not assumed.** `-an -dn` on the remux command means
there is exactly one elementary stream, so the reader locks onto the first PID
that carries a video PES (a PES start code with a video stream id) and ignores
the program tables entirely — there is nothing to disambiguate, and parsing the
PAT and PMT would be code to identify the one stream there could ever be. Audio
PES (stream ids 0xC0-0xDF) and the PSI tables (which never begin with a PES start
code) cannot be mistaken for it.
"""

from __future__ import annotations

from .. import clock
from ..camera.h264 import H264, AccessUnit, NalRules, split_annexb

#: A transport packet is always 188 bytes and always begins with this sync byte.
PACKET = 188
SYNC = 0x47

#: The null-packet PID, which carries only stuffing and is dropped.
NULL_PID = 0x1FFF

#: Video PES stream ids are 0xE0-0xEF (§2.4.3.7). Audio is 0xC0-0xDF, which is
#: how a stream id tells the two apart — though `-an` means no audio is present
#: anyway.
_VIDEO_STREAM_ID = range(0xE0, 0xF0)


def _is_video_pes(payload: bytes) -> bool:
    """Whether a transport payload is the start of a video PES packet.

    A PES packet begins with the start code `00 00 01` and a stream id. The PSI
    tables (PAT, PMT) begin with a pointer byte and a table id instead, so they
    never match; audio PES matches the start code but not the video stream id.
    """
    return (
        len(payload) >= 4
        and payload[0] == 0 and payload[1] == 0 and payload[2] == 1
        and payload[3] in _VIDEO_STREAM_ID
    )


def _read_timestamp(data: bytes, at: int) -> int:
    """A 33-bit PTS/DTS out of its five bytes — ISO/IEC 13818-1 §2.4.3.7.

    The value is split across the five bytes by marker bits that have to be
    masked out: three bits in the first byte, then two runs of fifteen. Reading
    it as a plain 40-bit integer would fold the marker bits into the timestamp
    and put every frame a little in the wrong place.
    """
    return (
        ((data[at] >> 1) & 0x07) << 30
        | data[at + 1] << 22
        | ((data[at + 2] >> 1) & 0x7F) << 15
        | data[at + 3] << 7
        | ((data[at + 4] >> 1) & 0x7F)
    )


def _parse_pes(payload: bytes) -> tuple[int | None, bytes]:
    """`(pts, elementary-stream bytes)` from the first packet of a PES.

    The PES header is a start code, a stream id, a two-byte length (zero and
    unbounded for video), then an optional header whose `PTS_DTS_flags` say
    whether a PTS follows and whose length says where the elementary stream
    begins. A PES with no PTS comes back with `None`, and the frame then paces
    at the muxer's nominal rate rather than from a timestamp that is not there.
    """
    if len(payload) < 9:
        return None, b""
    pts_dts_flags = (payload[7] >> 6) & 0x03
    header_length = payload[8]
    pts = None
    if pts_dts_flags & 0x02 and len(payload) >= 14:
        # 0b10 is PTS only, 0b11 is PTS then DTS; the PTS is in the same place
        # either way. This camera sends no B-frames, so PTS equals DTS and the
        # presentation gap is the decode gap — the muxer wants exactly one of
        # them and the PTS is the one that is always present.
        pts = _read_timestamp(payload, 9)
    elementary = payload[9 + header_length:]
    return pts, bytes(elementary)


class TsReader:
    """Transport-stream bytes in, access units out — the same shape as
    `AnnexBReader`, so the encoder pump downstream cannot tell which it holds.

    `rules` says which codec's NAL headers the elementary stream carries — H.264
    by default, HEVC for a camera streaming H.265 — and is used only to read the
    keyframe flag out of the recovered access units. Everything above that, the
    transport and PES framing, is codec-blind.
    """

    def __init__(self, rules: NalRules = H264) -> None:
        self.rules = rules
        self._buffer = bytearray()
        self._video_pid: int | None = None
        #: The elementary-stream bytes of the PES currently being reassembled,
        #: and its PTS. `_have_pes` is False until the first PES start is seen,
        #: which is what keeps a stream that begins mid-PES from emitting a
        #: half access unit.
        self._pes = bytearray()
        self._pts: int | None = None
        self._have_pes = False

    def feed(self, chunk: bytes) -> list[AccessUnit]:
        self._buffer += chunk
        buffer = self._buffer
        units: list[AccessUnit] = []
        index = 0
        length = len(buffer)
        while length - index >= PACKET:
            if buffer[index] != SYNC:
                # Misaligned — a partial packet, or a byte lost off the front.
                # Resynchronise on the next sync byte rather than trusting the
                # offset, which is what keeps one dropped byte from turning the
                # whole rest of the stream into garbage.
                nxt = buffer.find(b"\x47", index + 1)
                if nxt < 0:
                    index = length
                    break
                index = nxt
                continue
            unit = self._packet(buffer[index:index + PACKET])
            if unit is not None:
                units.append(unit)
            index += PACKET
        del buffer[:index]
        return units

    def flush(self) -> list[AccessUnit]:
        """Release the last PES, which has no following packet to close it.

        The final access unit of a stream is still being reassembled when the
        encoder stops — nothing marks its end but the end of the stream — so it
        is emitted here or it is lost, the same one-frame-short the AnnexBReader
        flush prevents.
        """
        if not self._have_pes:
            return []
        unit = self._emit()
        return [unit] if unit is not None else []

    def _packet(self, packet: bytes) -> AccessUnit | None:
        """One 188-byte transport packet, or None if it yields no access unit."""
        if packet[1] & 0x80:                   # transport_error_indicator
            return None
        payload_start = bool(packet[1] & 0x40)  # payload_unit_start_indicator
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid == NULL_PID:
            return None
        adaptation = (packet[3] >> 4) & 0x03
        if not adaptation & 0x01:              # no payload (adaptation only)
            return None
        offset = 4
        if adaptation & 0x02:                  # adaptation field before payload
            offset = 5 + packet[4]
            if offset >= PACKET:
                return None
        payload = bytes(packet[offset:])

        if self._video_pid is None:
            if payload_start and _is_video_pes(payload):
                self._video_pid = pid
            else:
                return None
        if pid != self._video_pid:
            return None
        return self._video(payload, payload_start)

    def _video(self, payload: bytes, payload_start: bool) -> AccessUnit | None:
        """A transport payload on the video PID. Returns the completed access
        unit when this payload starts a new PES and closes the previous one."""
        unit = None
        if payload_start:
            if self._have_pes:
                unit = self._emit()            # reads self._pts before it resets
            self._pts, elementary = _parse_pes(payload)
            self._pes = bytearray(elementary)
            self._have_pes = True
        elif self._have_pes:
            self._pes += payload
        return unit

    def _emit(self) -> AccessUnit | None:
        self._have_pes = False
        data = bytes(self._pes)
        self._pes = bytearray()
        pts, self._pts = self._pts, None
        nals = split_annexb(data)
        if not nals:
            return None
        rules = self.rules
        return AccessUnit(
            data=b"".join(b"\x00\x00\x00\x01" + nal for nal in nals),
            captured_at=clock.now(),
            keyframe=any(rules.nal_type(nal) in rules.keyframes for nal in nals),
            nals=tuple(nals),
            pts=pts,
        )
