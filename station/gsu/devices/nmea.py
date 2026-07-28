"""NMEA 0183, and the Airmar sentences the weather head actually sends.

The 110WX is an ultrasonic instrument: wind, air temperature and barometric
pressure, plus relative humidity **only if the RH module is fitted** — Airmar
sell the unit both ways. It has no rain gauge, no visibility sensor and no
pyranometer, which is a fact with consequences for the telemetry contract; see
`registry.py` and CONTRACT-QUESTIONS.md.

Sentences decoded, per Airmar's WeatherStation technical manual:

    MWV   wind speed and angle, relative (R) or theoretical/true (T)
    MDA   the meteorological composite: pressure, air temperature, humidity,
          dew point, and wind in both knots and m/s
    XDR   transducer measurements, in (type, value, unit, name) quadruples

Checksums are validated and bad sentences dropped. A serial line picks up noise
and half a sentence read as good data is a wind speed that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Sentence:
    talker: str
    kind: str
    fields: list[str]


def checksum(body: str) -> int:
    value = 0
    for char in body:
        value ^= ord(char)
    return value


def parse_sentence(line: str) -> Sentence | None:
    """One `$..` line to a sentence, or None if it is not one we can trust."""
    line = line.strip()
    if not line.startswith(("$", "!")):
        return None
    if "*" in line:
        body, _, given = line[1:].partition("*")
        try:
            if int(given[:2], 16) != checksum(body):
                return None
        except ValueError:
            return None
    else:
        # Airmar sends checksums; a sentence without one is either a different
        # device or a corrupted line, and neither is worth guessing at.
        return None
    parts = body.split(",")
    if not parts or len(parts[0]) < 5:
        return None
    return Sentence(talker=parts[0][:2], kind=parts[0][2:5], fields=parts[1:])


def _number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


KNOTS_PER_MS = 3600.0 / 1852.0


@dataclass
class AirmarState:
    """Latest value for each parameter the unit provides, with nothing invented.

    Every field starts None and stays None until the instrument has actually
    reported it. That is what lets the driver publish "absent" rather than a
    plausible zero.
    """

    wind_kt: float | None = None
    wind_dir_deg: float | None = None       # true, degrees the wind comes FROM
    wind_dir_is_relative: bool = False
    temperature_c: float | None = None
    pressure_hpa: float | None = None
    humidity_pct: float | None = None
    sentences: int = 0
    unknown: set[str] = field(default_factory=set)


class AirmarDecoder:
    """Accumulates state from the sentence stream.

    Stateful because NMEA is: wind arrives in MWV, pressure in MDA or XDR, and a
    consumer wants the current picture rather than whichever sentence came last.
    """

    def __init__(self) -> None:
        self.state = AirmarState()

    def feed_line(self, line: str) -> bool:
        sentence = parse_sentence(line)
        if sentence is None:
            return False
        handler = getattr(self, f"_on_{sentence.kind.lower()}", None)
        if handler is None:
            self.state.unknown.add(sentence.kind)
            return False
        handler(sentence.fields)
        self.state.sentences += 1
        return True

    # --- sentences ------------------------------------------------------

    def _on_mwv(self, fields: list[str]) -> None:
        # $WIMWV,angle,reference(R|T),speed,units(K|M|N),status(A|V)
        if len(fields) < 5 or fields[4].upper() != "A":
            return  # status V: the instrument is telling us the data is void
        angle = _number(fields[0])
        speed = _number(fields[2])
        units = (fields[3] or "N").upper()
        if speed is not None:
            if units == "K":       # km/h
                speed = speed / 1.852
            elif units == "M":     # m/s
                speed = speed * KNOTS_PER_MS
            self.state.wind_kt = speed
        if angle is not None:
            reference = (fields[1] or "R").upper()
            # Relative (apparent) wind is referenced to the instrument's own
            # heading. On a fixed mast with a known orientation that is the same
            # as true; on anything that moves it is not, which is why the
            # reference is kept rather than assumed.
            self.state.wind_dir_deg = angle % 360
            self.state.wind_dir_is_relative = reference == "R"

    def _on_mda(self, fields: list[str]) -> None:
        # Pressure (inHg, bars), air temp, water temp, humidity, dew point,
        # wind direction true/magnetic, wind speed knots/m-s.
        if len(fields) >= 4:
            bars = _number(fields[2])
            inches = _number(fields[0])
            if bars is not None:
                self.state.pressure_hpa = bars * 1000.0
            elif inches is not None:
                self.state.pressure_hpa = inches * 33.8639
        if len(fields) >= 5:
            temperature = _number(fields[4])
            if temperature is not None:
                self.state.temperature_c = temperature
        if len(fields) >= 9:
            humidity = _number(fields[8])
            if humidity is not None:
                # Only ever present when the RH module is fitted.
                self.state.humidity_pct = humidity
        if len(fields) >= 14:
            direction = _number(fields[12])
            if direction is not None:
                self.state.wind_dir_deg = direction % 360
                self.state.wind_dir_is_relative = False
        if len(fields) >= 18:
            speed = _number(fields[16])
            if speed is not None:
                self.state.wind_kt = speed

    def _on_xdr(self, fields: list[str]) -> None:
        # Quadruples of (type, value, unit, name).
        for index in range(0, len(fields) - 3, 4):
            kind, raw, unit, name = fields[index:index + 4]
            value = _number(raw)
            if value is None:
                continue
            kind = kind.upper()
            name = (name or "").upper()
            if kind == "C" and unit.upper() == "C" and "AIR" in name:
                self.state.temperature_c = value
            elif kind == "P" and unit.upper() == "P":
                self.state.pressure_hpa = value / 100.0   # pascals -> hPa
            elif kind == "H" and unit.upper() == "P":
                self.state.humidity_pct = value
