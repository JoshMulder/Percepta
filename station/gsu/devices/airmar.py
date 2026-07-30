"""The Airmar 110WX weather head, and the fields it cannot give us.

The instrument measures ultrasonic wind, air temperature and barometric
pressure. Relative humidity only if the optional module is fitted. It has **no
rain gauge, no visibility sensor and no sky observation**, all three of which
the Percepta console renders.

So this driver publishes what the instrument said and omits the rest. A rain
total of 0.0 mm during a downpour, because there is no rain sensor, is worse
than no rain field at all: it is a number an operator can act on and it is
wrong. `contract/schemas/telemetry.schema.json` currently *requires*
`humidity_pct` in a weather payload, which a 110WX without the RH module cannot
satisfy — that conflict is written up in CONTRACT-QUESTIONS.md rather than
resolved by inventing a value here.

Two things are derived rather than measured, and both are marked as such:

* **Gust** is the peak wind over a rolling window. The instrument reports 1 Hz
  wind; a gust is by definition a short peak, so it is the station's job. WMO
  uses a 3-second peak within a 10-minute window; at 1 Hz the best available is
  the peak sample, which is what this does and is close enough to be useful and
  honest enough to be labelled.
* **True wind direction** from a relative angle plus the mast's orientation. The
  110WX has no compass, so on a fixed installation the mast offset is a
  configured constant. Getting it wrong rotates every wind reading, which is why
  it is a parameter with a name rather than a zero.
"""

from __future__ import annotations

import logging
import time
from collections import deque

from ..sensors import Device, WeatherReading
from .nmea import AirmarDecoder
from .serialio import ByteSource, LineAssembler

log = logging.getLogger("gsu.airmar")

#: How long the peak-gust window is. Ten minutes is the meteorological
#: convention and the number the console's gust figure should mean.
GUST_WINDOW_SECONDS = 600.0

#: Longer than several missed reports at the instrument's 1 Hz output. Past
#: this the device is present-but-silent, which is a different fault from
#: absent and is reported as such.
SILENT_AFTER_SECONDS = 15.0


class AirmarWeather:
    """Reads the 110WX. Publishes only what it actually reported."""

    def __init__(
        self,
        source: ByteSource,
        humidity_module: bool = False,
        mast_offset_deg: float = 0.0,
        label: str = "Airmar 110WX",
        port: str = "",
    ) -> None:
        self.source = source
        self.humidity_module = bool(humidity_module)
        self.mast_offset_deg = float(mast_offset_deg)
        self.label = label
        self.port = port
        self._decoder = AirmarDecoder()
        self._lines = LineAssembler()
        self._gusts: deque[tuple[float, float]] = deque()
        self._last_data: float | None = None
        self._failed = False
        # The last few sentences as they arrived, for the setup page's
        # datastream field. Bounded and current — a tap, never a history.
        self._raw: deque[str] = deque(maxlen=4)

    # --- reading --------------------------------------------------------

    def pump(self) -> None:
        """Drain whatever the port has. Never blocks."""
        try:
            data = self.source.read()
        except OSError:
            self._failed = True
            return
        if not data:
            return
        for line in self._lines.feed(data):
            self._raw.append(line)
            if self._decoder.feed_line(line):
                self._last_data = time.monotonic()

    def read(self, dt: float) -> WeatherReading | None:
        """The current picture, or None if the instrument has told us nothing.

        None means "no reading", and the caller publishes nothing rather than
        publishing a frame full of absences that looks like a working sensor
        with nothing to say.
        """
        self.pump()
        state = self._decoder.state
        if self._last_data is None:
            return None

        now = time.monotonic()
        if state.wind_kt is not None:
            self._gusts.append((now, state.wind_kt))
        while self._gusts and now - self._gusts[0][0] > GUST_WINDOW_SECONDS:
            self._gusts.popleft()
        gust = max((value for _, value in self._gusts), default=None)

        direction = state.wind_dir_deg
        if direction is not None and state.wind_dir_is_relative:
            direction = (direction + self.mast_offset_deg) % 360

        return WeatherReading(
            wind_kt=state.wind_kt,
            gust_kt=gust,
            wind_dir_deg=direction,
            temperature_c=state.temperature_c,
            # Absent unless the module is fitted *and* the instrument reported
            # it. Two conditions, because a mis-set flag must not conjure a
            # humidity out of an instrument that has no sensor for it.
            humidity_pct=state.humidity_pct if self.humidity_module else None,
            pressure_hpa=state.pressure_hpa,
            # No sensor exists for any of these on this instrument.
            visibility_km=None,
            sky=None,
            is_day=None,
            rain_rate_mmh=None,
            rain_mm_today=None,
        )

    # --- state ----------------------------------------------------------

    @property
    def receiving(self) -> bool:
        return (
            self._last_data is not None
            and time.monotonic() - self._last_data < SILENT_AFTER_SECONDS
        )

    @property
    def status(self) -> str:
        if self._failed:
            return "failed"
        if self._last_data is None:
            return "silent"      # the port is open and nothing has ever arrived
        return "streaming" if self.receiving else "stalled"

    def describe(self) -> Device:
        state = self._decoder.state
        detail = f"{self.label} on {self.port or 'a serial port'}, {self.status}"
        if state.sentences:
            detail += f", {state.sentences} sentences"
        if not self.humidity_module:
            detail += ", no humidity module"
        return Device(
            id="weather", kind="weather-station",
            present=self.status in ("streaming", "stalled"),
            detail=detail, simulated=False,
        )

    def raw_sample(self) -> list[str]:
        """The instrument's own last sentences, only while it is talking.
        Stale lines rendered as live data would be a lie with a timestamp."""
        return list(self._raw) if self.receiving else []

    def close(self) -> None:
        self.source.close()
