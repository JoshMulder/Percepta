"""Simulated hardware. There is none attached to this machine.

Every adapter here reports `simulated=True` in its device inventory, so the
platform is never told a simulation is a sensor. Swapping in a real driver means
implementing the same protocol from `__init__.py`; nothing above the interface
changes, including the alerting that runs with no link.

The behaviour is modelled on `server/app/backend/scripts/simulate_station.py`,
which is the reference for what a field should contain — deliberately, since the
console is built against it and the brief is to replace the simulator without
the console noticing.
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import Device, PowerReading, WeatherReading


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


class SimulatedWeather:
    """Slow, coherent weather. The sky follows humidity and visibility rather
    than being drawn independently, so the icon can never contradict the numbers
    beside it."""

    def __init__(self, timezone: str = "UTC", seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._tz = _zone(timezone)
        self._t = self._rng.uniform(0, 1000)
        self._wind_dir = self._rng.uniform(0, 360)
        self.rain_mm_today = round(self._rng.uniform(0, 6), 1)
        self._rain_rate = 0.0
        self._day_index = self._local_day()

    def set_timezone(self, timezone: str) -> None:
        """The station is told its timezone at enrolment, which is what the
        daily rain total resets against. Deriving one from a position would be
        a guess, and wrong by a day either side of a border."""
        self._tz = _zone(timezone)
        self._day_index = self._local_day()

    def _local_day(self) -> int:
        return datetime.now(self._tz).toordinal()

    def read(self, dt: float) -> WeatherReading:
        self._t += dt
        day = (math.sin(self._t / 240) + 1) / 2
        humidity = 62 + 14 * math.sin(self._t / 130)
        visibility = max(2.0, 30 - 12 * abs(math.sin(self._t / 200)))
        if visibility < 5:
            sky = "fog"
        elif humidity > 82:
            sky = "rain"
        elif humidity > 72:
            sky = "cloudy"
        elif humidity > 62:
            sky = "partly"
        else:
            sky = "clear"

        self._rain_rate = round(self._rng.uniform(0.4, 7.5), 1) if sky == "rain" else 0.0

        # A tipping-bucket total resets at local midnight in the station's own
        # timezone, which is why the station is told its timezone at enrolment
        # rather than deriving one from a position.
        today = self._local_day()
        if today != self._day_index:
            self._day_index = today
            self.rain_mm_today = 0.0
        self.rain_mm_today += self._rain_rate * (dt / 3600)

        reading = WeatherReading(
            wind_kt=8 + 6 * math.sin(self._t / 90) + self._rng.uniform(-1, 1),
            gust_kt=12 + 8 * math.sin(self._t / 70) + self._rng.uniform(0, 3),
            # Backs and veers slowly rather than jumping, so the compass needle
            # moves the way a real one does.
            wind_dir_deg=self._wind_dir + 18 * math.sin(self._t / 260),
            temperature_c=9 + 6 * day + self._rng.uniform(-0.4, 0.4),
            humidity_pct=humidity,
            pressure_hpa=1013 + 9 * math.sin(self._t / 400),
            visibility_km=visibility,
            sky=sky,
            is_day=day > 0.35,
            rain_rate_mmh=self._rain_rate,
            rain_mm_today=self.rain_mm_today,
        )
        # For the setup page's datastream field. A simulation has no raw
        # bytes, so its "raw" is the reading it just produced, in one line.
        self._last_line = (
            f"wind {reading.wind_kt:.1f} kt @ {reading.wind_dir_deg % 360:.0f}°  "
            f"{reading.temperature_c:.1f} °C  {reading.pressure_hpa:.1f} hPa  "
            f"rh {reading.humidity_pct:.0f}%  {reading.sky}"
        )
        return reading

    def raw_sample(self) -> list[str]:
        line = getattr(self, "_last_line", "")
        return [line] if line else []

    def describe(self) -> Device:
        return Device(
            id="weather",
            kind="weather-station",
            present=True,
            # Named for what it is: an instrument with every sensor the console
            # renders, which no instrument in this box actually has.
            detail="simulated full weather station, including a rain gauge",
            simulated=True,
        )


class SimulatedPower:
    """Solar, a battery, and a load that depends on what is switched on.

    The day cycle is fast enough to be visible in a demo and slow enough that
    the console's low/critical thresholds mean something.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._t = self._rng.uniform(0, 1000)
        self.soc = self._rng.uniform(62, 92)

    def read(self, dt: float, extra_load_w: float = 0.0) -> PowerReading:
        self._t += dt
        day = (math.sin(self._t / 240) + 1) / 2
        pv_w = day * 780
        load_w = 120 + extra_load_w + self._rng.uniform(-8, 8)
        net = pv_w - load_w
        self.soc = max(2.0, min(100.0, self.soc + net * dt / 40000))
        self._last_line = (
            f"soc {self.soc:.0f}%  {48.0 + (self.soc - 50) * 0.042:.1f} V  "
            f"pv {pv_w:.0f} W  load {load_w:.0f} W"
        )
        return PowerReading(
            soc_pct=self.soc,
            battery_v=48.0 + (self.soc - 50) * 0.042,
            pv_w=pv_w,
            load_w=load_w,
            # Null while charging: an hours-remaining figure that means
            # "indefinitely" is worse than no figure.
            runtime_h=None if net > 0 else self.soc * 0.42,
        )

    def raw_sample(self) -> list[str]:
        line = getattr(self, "_last_line", "")
        return [line] if line else []

    def describe(self) -> Device:
        return Device(
            id="power",
            kind="charge-controller",
            present=True,
            detail="simulated MPPT controller and 48 V bank",
            simulated=True,
        )


class SimulatedFloodlight:
    """A relay with a contactor that takes a moment.

    The delay is not decoration: it is the difference between reporting what was
    commanded and reporting what the hardware is doing, and the console is built
    on the second (`contract/schemas/telemetry.schema.json`, light.on).
    """

    ACTUATION_SECONDS = 0.4
    LOAD_W = 60.0

    def __init__(self) -> None:
        self._on = False
        self._requested = False
        self._pending = 0.0

    def request(self, on: bool) -> None:
        if bool(on) != self._requested:
            self._requested = bool(on)
            self._pending = self.ACTUATION_SECONDS

    def step(self, dt: float) -> None:
        if self._pending > 0:
            self._pending -= dt
            if self._pending <= 0:
                self._on = self._requested

    @property
    def on(self) -> bool:
        return self._on

    @property
    def load_w(self) -> float:
        return self.LOAD_W if self._on else 0.0

    def raw_sample(self) -> list[str]:
        return [
            f"relay {'on' if self._on else 'off'}"
            + (", actuating" if self._pending > 0 else "")
            + (f", {self.load_w:.0f} W" if self._on else "")
        ]

    def describe(self) -> Device:
        return Device(
            id="light",
            kind="floodlight",
            present=True,
            detail="simulated relay, state read back",
            simulated=True,
        )
