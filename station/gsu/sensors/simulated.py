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
    """Solar, a battery, AC mains, a backup generator, and a load.

    All four sources, because the point of a demo box is to exercise the
    console's whole power display without anybody wiring a generator to a
    bench. The day cycle is fast enough to be visible and slow enough that the
    low and critical thresholds mean something.

    **The interesting behaviour is the handover.** Mains occasionally drops —
    that is what a remote site's grid does — and when it does the battery
    starts carrying the load. If the state of charge then falls far enough the
    generator starts, takes over, and charges the battery back up before
    stopping. That sequence is the one an operator most needs to be able to
    read at a glance, and it is the one a static demo never shows.

    Priority when several sources are live: solar first because it is free,
    then mains, then the generator. The battery makes up any shortfall and
    absorbs any surplus.
    """

    #: Mains fails roughly this often, in simulated seconds, and stays down for
    #: a while. Frequent enough to see during a demo, rare enough that the
    #: display is not permanently in a fault state.
    MAINS_MTBF_S = 900.0
    MAINS_OUTAGE_S = 240.0

    #: The generator starts below this and stops once the battery is back above
    #: the second. The gap is deliberate: a single threshold makes a generator
    #: hunt on and off around it, which is hard on the machine and looks like a
    #: fault on a graph.
    GEN_START_SOC = 45.0
    GEN_STOP_SOC = 70.0
    GEN_OUTPUT_W = 1400.0

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._t = self._rng.uniform(0, 1000)
        self.soc = self._rng.uniform(62, 92)
        self._mains_down_for = 0.0
        self._gen_running = False

    def read(self, dt: float, extra_load_w: float = 0.0) -> PowerReading:
        self._t += dt
        day = (math.sin(self._t / 240) + 1) / 2
        pv_w = day * 780
        load_w = 120 + extra_load_w + self._rng.uniform(-8, 8)

        # The grid, and its habit of going away.
        if self._mains_down_for > 0:
            self._mains_down_for = max(0.0, self._mains_down_for - dt)
        elif self._rng.random() < dt / self.MAINS_MTBF_S:
            self._mains_down_for = self.MAINS_OUTAGE_S
        mains_present = self._mains_down_for <= 0

        # The generator, with hysteresis so it does not hunt.
        if self._gen_running and self.soc >= self.GEN_STOP_SOC:
            self._gen_running = False
        elif not self._gen_running and self.soc <= self.GEN_START_SOC:
            self._gen_running = True
        gen_w = self.GEN_OUTPUT_W if self._gen_running else 0.0

        # Solar first because it is free, then mains, then the generator; the
        # battery takes whatever is left over in either direction.
        remaining = max(0.0, load_w - pv_w)
        mains_w = min(remaining, 2000.0) if mains_present else 0.0
        remaining = max(0.0, remaining - mains_w)
        gen_delivered = min(remaining, gen_w)

        supplied = pv_w + mains_w + gen_w
        battery_w = supplied - load_w
        self.soc = max(2.0, min(100.0, self.soc + battery_w * dt / 40000))

        self._last_line = (
            f"soc {self.soc:.0f}%  {48.0 + (self.soc - 50) * 0.042:.1f} V  "
            f"pv {pv_w:.0f} W  mains {mains_w:.0f} W"
            f"{'' if mains_present else ' (down)'}  "
            f"gen {gen_w:.0f} W{' running' if self._gen_running else ''}  "
            f"load {load_w:.0f} W  batt {battery_w:+.0f} W"
        )
        return PowerReading(
            soc_pct=self.soc,
            battery_v=48.0 + (self.soc - 50) * 0.042,
            pv_w=pv_w,
            load_w=load_w,
            battery_w=battery_w,
            # Null while charging: an hours-remaining figure that means
            # "indefinitely" is worse than no figure.
            runtime_h=None if battery_w > 0 else self.soc * 0.42,
            mains_w=mains_w,
            mains_present=mains_present,
            generator_w=gen_delivered,
            generator_running=self._gen_running,
        )

    def raw_sample(self) -> list[str]:
        line = getattr(self, "_last_line", "")
        return [line] if line else []

    def describe(self) -> Device:
        return Device(
            id="power",
            kind="charge-controller",
            present=True,
            detail="simulated MPPT, 48 V bank, AC mains and backup generator",
            simulated=True,
        )


class SimulatedFloodlight:
    """A relay with a contactor that takes a moment, and an optional current
    sensor on the lamp circuit.

    The delay is not decoration: it is the difference between reporting what was
    commanded and reporting what the hardware is doing, and the console is built
    on the second (`contract/schemas/telemetry.schema.json`, light.on).

    The sensor measures the *circuit*, not the relay — which is the entire
    point of fitting one. A dead lamp draws nothing behind a closed contact;
    a welded contact keeps drawing after the command went off. `lamp_failed`
    and `relay_welded` exist so tests and demos can produce exactly those two
    faults, the same way the rest of this module fakes weather nobody is
    having; no real driver in this build can sense them yet (the gpio-relay
    registry row says so).
    """

    ACTUATION_SECONDS = 0.4
    LOAD_W = 60.0
    #: The simulated site runs a 48 V bank (`SimulatedPower`), so the lamp's
    #: nominal draw is LOAD_W / BUS_V = 1.25 A.
    BUS_V = 48.0

    def __init__(self, sense_source: str = "simulated",
                 sense_threshold_a: float = 0.2,
                 state_source: str = "relay") -> None:
        self._on = False
        self._requested = False
        self._pending = 0.0
        self.sense_source = str(sense_source or "none")
        self.sense_threshold_a = float(sense_threshold_a or 0.0)
        # Anything that is not exactly "current" reports the relay: a typo in
        # a config file must fall back to today's behaviour, not to a mode
        # that needs a sensor the typo may not have configured.
        self.state_source = "current" if str(state_source) == "current" else "relay"
        #: Injectable faults, for tests and demos.
        self.lamp_failed = False
        self.relay_welded = False

    def request(self, on: bool) -> None:
        if bool(on) != self._requested:
            self._requested = bool(on)
            self._pending = self.ACTUATION_SECONDS

    def step(self, dt: float) -> None:
        if self._pending > 0:
            self._pending -= dt
            if self._pending <= 0:
                self._on = self._requested
        if self.relay_welded:
            # A welded contact does not open, whatever was commanded.
            self._on = True

    @property
    def commanded(self) -> bool:
        """What was asked for — the intent half of the fault check."""
        return self._requested

    @property
    def _drawing(self) -> bool:
        return self._on and not self.lamp_failed

    @property
    def measured_a(self) -> float | None:
        """Amps through the lamp circuit, or None when no sensor is fitted.

        None and 0.0 are different statements — "nothing is measuring" versus
        "measured, and nothing flows" — and the fault checks only run on the
        second.
        """
        if self.sense_source in ("", "none"):
            return None
        return round(self.LOAD_W / self.BUS_V, 2) if self._drawing else 0.0

    @property
    def on(self) -> bool:
        """The reported state: the relay's contact, or the measured current
        when this light is configured to trust the stronger witness."""
        measured = self.measured_a
        if self.state_source == "current" and measured is not None:
            return measured >= self.sense_threshold_a
        return self._on

    @property
    def load_w(self) -> float:
        return self.LOAD_W if self._drawing else 0.0

    def raw_sample(self) -> list[str]:
        line = (
            f"relay {'on' if self._on else 'off'}"
            + (", actuating" if self._pending > 0 else "")
            + (f", {self.load_w:.0f} W" if self._drawing else "")
        )
        measured = self.measured_a
        if measured is not None:
            line += f", {measured:.2f} A measured"
        return [line]

    def describe(self) -> Device:
        measured = self.measured_a
        detail = "simulated relay, state read back"
        if measured is not None:
            detail = f"simulated relay, {measured:.2f} A measured"
        return Device(
            id="light",
            kind="floodlight",
            present=True,
            detail=detail,
            simulated=True,
        )
