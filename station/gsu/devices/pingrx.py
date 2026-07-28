"""The uAvionix ping RX Pro, and a simulation that goes through the same code.

The receiver emits `ADSB_VEHICLE` over a serial link. `mavlink.py` does the
framing, the decode and every unit conversion; this holds the contact table,
works out range and bearing from the station's own position, and decides what
the station is entitled to say about the airspace.

**Three states, and telling them apart is the point.**

    absent          nothing has ever arrived on the port. The device is
                    configured and not talking. Publishes *nothing*.
    stalled         it was talking and has stopped. Also publishes nothing.
    present, quiet  frames are arriving and there are no contacts. Publishes an
                    empty list, which honestly means "clear airspace".

An empty `aircraft` array and no telemetry at all mean completely different
things — one is clear sky, one is a dead receiver — and the console can only
distinguish them if the station is disciplined about which it sends. Sending an
empty list from a receiver that has never said anything is the exact failure
this project keeps designing against.

**A contact with no position is not published.** The schema allows null
`latitude`/`longitude` for a Mode S response with no position, but *requires*
`range_km` and `bearing`, which can only be computed from a position. Rather
than invent a range, such contacts are counted and reported in health telemetry;
the contradiction is written up in CONTRACT-QUESTIONS.md.
"""

from __future__ import annotations

import logging
import math
import random
import time

from ..sensors import Aircraft, Device, bearing_to
from . import mavlink
from .serialio import ByteSource

log = logging.getLogger("gsu.adsb")

#: How long a contact survives without an update. ADS-B position reports are
#: roughly 1 Hz; a minute of silence means it has left the area or the receiver
#: has stopped hearing it, and either way it should leave the map.
CONTACT_TTL_SECONDS = 60.0

#: Past this with no valid frame at all, the receiver is treated as stalled.
#: The ping RX Pro emits a heartbeat, so silence is a real symptom.
SILENT_AFTER_SECONDS = 10.0


class PingRxAdsb:
    def __init__(
        self,
        source: ByteSource,
        latitude: float = 0.0,
        longitude: float = 0.0,
        alert_range_km: float = 12.0,
        alert_altitude_m: float = 1500.0,
        port: str = "",
        label: str = "uAvionix ping RX Pro",
    ) -> None:
        self.source = source
        self.lat = latitude
        self.lon = longitude
        self.alert_range_km = alert_range_km
        self.alert_altitude_m = alert_altitude_m
        self.port = port
        self.label = label
        self._parser = mavlink.MavlinkParser()
        self._vehicles: dict[str, tuple[mavlink.AdsbVehicle, float]] = {}
        self._last_frame: float | None = None
        self._failed = False
        self.positionless = 0

    def set_site(self, latitude: float, longitude: float) -> None:
        self.lat = latitude
        self.lon = longitude

    def set_thresholds(self, range_km: float, altitude_m: float) -> None:
        self.alert_range_km = range_km
        self.alert_altitude_m = altitude_m

    # --- reading --------------------------------------------------------

    def pump(self) -> None:
        try:
            data = self.source.read()
        except OSError:
            self._failed = True
            return
        if not data:
            return
        now = time.monotonic()
        for frame in self._parser.feed(data):
            self._last_frame = now
            if frame.msgid != mavlink.MSG_ADSB_VEHICLE:
                continue
            vehicle = mavlink.decode_adsb_vehicle(frame.payload)
            self._vehicles[vehicle.icao] = (vehicle, now)

    def poll(self, dt: float) -> list[Aircraft] | None:
        """Contacts, or None when the receiver is not talking to us."""
        self.pump()
        if self.status not in ("streaming",):
            return None

        now = time.monotonic()
        for icao, (_, seen) in list(self._vehicles.items()):
            if now - seen > CONTACT_TTL_SECONDS:
                del self._vehicles[icao]

        contacts: list[Aircraft] = []
        positionless = 0
        for vehicle, _ in self._vehicles.values():
            if vehicle.latitude is None or vehicle.longitude is None:
                # No position means no range and no bearing, both of which the
                # contract requires. Counted, not invented.
                positionless += 1
                continue
            range_km, bearing = bearing_to(
                self.lat, self.lon, vehicle.latitude, vehicle.longitude
            )
            contacts.append(
                Aircraft(
                    icao=vehicle.icao,
                    callsign=vehicle.callsign,
                    latitude=vehicle.latitude,
                    longitude=vehicle.longitude,
                    altitude=vehicle.altitude_m,
                    track=vehicle.heading_deg,
                    speed=vehicle.speed_kt,
                    range_km=range_km,
                    bearing=bearing,
                    alert=(
                        range_km < self.alert_range_km
                        and vehicle.altitude_m is not None
                        and vehicle.altitude_m < self.alert_altitude_m
                    ),
                )
            )
        self.positionless = positionless
        return contacts

    # --- state ----------------------------------------------------------

    @property
    def status(self) -> str:
        if self._failed:
            return "failed"
        if self._last_frame is None:
            return "absent"
        if time.monotonic() - self._last_frame > SILENT_AFTER_SECONDS:
            return "stalled"
        return "streaming"

    def describe(self) -> Device:
        where = f" on {self.port}" if self.port else ""
        detail = f"{self.label}{where}, {self.status}"
        if self._parser.good_frames:
            detail += f", {self._parser.good_frames} frames"
        if self._parser.bad_frames:
            detail += f", {self._parser.bad_frames} bad"
        if self.positionless:
            detail += f", {self.positionless} contact(s) with no position"
        return Device(
            id="adsb", kind="adsb-receiver",
            present=self.status == "streaming",
            detail=detail, simulated=False,
        )

    def close(self) -> None:
        self.source.close()


# ---------------------------------------------------------------------------


class _SimulatedPingSource:
    """A byte source that emits real MAVLink frames.

    Deliberately not a shortcut past the parser: the simulated receiver encodes
    `ADSB_VEHICLE` exactly as the hardware does and the driver decodes it with
    the same code, so the units, the flags and the framing are all exercised
    every time the station runs. A simulation that bypassed them would test
    nothing and hide precisely the unit-scaling mistakes that put an aircraft at
    a plausible wrong altitude.
    """

    RANGE_KM = 80.0

    def __init__(self, latitude: float, longitude: float, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self.lat = latitude
        self.lon = longitude
        self._contacts = [self._new_contact() for _ in range(self._rng.randint(2, 5))]
        self._seq = 0
        self._last = time.monotonic()

    def set_site(self, latitude: float, longitude: float) -> None:
        self.lat, self.lon = latitude, longitude

    def _new_contact(self) -> dict:
        angle = self._rng.uniform(0, 2 * math.pi)
        x = math.sin(angle) * self.RANGE_KM
        y = math.cos(angle) * self.RANGE_KM
        heading = math.atan2(-x, -y) + self._rng.uniform(-0.7, 0.7)
        speed_kmh = self._rng.uniform(320, 850)
        return {
            "icao": self._rng.randint(0, 0xFFFFFF),
            "callsign": self._rng.choice(
                ["ANZ", "JST", "QFA", "VAN", "RSCU", "KIWI", "NZAF"]
            ) + str(self._rng.randint(100, 999)),
            "x": x, "y": y,
            "vx": math.sin(heading) * speed_kmh / 3600,
            "vy": math.cos(heading) * speed_kmh / 3600,
            "altitude": self._rng.uniform(600, 11000),
            "speed_ms": speed_kmh / 3.6,
            # Some contacts are heard without a position, which is what the
            # validity flags exist for.
            "positioned": self._rng.random() > 0.1,
        }

    def read(self) -> bytes:
        # One read is one report cycle, rather than being gated on wall-clock
        # time: the driver pumps once per tick, and tying the simulation to the
        # clock would make it behave differently under test than in the field.
        now = time.monotonic()
        dt = min(5.0, max(0.0, now - self._last))
        self._last = now

        out = bytearray()
        # A heartbeat, so "present and quiet" is distinguishable from "absent"
        # even when there is nothing in the sky.
        out += mavlink.build_frame(mavlink.MSG_HEARTBEAT, bytes(9), self._next_seq())

        for contact in self._contacts:
            contact["x"] += contact["vx"] * dt
            contact["y"] += contact["vy"] * dt
        self._contacts = [
            c for c in self._contacts if math.hypot(c["x"], c["y"]) <= self.RANGE_KM * 1.1
        ]
        while len(self._contacts) < 2 or (
            self._rng.random() < 0.004 and len(self._contacts) < 8
        ):
            self._contacts.append(self._new_contact())

        for contact in self._contacts:
            flags = (
                mavlink.FLAG_VALID_ALTITUDE
                | mavlink.FLAG_VALID_HEADING
                | mavlink.FLAG_VALID_VELOCITY
                | mavlink.FLAG_VALID_CALLSIGN
                | mavlink.FLAG_SIMULATED
            )
            lat_e7 = lon_e7 = mavlink.INVALID_I32
            if contact["positioned"]:
                flags |= mavlink.FLAG_VALID_COORDS
                lat = self.lat + contact["y"] / 111.0
                lon = self.lon + contact["x"] / (
                    111.0 * max(0.05, math.cos(math.radians(self.lat)))
                )
                lat_e7 = int(lat * 1e7)
                lon_e7 = int(lon * 1e7)
            track = (math.degrees(math.atan2(contact["vx"], contact["vy"])) + 360) % 360
            payload = mavlink.encode_adsb_vehicle(
                icao=contact["icao"],
                flags=flags,
                lat_e7=lat_e7,
                lon_e7=lon_e7,
                altitude_mm=int(contact["altitude"] * 1000),
                heading_cdeg=int(track * 100),
                hor_cms=int(contact["speed_ms"] * 100),
                callsign=contact["callsign"],
                tslc=0,
            )
            out += mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload, self._next_seq())
        return bytes(out)

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def close(self) -> None:
        pass


class SimulatedPingRx(PingRxAdsb):
    """The driver, fed by the simulated source. Says it is simulated."""

    def __init__(
        self,
        latitude: float = -43.5,
        longitude: float = 172.6,
        alert_range_km: float = 12.0,
        alert_altitude_m: float = 1500.0,
    ) -> None:
        source = _SimulatedPingSource(latitude, longitude)
        super().__init__(
            source, latitude=latitude, longitude=longitude,
            alert_range_km=alert_range_km, alert_altitude_m=alert_altitude_m,
            label="simulated ADS-B receiver (MAVLink)",
        )
        self._source = source

    def set_site(self, latitude: float, longitude: float) -> None:
        super().set_site(latitude, longitude)
        self._source.set_site(latitude, longitude)

    def describe(self) -> Device:
        device = super().describe()
        return Device(
            id=device.id, kind=device.kind, present=device.present,
            detail=device.detail, simulated=True,
        )
