"""What is bolted to the box, behind interfaces.

There is no hardware on this machine, so everything in `simulated.py` is
simulated and says so — in its own `describe()`, which is what the station
reports as its device inventory. `contract/enrolment.md` §7 is explicit that the
station owns the truth about what is attached, and "a camera that has failed and
a camera that was never fitted look identical in a database and completely
different at the site". A simulated sensor claiming to be a real one would be
the same lie with worse consequences.

Each adapter converts hardware into the payload the contract asks for and
nothing more. Where a real driver goes, and what it must do:

    ADS-B      a dump1090/readsb feed on its own dongle — 1090 MHz cannot share
               a tuner with airband (server/docs/05-radio-integration.md §4)
    weather    Modbus or serial, per the device inventory in configuration
    power      the solar charge controller's Modbus registers
    light      a relay on a GPIO, read back rather than assumed
    radio      see ../radio/
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Device:
    """One line of the station's own inventory."""

    id: str
    kind: str
    present: bool
    detail: str = ""
    simulated: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "present": self.present,
            "detail": self.detail,
            "simulated": self.simulated,
        }


@runtime_checkable
class Sensor(Protocol):
    def describe(self) -> Device: ...


@dataclass(frozen=True)
class Aircraft:
    """One contact, in the shape the contract wants it.

    `latitude`/`longitude` are nullable because a Mode S response alone gives no
    position, and the console draws a dot rather than inventing one.
    """

    icao: str
    range_km: float
    bearing: float
    callsign: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    track: float | None = None
    speed: float | None = None
    alert: bool = False

    def to_payload(self) -> dict:
        return {
            "icao": self.icao,
            "callsign": self.callsign,
            "latitude": None if self.latitude is None else round(self.latitude, 5),
            "longitude": None if self.longitude is None else round(self.longitude, 5),
            "altitude": None if self.altitude is None else round(self.altitude),
            "track": None if self.track is None else round(self.track % 360, 1),
            "speed": None if self.speed is None else round(self.speed),
            "range_km": round(self.range_km, 2),
            "bearing": round(self.bearing % 360, 1),
            "alert": self.alert,
        }


@runtime_checkable
class AdsbReceiver(Protocol):
    def poll(self, dt: float) -> list[Aircraft]:
        """The complete current picture, not a delta. An empty list means no
        contacts, which is different from a failed receiver — a failed receiver
        stops the telemetry entirely, and the platform notices that."""

    def describe(self) -> Device: ...


@dataclass(frozen=True)
class WeatherReading:
    """A reading in which **None means "no sensor said so"**.

    Every field is optional and every None is omitted from the payload rather
    than defaulted. The alternative — zero for rainfall on an instrument with no
    rain gauge, or a plausible humidity on one with no RH module — is a number
    an operator can act on and cannot tell is invented. Which fields a given
    device can source is declared in `devices/registry.py`.
    """

    wind_kt: float | None = None
    gust_kt: float | None = None
    #: The direction the wind comes FROM. Meteorological convention, the
    #: opposite of a movement vector, and the classic wind-rose bug.
    wind_dir_deg: float | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None
    visibility_km: float | None = None
    sky: str | None = None
    is_day: bool | None = None
    rain_rate_mmh: float | None = None
    rain_mm_today: float | None = None

    def to_payload(self) -> dict:
        gust = self.gust_kt
        if gust is not None and self.wind_kt is not None:
            gust = max(gust, self.wind_kt)
        values = {
            "wind_kt": None if self.wind_kt is None else round(self.wind_kt, 1),
            "gust_kt": None if gust is None else round(gust, 1),
            "wind_dir_deg": None if self.wind_dir_deg is None else round(self.wind_dir_deg % 360, 1),
            "temperature_c": None if self.temperature_c is None else round(self.temperature_c, 1),
            "humidity_pct": None if self.humidity_pct is None
            else round(min(100.0, max(0.0, self.humidity_pct)), 0),
            "pressure_hpa": None if self.pressure_hpa is None else round(self.pressure_hpa, 1),
            "visibility_km": None if self.visibility_km is None else round(self.visibility_km, 1),
            "sky": self.sky,
            "is_day": self.is_day,
            "rain_rate_mmh": None if self.rain_rate_mmh is None
            else round(max(0.0, self.rain_rate_mmh), 1),
            "rain_mm_today": None if self.rain_mm_today is None
            else round(max(0.0, self.rain_mm_today), 1),
        }
        payload = {"kind": "weather"}
        # Absent, not zero, and absent rather than null: the schema's optional
        # fields are nullable but its required ones are not, so an unmeasured
        # value is left out entirely and reported as unsourced in health
        # telemetry instead.
        payload.update({key: value for key, value in values.items() if value is not None})
        return payload

    def missing(self) -> list[str]:
        payload = self.to_payload()
        return [
            name for name in (
                "wind_kt", "gust_kt", "wind_dir_deg", "temperature_c", "humidity_pct",
                "pressure_hpa", "visibility_km", "sky", "is_day", "rain_rate_mmh",
                "rain_mm_today",
            )
            if name not in payload
        ]


@runtime_checkable
class WeatherStation(Protocol):
    def read(self, dt: float) -> WeatherReading: ...
    def describe(self) -> Device: ...


@dataclass(frozen=True)
class PowerReading:
    soc_pct: float
    battery_v: float
    pv_w: float
    load_w: float
    #: Hours left at the current draw; null while charging, because "infinite"
    #: is not a number the console should have to special-case.
    runtime_h: float | None

    def to_payload(self) -> dict:
        return {
            "kind": "power",
            "soc_pct": round(min(100.0, max(0.0, self.soc_pct)), 1),
            "battery_v": round(self.battery_v, 2),
            "pv_w": round(self.pv_w, 1),
            "load_w": round(self.load_w, 1),
            "runtime_h": None if self.runtime_h is None else round(self.runtime_h, 1),
        }


@runtime_checkable
class PowerSystem(Protocol):
    def read(self, dt: float, extra_load_w: float = 0.0) -> PowerReading: ...
    def describe(self) -> Device: ...


@runtime_checkable
class Floodlight(Protocol):
    """The one actuator in the box.

    `on` reports what the hardware is doing, not what was last commanded — the
    console renders this and never assumes a command succeeded. On real hardware
    that means reading the relay back, not remembering the write.
    """

    def request(self, on: bool) -> None: ...
    def step(self, dt: float) -> None: ...
    @property
    def on(self) -> bool: ...
    def describe(self) -> Device: ...


def bearing_to(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Range in km and true bearing from the station to a point.

    Equirectangular, which is accurate to a fraction of a percent over the ~100
    km an ADS-B receiver sees and much cheaper than the alternatives. The
    cos(latitude) term is not optional at the ~45°S these sites sit at: without
    it every contact lands noticeably east or west of where it is.
    """
    dx = math.radians(lon - lon0) * math.cos(math.radians((lat + lat0) / 2)) * 6371.0
    dy = math.radians(lat - lat0) * 6371.0
    return math.hypot(dx, dy), (math.degrees(math.atan2(dx, dy)) + 360) % 360
