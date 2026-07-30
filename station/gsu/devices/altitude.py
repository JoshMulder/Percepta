"""Re-referencing a transponder's pressure altitude to the station's barometer.

**What the problem is.** Almost every transponder reports *pressure altitude*:
the altitude an ISA atmosphere would put you at if sea level were 1013.25 hPa.
It usually is not. On a 990 hPa day an aircraft reporting 1000 m is nearer 810 m
above the sea, and on a 1030 hPa day it is nearer 1140 m. That is not a rounding
error — it is the difference between an aircraft being above or below the ridge
line behind a site, which is exactly the judgement an operator makes from this
number.

**Why it is optional and off by default.** This applies one sensor's reading to
another sensor's data. The Airmar's barometer being right, the station's
elevation being right, and the transponder actually sending pressure altitude
are three separate assumptions, and an operator should be the one to accept
them. So `SiteConfig.adsb_baro_correction` gates it, defaults off, and the
corrected value always travels *alongside* `altitude` rather than replacing it.

**The formulas, and their source.** ICAO Standard Atmosphere / US Standard
Atmosphere 1976, troposphere layer (0-11 km), the barometric formula with a
non-zero lapse rate:

    p(h) = p0 * (1 - h / H) ** n         H = T0/L = 44330.77 m
    h(p) = H * (1 - (p/p0) ** (1/n))     n = g0*M / (R*L) = 5.255876

with T0 = 288.15 K, L = 0.0065 K/m, g0 = 9.80665 m/s2, M = 0.0289644 kg/mol,
R = 8.31432 J/(mol K), p0 = 1013.25 hPa. The rule of thumb "27 ft per hPa near
sea level" falls out of this: 1 hPa at 1013 hPa is 8.4 m, which is 27.6 ft.

**Which pressure, and why it matters more than anything else here.** The Airmar
reports *station pressure* - the actual pressure at the sensor, at the station's
own elevation. What the correction needs is *sea-level* pressure, because a
pressure altitude is measured from the 1013.25 hPa surface and the question is
where that surface sits relative to mean sea level. So station pressure is first
reduced to sea level using the configured station elevation:

    p_sea = p_station / (1 - elevation / H) ** n

Using the station reading directly, as if it were already sea-level pressure,
would push every aircraft down by very nearly the station's elevation - a 300 m
site would produce a 300 m error, in the wrong direction, on data that looked
more trustworthy than the uncorrected number. That is the mistake this module is
mostly written to avoid, and it is why the correction refuses to run at all when
the elevation is unset rather than assuming zero.

The two steps are then self-consistent by construction: a transponder sitting on
top of the station's own barometer would come out at exactly the station's
configured elevation. That is the one-line statement of what this does.
"""

from __future__ import annotations

import time

#: ISA troposphere constants. See the module docstring for their provenance.
ISA_SEA_LEVEL_HPA = 1013.25
ISA_SCALE_HEIGHT_M = 44330.77      # T0 / L
ISA_EXPONENT = 5.255876            # g0 * M / (R * L)

#: How old a pressure reading may be and still be used. The weather slot
#: publishes at 0.2 Hz by default, so this is generous by design; the limit is
#: there for the sensor that has *stopped*, not for normal cadence. Pressure
#: moves a few hPa per hour in bad weather, so five minutes is well under a
#: metre of drift - while an hour-old reading, which is what a silently dead
#: Airmar leaves behind, can be tens of metres wrong and is refused.
MAX_READING_AGE_S = 300.0

#: A pressure reading has to be physically possible before it is arithmetic.
#: The raw bound is wide enough for a high-altitude site; the reduced bound is
#: the meteorological one - recorded sea-level extremes are roughly 870 to 1084
#: hPa, and anything outside that is a decoder fault, not weather.
PLAUSIBLE_STATION_HPA = (300.0, 1100.0)
PLAUSIBLE_SEA_LEVEL_HPA = (870.0, 1090.0)

#: The troposphere layer the formulas above are valid in. No ground station is
#: near the top of it; the bound exists so a mistyped elevation cannot produce
#: arithmetic that is merely wrong rather than refused.
ELEVATION_BOUNDS_M = (-500.0, 9000.0)


def reduce_to_sea_level(station_hpa: float, elevation_m: float) -> float:
    """Station pressure at `elevation_m` -> the equivalent sea-level pressure."""
    return station_hpa / (1.0 - elevation_m / ISA_SCALE_HEIGHT_M) ** ISA_EXPONENT


def correct_pressure_altitude(pressure_altitude_m: float, sea_level_hpa: float) -> float:
    """A 1013.25-referenced altitude, re-referenced to `sea_level_hpa`.

    Derived rather than approximated. The reported altitude implies a static
    pressure at the aircraft; that pressure is then read back as an altitude in
    an atmosphere whose sea level is the measured one:

        p_ac = p0 * (1 - PA/H) ** n
        TA   = H * (1 - (p_ac / p_sea) ** (1/n))
             = H * (1 - k) + k * PA        with k = (p0 / p_sea) ** (1/n)

    The affine form is the same arithmetic, and shows the shape of the answer:
    a fixed offset that shrinks with altitude, not a constant added everywhere.
    A low-pressure day gives a negative offset - "high to low, look out below" -
    and if that sign ever comes out the other way this function is wrong.
    """
    k = (ISA_SEA_LEVEL_HPA / sea_level_hpa) ** (1.0 / ISA_EXPONENT)
    return ISA_SCALE_HEIGHT_M * (1.0 - k) + k * pressure_altitude_m


class BarometricReference:
    """The station's current barometer reading, and whether it may be used.

    One instance is owned by the agent and handed to the ADS-B driver, so that
    the driver never reaches for the weather station itself: rediscovery can
    rebuild either slot without the other noticing, and a station with no
    weather head simply has a reference that never becomes usable.

    Every refusal is nameable. `state()` is what health telemetry reports, and
    the reason is written for whoever is reading the console, because "the
    correction is off" and "the correction is on and the barometer died" are
    the same blank field otherwise.
    """

    def __init__(
        self,
        enabled: bool = False,
        elevation_m: float | None = None,
        max_age_s: float = MAX_READING_AGE_S,
    ) -> None:
        self.enabled = bool(enabled)
        self.elevation_m = elevation_m
        self.max_age_s = float(max_age_s)
        self._pressure_hpa: float | None = None
        self._taken: float | None = None

    # --- inputs -----------------------------------------------------------

    def configure(self, enabled: bool, elevation_m: float | None) -> None:
        """Track site configuration. Called every tick; both may change under
        us when the setup page saves or a `config.set` arrives."""
        self.enabled = bool(enabled)
        self.elevation_m = elevation_m

    def update(self, pressure_hpa: float | None, now: float | None = None) -> None:
        """Take a reading. `None` means the weather station reported no
        pressure, which ages out the previous one rather than preserving it -
        a sensor that has stopped saying must not keep its last word forever."""
        if pressure_hpa is None:
            return
        self._pressure_hpa = float(pressure_hpa)
        self._taken = time.monotonic() if now is None else now

    def forget(self) -> None:
        """Drop the reading entirely. For a weather slot that has gone away."""
        self._pressure_hpa = None
        self._taken = None

    # --- the decision -----------------------------------------------------

    def sea_level_hpa(self, now: float | None = None) -> tuple[float | None, str]:
        """The usable sea-level pressure, or None and the reason there is none."""
        if not self.enabled:
            return None, "disabled in site configuration"
        if self.elevation_m is None:
            # Deliberately a refusal and not an assumption of zero. See the
            # module docstring: guessing here is worth the station's elevation
            # in error, applied with confidence.
            return None, "station elevation is not set"
        low, high = ELEVATION_BOUNDS_M
        if not low <= self.elevation_m <= high:
            return None, f"station elevation {self.elevation_m:.0f} m is out of range"
        if self._pressure_hpa is None or self._taken is None:
            return None, "no barometric reading from the weather station"
        age = (time.monotonic() if now is None else now) - self._taken
        if age > self.max_age_s:
            return None, f"barometric reading is {age:.0f} s old"
        low, high = PLAUSIBLE_STATION_HPA
        if not low <= self._pressure_hpa <= high:
            return None, f"barometric reading of {self._pressure_hpa:.1f} hPa is implausible"
        sea_level = reduce_to_sea_level(self._pressure_hpa, self.elevation_m)
        low, high = PLAUSIBLE_SEA_LEVEL_HPA
        if not low <= sea_level <= high:
            return None, (
                f"{self._pressure_hpa:.1f} hPa at {self.elevation_m:.0f} m reduces to "
                f"{sea_level:.1f} hPa at sea level, which is not weather"
            )
        return sea_level, ""

    def correct(self, altitude_m: float | None, altitude_type: str | None) -> float | None:
        """The corrected altitude for one contact, or None.

        Null rather than a fallback in every refusing case, including the two
        that are properties of the contact rather than of the station: an
        altitude the receiver never sent, and an altitude that is already
        geometric and therefore has nothing to correct.
        """
        if altitude_m is None or altitude_type != "pressure":
            return None
        sea_level, _ = self.sea_level_hpa()
        if sea_level is None:
            return None
        return correct_pressure_altitude(altitude_m, sea_level)

    # --- reporting --------------------------------------------------------

    def state(self) -> dict:
        """For health telemetry: is it correcting, and if not, why not."""
        sea_level, reason = self.sea_level_hpa()
        return {
            "enabled": self.enabled,
            "active": sea_level is not None,
            "reason": reason,
            "station_pressure_hpa": (
                None if self._pressure_hpa is None else round(self._pressure_hpa, 1)
            ),
            "sea_level_pressure_hpa": None if sea_level is None else round(sea_level, 1),
            "station_elevation_m": self.elevation_m,
        }
