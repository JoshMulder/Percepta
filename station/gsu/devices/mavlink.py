"""Just enough MAVLink to read an ADS-B receiver, and all the unit scaling.

The uAvionix ping RX Pro emits `ADSB_VEHICLE` (#246) over a serial link. This
module parses MAVLink v1 and v2 frames, validates their checksums, and decodes
that one message. It deliberately does not depend on pymavlink: one message, on
a box that must boot from its image, is not worth a code generator.

**Every unit conversion in the station happens here, once.** The wire units and
the contract's units disagree in four places and each disagreement is a way to
put an aircraft somewhere plausible and wrong:

    field          MAVLink wire unit        contract wants
    lat / lon      degE7 (1e-7 degrees)     degrees
    altitude       millimetres, ASL         metres
    heading        centidegrees (cdeg)      degrees true
    hor_velocity   centimetres per second   knots

Verified against `message_definitions/v1.0/common.xml` in mavlink/mavlink, not
from memory: message id 246, 38-byte payload, CRC_EXTRA 184, and the packed
field order below (which is *not* the XML declaration order — MAVLink sorts
fields by descending native size for the wire).

**Validity is honoured twice.** `flags` says which values are meaningful, and
each field additionally has an `invalid` sentinel in the XML (INT32_MAX and
friends). A receiver that has heard a Mode S response but no position sends the
vehicle with the coordinates flag clear, and the contract has nullable
`latitude`/`longitude` for exactly that. Emitting a zero there would place the
aircraft in the Gulf of Guinea; emitting a stale one is worse, because it looks
right.

The same rule reaches the fields that are not numbers. A squawk of 0000 is a
code an aircraft can actually be assigned; "the receiver did not report a
squawk" is not. Both become the integer zero if the flag is ignored, and the
console cannot tell them apart afterwards, so an unflagged squawk is None here
and null on the wire. Callsign, heading, velocity and vertical velocity are the
same argument with different units.

**Everything in the message is decoded**, not merely the fields that had a home
when this was written: emitter type, altitude datum, time since last contact,
the simulated and UAT-source flags. A datapoint the receiver paid for and the
station threw away is a datapoint nobody knows is missing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

MAVLINK_V1 = 0xFE
MAVLINK_V2 = 0xFD

MSG_ADSB_VEHICLE = 246
ADSB_VEHICLE_LEN = 38
ADSB_VEHICLE_CRC_EXTRA = 184

MSG_HEARTBEAT = 0
HEARTBEAT_LEN = 9
HEARTBEAT_CRC_EXTRA = 50

#: `ADSB_FLAGS`, from common.xml. "Set = data valid".
FLAG_VALID_COORDS = 1
FLAG_VALID_ALTITUDE = 2
FLAG_VALID_HEADING = 4
FLAG_VALID_VELOCITY = 8
FLAG_VALID_CALLSIGN = 16
FLAG_VALID_SQUAWK = 32
FLAG_SIMULATED = 64
FLAG_VERTICAL_VELOCITY_VALID = 128
FLAG_BARO_VALID = 256
FLAG_SOURCE_UAT = 32768

#: `ADSB_ALTITUDE_TYPE`. Two entries and no invalid sentinel, so anything else
#: is a receiver saying something this build does not understand, and is
#: reported as "did not say" rather than guessed at.
#:
#: MAVLink names entry 0 `PRESSURE_QNH`, which is a misnomer worth knowing
#: about: ADS-B airborne position messages carry barometric altitude referenced
#: to the standard 1013.25 hPa datum (DO-260B), not to a local QNH. The
#: contract calls it `pressure` for that reason, and `devices/altitude.py`
#: corrects from 1013.25 accordingly.
ALTITUDE_TYPE_PRESSURE = 0
ALTITUDE_TYPE_GEOMETRIC = 1
ALTITUDE_TYPES = {ALTITUDE_TYPE_PRESSURE: "pressure", ALTITUDE_TYPE_GEOMETRIC: "geometric"}

#: `ADSB_EMITTER_TYPE` values that are, by their own definition, on the
#: surface: two classes of airport ground vehicle and a fixed obstacle.
#:
#: This is the *only* on-ground evidence `ADSB_VEHICLE` carries. The message has
#: no airborne/surface status field, so every other emitter type yields `None`
#: - unknown - and never `False`. Inferring "airborne" from a non-zero altitude
#: would be an invention, and an aircraft holding on a taxiway would be the case
#: it got wrong.
SURFACE_EMITTER_TYPES = frozenset({17, 18, 19})

#: The `invalid=` sentinels the message definition carries per field.
INVALID_I32 = 0x7FFFFFFF
INVALID_U16 = 0xFFFF

#: Wire layout, little-endian, in MAVLink's packed (size-sorted) order:
#:
#:   I  ICAO_address   i lat degE7     i lon degE7    i altitude mm
#:   H  heading cdeg   H hor_velocity cm/s            h ver_velocity cm/s
#:   H  flags          H squawk        B altitude_type
#:   9s callsign       B emitter_type  B tslc                       = 38 bytes
ADSB_VEHICLE_FORMAT = "<IiiiHHhHHB9sBB"

#: The other three messages the ping RX Pro puts on the wire, once a second
#: each, from component 156 (`MAV_COMP_ID_ADSB`).
#:
#: They are here for one reason: **liveness**. The receiver sends
#: `ADSB_VEHICLE` only when there is an aircraft to report, and it does not
#: send `HEARTBEAT` at all — so on a quiet day these are the only frames on the
#: line. Without their CRC_EXTRA every one failed the checksum, which mixes
#: that byte in, and the station reported "absent, 61 false starts": a healthy
#: receiver under clear sky, indistinguishable on the setup page from a dead
#: dongle or the wrong baud rate.
#:
#: Nothing decodes them. Being framed and checksummed correctly is the entire
#: contribution, and it is enough — it proves the cable, the power and the baud.
#:
#: 66 is `REQUEST_DATA_STREAM` and its 148 is the published value, which is how
#: this method was checked. 202 and 203 are uAvionix's own and are not in
#: common.xml, so their bytes were derived from the hardware: for each captured
#: frame, the one CRC_EXTRA in 0..255 that makes the stated checksum correct.
#: Fifteen frames of each agreed on a single value with no second candidate.
#: Derived, not guessed — and a wrong byte here cannot let anything through,
#: it can only keep rejecting.
MSG_REQUEST_DATA_STREAM = 66
UAVIONIX_STATUS_MSGIDS = {202: 7, 203: 85}

CRC_EXTRA = {
    MSG_ADSB_VEHICLE: ADSB_VEHICLE_CRC_EXTRA,
    MSG_HEARTBEAT: HEARTBEAT_CRC_EXTRA,
    MSG_REQUEST_DATA_STREAM: 148,
    **UAVIONIX_STATUS_MSGIDS,
}


def x25_crc(data: bytes, crc: int = 0xFFFF) -> int:
    """The CRC-16/MCRF4XX MAVLink calls X.25."""
    for byte in data:
        tmp = (byte ^ (crc & 0xFF)) & 0xFF
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


@dataclass(frozen=True)
class Frame:
    msgid: int
    payload: bytes
    sysid: int
    compid: int
    seq: int


@dataclass(frozen=True)
class AdsbVehicle:
    """One `ADSB_VEHICLE`, in contract units, with absent values as None.

    Nothing here is scaled again downstream. If a value is None it is because
    the receiver said it did not know, and the honest thing to publish is null.
    """

    icao: str
    latitude: float | None
    longitude: float | None
    altitude_m: float | None
    heading_deg: float | None
    speed_kt: float | None
    vertical_speed_ms: float | None
    callsign: str | None
    squawk: int | None
    tslc_s: int
    simulated: bool

    #: `pressure` | `geometric` | None. Which datum `altitude_m` is in, and
    #: therefore whether it can be corrected against the station's barometer.
    altitude_type: str | None = None

    #: `ADSB_EMITTER_TYPE`, unmapped. 0 is a real value meaning "no
    #: information", which is why this is not nullable: the receiver always
    #: sends a byte, and naming it is a display decision made elsewhere.
    emitter_type: int = 0

    #: True only for the surface emitter categories; None everywhere else,
    #: because the message carries no airborne/surface bit. Never False.
    on_ground: bool | None = None

    #: `adsb` (1090ES) or `uat` (978 MHz), from `ADSB_FLAGS_SOURCE_UAT`. The
    #: flag names one band, so its absence names the other.
    source: str = "adsb"

    #: `ADSB_FLAGS_BARO_VALID`. Decoded because the receiver sends it, and it
    #: is a second, independent statement about the altitude alongside
    #: `altitude_type`. **The contract has no field for it** - see the report
    #: in CONTRACT-QUESTIONS.md - so it reaches the setup page's datastream
    #: line and nothing else today.
    baro_valid: bool = False

    #: The raw `flags` word. Kept so a decode can be argued about against a
    #: capture without re-deriving which bits were set.
    flags: int = 0


def decode_adsb_vehicle(payload: bytes) -> AdsbVehicle:
    """Decode one payload. Truncated v2 payloads are zero-padded first, which is
    what MAVLink 2's trailing-zero trimming requires of any receiver."""
    if len(payload) < ADSB_VEHICLE_LEN:
        payload = payload + bytes(ADSB_VEHICLE_LEN - len(payload))
    (
        icao, lat_e7, lon_e7, altitude_mm, heading_cdeg, hor_cms, ver_cms,
        flags, squawk, altitude_type_raw, callsign_raw, emitter_type, tslc,
    ) = struct.unpack(ADSB_VEHICLE_FORMAT, payload[:ADSB_VEHICLE_LEN])

    valid_coords = bool(flags & FLAG_VALID_COORDS)
    valid_altitude = bool(flags & FLAG_VALID_ALTITUDE)
    valid_heading = bool(flags & FLAG_VALID_HEADING)
    valid_velocity = bool(flags & FLAG_VALID_VELOCITY)
    valid_callsign = bool(flags & FLAG_VALID_CALLSIGN)
    valid_squawk = bool(flags & FLAG_VALID_SQUAWK)

    latitude = longitude = None
    if valid_coords and lat_e7 != INVALID_I32 and lon_e7 != INVALID_I32:
        latitude = lat_e7 / 1e7            # degE7 -> degrees
        longitude = lon_e7 / 1e7

    altitude_m = None
    if valid_altitude and altitude_mm != INVALID_I32:
        altitude_m = altitude_mm / 1000.0  # mm -> m

    heading_deg = None
    if valid_heading and heading_cdeg != INVALID_U16:
        heading_deg = (heading_cdeg / 100.0) % 360.0  # cdeg -> degrees

    speed_kt = None
    if valid_velocity and hor_cms != INVALID_U16:
        # cm/s -> m/s -> knots. 1 knot = 1852 m / 3600 s.
        speed_kt = (hor_cms / 100.0) * 3600.0 / 1852.0

    vertical_ms = None
    if flags & FLAG_VERTICAL_VELOCITY_VALID and ver_cms != 0x7FFF:
        vertical_ms = ver_cms / 100.0

    callsign = None
    if valid_callsign:
        text = callsign_raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
        callsign = text or None

    # The datum is only a fact about an altitude that exists. Reporting
    # "pressure" for a contact whose altitude flag is clear would offer the
    # correction machinery something to bite on that is not there.
    altitude_type = (
        ALTITUDE_TYPES.get(altitude_type_raw) if altitude_m is not None else None
    )

    return AdsbVehicle(
        icao=f"{icao & 0xFFFFFF:06X}",
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude_m,
        heading_deg=heading_deg,
        speed_kt=speed_kt,
        vertical_speed_ms=vertical_ms,
        callsign=callsign,
        squawk=squawk if valid_squawk else None,
        tslc_s=tslc,
        simulated=bool(flags & FLAG_SIMULATED),
        altitude_type=altitude_type,
        emitter_type=emitter_type,
        on_ground=True if emitter_type in SURFACE_EMITTER_TYPES else None,
        source="uat" if flags & FLAG_SOURCE_UAT else "adsb",
        baro_valid=bool(flags & FLAG_BARO_VALID),
        flags=flags,
    )


def encode_adsb_vehicle(
    icao: int,
    flags: int,
    lat_e7: int = INVALID_I32,
    lon_e7: int = INVALID_I32,
    altitude_mm: int = INVALID_I32,
    heading_cdeg: int = INVALID_U16,
    hor_cms: int = INVALID_U16,
    ver_cms: int = 0x7FFF,
    callsign: str = "",
    squawk: int = 0,
    tslc: int = 0,
    emitter_type: int = 0,
    altitude_type: int = 0,
) -> bytes:
    """The inverse, used by the simulated receiver and by the tests.

    It exists so the simulated ADS-B source produces real frames that go through
    the real parser — a simulator that bypasses the code it is standing in for
    tests nothing.
    """
    return struct.pack(
        ADSB_VEHICLE_FORMAT,
        icao & 0xFFFFFFFF, lat_e7, lon_e7, altitude_mm,
        heading_cdeg & 0xFFFF, hor_cms & 0xFFFF, ver_cms,
        flags & 0xFFFF, squawk & 0xFFFF, altitude_type,
        callsign.encode("ascii", "ignore")[:8].ljust(9, b"\x00"),
        emitter_type, tslc & 0xFF,
    )


def build_frame(msgid: int, payload: bytes, seq: int = 0, sysid: int = 1, compid: int = 1) -> bytes:
    """One MAVLink v2 frame around a payload."""
    header = struct.pack(
        "<BBBBBB", len(payload), 0, 0, seq & 0xFF, sysid, compid
    ) + struct.pack("<I", msgid)[:3]
    crc = x25_crc(header + payload)
    crc = x25_crc(bytes([CRC_EXTRA.get(msgid, 0)]), crc)
    return bytes([MAVLINK_V2]) + header + payload + struct.pack("<H", crc)


class MavlinkParser:
    """A byte-stream parser. Resynchronises rather than giving up.

    A serial link picks up noise, and a parser that cannot find the next start
    byte after a bad frame is a receiver that goes permanently quiet after one
    glitch — indistinguishable, from the console, from empty airspace.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        #: Byte positions that looked like a start byte and did not parse.
        #: Emphatically *not* a corruption count, which is what it was called
        #: before: 0xFD and 0xFE occur inside the payloads of good frames, and
        #: `_next` tries every candidate rather than trusting the first, so a
        #: healthy link generates these continuously. It outnumbering
        #: `good_frames` is normal and says nothing about the hardware.
        self.false_starts = 0
        self.good_frames = 0

    def feed(self, data: bytes) -> Iterator[Frame]:
        self._buffer.extend(data)
        while True:
            frame = self._next()
            if frame is None:
                return
            yield frame

    def _next(self) -> Frame | None:
        """Find the next good frame, trying every candidate start byte.

        Every start byte in the buffer is tried, not just the first. A noise
        byte that happens to be 0xFD with a large length field would otherwise
        make the parser wait for a frame that does not exist while a real one
        sat behind it in the buffer — a receiver that goes quiet after one
        glitch and looks, from the console, exactly like empty airspace.

        Bytes are only discarded once they are known to be useless: everything
        before the earliest *incomplete* candidate, which may yet complete when
        more of the stream arrives.
        """
        buffer = self._buffer
        index = 0
        earliest_incomplete: int | None = None
        false_start_positions: list[int] = []

        while index < len(buffer):
            if buffer[index] not in (MAVLINK_V1, MAVLINK_V2):
                index += 1
                continue
            outcome, frame, end = self._try_at(index)
            if outcome == "frame":
                self.false_starts += sum(1 for position in false_start_positions if position < index)
                del buffer[:end]
                self.good_frames += 1
                return frame
            if outcome == "incomplete":
                if earliest_incomplete is None:
                    earliest_incomplete = index
            else:
                false_start_positions.append(index)
            index += 1

        cut = earliest_incomplete if earliest_incomplete is not None else len(buffer)
        self.false_starts += sum(1 for position in false_start_positions if position < cut)
        del buffer[:cut]
        return None

    def _try_at(self, start: int) -> tuple[str, Frame | None, int]:
        buffer = self._buffer
        magic = buffer[start]
        if magic == MAVLINK_V2:
            if len(buffer) - start < 12:
                return "incomplete", None, 0
            length = buffer[start + 1]
            signature = 13 if buffer[start + 2] & 0x01 else 0
            total = 12 + length + signature
            if len(buffer) - start < total:
                return "incomplete", None, 0
            msgid = int.from_bytes(buffer[start + 7:start + 10], "little")
            payload = bytes(buffer[start + 10:start + 10 + length])
            crc = int.from_bytes(
                buffer[start + 10 + length:start + 12 + length], "little"
            )
            computed = x25_crc(bytes(buffer[start + 1:start + 10 + length]))
            sysid, compid, seq = buffer[start + 5], buffer[start + 6], buffer[start + 4]
        else:
            if len(buffer) - start < 8:
                return "incomplete", None, 0
            length = buffer[start + 1]
            total = 8 + length
            if len(buffer) - start < total:
                return "incomplete", None, 0
            msgid = buffer[start + 5]
            payload = bytes(buffer[start + 6:start + 6 + length])
            crc = int.from_bytes(buffer[start + 6 + length:start + 8 + length], "little")
            computed = x25_crc(bytes(buffer[start + 1:start + 6 + length]))
            sysid, compid, seq = buffer[start + 3], buffer[start + 4], buffer[start + 2]

        computed = x25_crc(bytes([CRC_EXTRA.get(msgid, 0)]), computed)
        if crc != computed:
            return "bad", None, 0
        return (
            "frame",
            Frame(msgid=msgid, payload=payload, sysid=sysid, compid=compid, seq=seq),
            start + total,
        )
