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

    **None is never zero.** `latitude`/`longitude` are nullable because a Mode S
    response alone gives no position, and the console draws a dot rather than
    inventing one. Every other nullable field here is nullable for the same
    reason: the receiver attaches a validity flag to each of altitude, heading,
    velocity, vertical velocity, callsign and squawk, and the flag is the
    receiver telling us which of "the value is zero" and "there is no value" it
    means. Collapsing the two loses the only copy of that distinction — squawk
    0000 is a code, and 0 kt is an aircraft that has stopped.
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

    #: `pressure` | `geometric` | None, and the pressure altitude re-referenced
    #: to the station's own barometer when that is switched on and possible.
    #: The corrected value is carried *beside* `altitude`, never instead of it:
    #: what the receiver said and what it means locally are two facts, and a
    #: console that cannot show both cannot show its working.
    altitude_type: str | None = None
    altitude_corrected_m: float | None = None

    #: Metres per second, positive climbing.
    vertical_speed: float | None = None
    #: `ADSB_EMITTER_TYPE` as reported, unmapped. Naming it is the console's job.
    emitter_type: int | None = None
    #: Mode A as an integer, so 7700 is 7700.
    squawk: int | None = None
    #: `tslc`. A track still drawn at 30 seconds is a memory, not an aircraft.
    seconds_since_contact: float | None = None
    on_ground: bool | None = None
    #: The receiver flagged this contact as injected. Carried so that a test
    #: transmission can never be read as traffic.
    simulated: bool | None = None
    #: `adsb` (1090ES) or `uat` (978 MHz).
    source: str | None = None

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
            "altitude_type": self.altitude_type,
            "altitude_corrected_m": (
                None if self.altitude_corrected_m is None
                else round(self.altitude_corrected_m)
            ),
            "vertical_speed": (
                None if self.vertical_speed is None else round(self.vertical_speed, 1)
            ),
            "emitter_type": self.emitter_type,
            "squawk": self.squawk,
            "seconds_since_contact": (
                None if self.seconds_since_contact is None
                else round(self.seconds_since_contact, 1)
            ),
            "on_ground": self.on_ground,
            "simulated": self.simulated,
            "source": self.source,
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
    """What is generating, what is consuming, and which way the battery is going.

    Four possible sources — battery, solar, AC mains, a backup generator — and
    one load. Not every site has all four, and the difference between "this
    site has no grid connection" and "this site's grid is down" is the whole
    reason the optional ones are *absent* rather than zero: a station reporting
    `mains_w: 0` on a site that never had mains looks exactly like one whose
    power has failed.

    Same rule as a weather head with no humidity module. A source that is not
    fitted is omitted; a source that is fitted reports, including reporting
    zero, which is then a real measurement.
    """

    soc_pct: float
    battery_v: float
    pv_w: float
    load_w: float
    #: Hours left at the current draw; null while charging, because "infinite"
    #: is not a number the console should have to special-case.
    runtime_h: float | None

    #: Signed: positive charging, negative discharging. Measured rather than
    #: left to be derived — with four sources a consumer cannot work out the
    #: battery's direction from the others without knowing about conversion
    #: losses and which source is carrying the load, so it would be guessing.
    battery_w: float = 0.0

    #: None means no mains input at this site. `mains_present` False on a
    #: fitted input means the grid is down, which is a fault; the pair being
    #: absent means nothing is wrong.
    mains_w: float | None = None
    mains_present: bool | None = None

    #: None means no generator fitted. Running and delivering nothing is a
    #: distinct state worth reporting: it has started and failed to take the
    #: load, which is exactly what somebody needs to be told.
    generator_w: float | None = None
    generator_running: bool | None = None

    def to_payload(self) -> dict:
        payload = {
            "kind": "power",
            "soc_pct": round(min(100.0, max(0.0, self.soc_pct)), 1),
            "battery_v": round(self.battery_v, 2),
            "pv_w": round(self.pv_w, 1),
            "load_w": round(self.load_w, 1),
            "battery_w": round(self.battery_w, 1),
            "runtime_h": None if self.runtime_h is None else round(self.runtime_h, 1),
        }
        # Omitted, not nulled. Absent is "no such source at this site"; a
        # number — including zero — is a measurement from something fitted.
        if self.mains_present is not None:
            payload["mains_present"] = self.mains_present
            payload["mains_w"] = round(self.mains_w or 0.0, 1)
        if self.generator_running is not None:
            payload["generator_running"] = self.generator_running
            payload["generator_w"] = round(self.generator_w or 0.0, 1)
        return payload


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
