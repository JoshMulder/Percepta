"""The barometric altitude correction, and every way it declines to run.

Two kinds of test here, and the second kind matters more. The arithmetic is
checked against values worked out from the ISA barometric formula by hand — a
correction with the wrong sign is worse than none, so the sign is asserted in
both directions and against the aviation rule of thumb it has to agree with.

But most of these are *refusals*. The failure this feature can actually cause is
not a slightly wrong number, it is a confident number computed from a pressure
reading an hour old, or from a station pressure treated as a sea-level one. Each
of those is a named test, because each of them produces output that looks
exactly as trustworthy as the correct answer.
"""

import unittest

from gsu.devices import mavlink
from gsu.devices.altitude import (
    ISA_EXPONENT,
    ISA_SCALE_HEIGHT_M,
    ISA_SEA_LEVEL_HPA,
    BarometricReference,
    correct_pressure_altitude,
    reduce_to_sea_level,
)
from gsu.devices.pingrx import PingRxAdsb


def _pressure_altitude(pressure_hpa: float) -> float:
    """What a transponder sitting in `pressure_hpa` of static pressure reports:
    the ISA altitude of that pressure against the 1013.25 hPa datum."""
    return ISA_SCALE_HEIGHT_M * (
        1.0 - (pressure_hpa / ISA_SEA_LEVEL_HPA) ** (1.0 / ISA_EXPONENT)
    )


class _Source:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    def read(self) -> bytes:
        data, self.data = self.data, b""
        return data

    def close(self) -> None:
        pass


class ArithmeticTests(unittest.TestCase):
    def test_standard_pressure_changes_nothing_at_any_altitude(self):
        for altitude in (0.0, 1000.0, 11000.0):
            self.assertAlmostEqual(
                correct_pressure_altitude(altitude, ISA_SEA_LEVEL_HPA), altitude, places=6
            )

    def test_the_sign_is_the_way_round_aviation_says_it_is(self):
        # "High to low, look out below": flying into low pressure, the true
        # altitude is BELOW the reported pressure altitude. Getting this
        # backwards is the failure mode this whole module is written around.
        self.assertLess(correct_pressure_altitude(1000.0, 990.0), 1000.0)
        self.assertGreater(correct_pressure_altitude(1000.0, 1030.0), 1000.0)

    def test_one_hectopascal_is_about_twenty_seven_feet(self):
        # The rule of thumb the formula has to agree with near sea level.
        metres = correct_pressure_altitude(0.0, ISA_SEA_LEVEL_HPA - 1.0)
        self.assertAlmostEqual(metres * 3.28084, -27.0, delta=0.5)

    def test_the_correction_shrinks_with_altitude(self):
        # It is not a constant offset: the column compresses. A test that
        # asserted a fixed number of metres everywhere would be asserting an
        # approximation this deliberately does not use.
        low = correct_pressure_altitude(0.0, 990.0)
        high = correct_pressure_altitude(10_000.0, 990.0) - 10_000.0
        self.assertLess(abs(high), abs(low))

    def test_a_transponder_on_the_station_barometer_reads_the_station_elevation(self):
        """The whole datum question, in one assertion.

        Reduce the station's own pressure to sea level, then correct the
        pressure altitude that same pressure implies: the answer must be the
        station's elevation. If station pressure were used as if it were
        sea-level pressure, this comes out at zero regardless of elevation —
        which is precisely the several-hundred-metre error in the wrong
        direction that the module docstring is about.
        """
        for elevation in (0.0, 120.0, 800.0):
            for station_hpa in (1013.25, 975.0, 940.0):
                sea_level = reduce_to_sea_level(station_hpa, elevation)
                reported = _pressure_altitude(station_hpa)
                self.assertAlmostEqual(
                    correct_pressure_altitude(reported, sea_level),
                    elevation, places=3,
                    msg=f"{station_hpa} hPa measured at {elevation} m",
                )

    def test_skipping_the_sea_level_reduction_is_wrong_by_the_elevation(self):
        # Named so that anyone tempted to simplify the two steps into one sees
        # what it costs: 600 m of error, applied downwards, on a number the
        # console would render as an improvement.
        elevation, station_hpa = 600.0, 943.0
        reported = _pressure_altitude(station_hpa)
        right = correct_pressure_altitude(
            reported, reduce_to_sea_level(station_hpa, elevation)
        )
        wrong = correct_pressure_altitude(reported, station_hpa)
        self.assertAlmostEqual(right, elevation, places=3)
        self.assertAlmostEqual(wrong, 0.0, places=3)
        self.assertAlmostEqual(right - wrong, elevation, places=3)


class RefusalTests(unittest.TestCase):
    def _working(self) -> BarometricReference:
        reference = BarometricReference(enabled=True, elevation_m=120.0)
        reference.update(1002.0)
        return reference

    def test_the_baseline_actually_corrects(self):
        # Otherwise every refusal test below passes for the wrong reason.
        reference = self._working()
        self.assertTrue(reference.state()["active"])
        self.assertIsNotNone(reference.correct(3000.0, "pressure"))

    def test_off_by_default(self):
        reference = BarometricReference()
        reference.update(1002.0)
        self.assertFalse(reference.state()["enabled"])
        self.assertIsNone(reference.correct(3000.0, "pressure"))
        self.assertIn("disabled", reference.state()["reason"])

    def test_no_station_elevation_refuses_rather_than_assuming_sea_level(self):
        reference = BarometricReference(enabled=True, elevation_m=None)
        reference.update(1002.0)
        self.assertIsNone(reference.correct(3000.0, "pressure"))
        self.assertIn("elevation", reference.state()["reason"])

    def test_no_weather_station_means_no_correction(self):
        reference = BarometricReference(enabled=True, elevation_m=120.0)
        self.assertIsNone(reference.correct(3000.0, "pressure"))
        self.assertIn("no barometric reading", reference.state()["reason"])

    def test_a_sensor_reporting_nothing_never_becomes_a_reading(self):
        reference = BarometricReference(enabled=True, elevation_m=120.0)
        reference.update(None)
        self.assertIsNone(reference.correct(3000.0, "pressure"))

    def test_a_stale_reading_is_refused(self):
        reference = BarometricReference(enabled=True, elevation_m=120.0, max_age_s=300.0)
        reference.update(1002.0, now=0.0)
        # Fresh at four minutes, refused at an hour. An hour-old pressure is
        # the one a silently dead Airmar leaves behind.
        self.assertIsNotNone(reference.sea_level_hpa(now=240.0)[0])
        self.assertIsNone(reference.sea_level_hpa(now=3600.0)[0])
        self.assertIn("old", reference.sea_level_hpa(now=3600.0)[1])

    def test_the_last_reading_does_not_become_permanent(self):
        # A weather head that reports pressure once and then stops must not
        # keep that value alive by continuing to report other fields.
        reference = BarometricReference(enabled=True, elevation_m=120.0, max_age_s=10.0)
        reference.update(1002.0, now=0.0)
        for _ in range(5):
            reference.update(None)
        self.assertIsNone(reference.sea_level_hpa(now=100.0)[0])

    def test_an_implausible_reading_is_refused(self):
        for pressure in (0.0, 12.5, 1450.0):
            reference = BarometricReference(enabled=True, elevation_m=0.0)
            reference.update(pressure)
            self.assertIsNone(
                reference.correct(3000.0, "pressure"), f"{pressure} hPa was accepted"
            )

    def test_a_reading_that_reduces_to_impossible_weather_is_refused(self):
        # Sea-level pressure at 1013 hPa measured at 2400 m reduces to about
        # 1358 hPa, which is not weather — it is an elevation or a decoder
        # fault, and either way the correction must not run.
        reference = BarometricReference(enabled=True, elevation_m=2400.0)
        reference.update(1013.25)
        self.assertIsNone(reference.correct(3000.0, "pressure"))
        self.assertIn("not weather", reference.state()["reason"])

    def test_a_wildly_wrong_elevation_is_refused(self):
        reference = BarometricReference(enabled=True, elevation_m=45_000.0)
        reference.update(1002.0)
        self.assertIsNone(reference.correct(3000.0, "pressure"))

    def test_a_geometric_altitude_has_nothing_to_correct(self):
        self.assertIsNone(self._working().correct(3000.0, "geometric"))

    def test_an_unstated_datum_is_not_assumed_to_be_pressure(self):
        self.assertIsNone(self._working().correct(3000.0, None))

    def test_an_absent_altitude_stays_absent(self):
        self.assertIsNone(self._working().correct(None, "pressure"))

    def test_state_names_the_reason_for_health(self):
        reference = BarometricReference(enabled=True, elevation_m=None)
        state = reference.state()
        self.assertTrue(state["enabled"])
        self.assertFalse(state["active"])
        self.assertTrue(state["reason"])
        self.assertIsNone(state["sea_level_pressure_hpa"])

        working = self._working().state()
        self.assertTrue(working["active"])
        self.assertEqual(working["reason"], "")
        self.assertEqual(working["station_pressure_hpa"], 1002.0)
        # Reduced upward from the station reading, because the station is above
        # sea level. If this ever reads 1002.0 the reduction has been dropped.
        self.assertGreater(working["sea_level_pressure_hpa"], 1002.0)


class DriverTests(unittest.TestCase):
    """The correction as the ADS-B stream actually sees it."""

    def _contact(self, reference, altitude_type=mavlink.ALTITUDE_TYPE_PRESSURE):
        payload = mavlink.encode_adsb_vehicle(
            icao=0x4CA1FB,
            flags=mavlink.FLAG_VALID_COORDS | mavlink.FLAG_VALID_ALTITUDE,
            lat_e7=-425000000, lon_e7=1726000000,
            altitude_mm=3_000_000, altitude_type=altitude_type,
        )
        driver = PingRxAdsb(
            _Source(mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload)),
            latitude=-43.5, longitude=172.6, altitude_reference=reference,
        )
        return driver.poll(1.0)[0].to_payload()

    def test_a_driver_with_no_reference_publishes_null_rather_than_failing(self):
        payload = self._contact(None)
        self.assertEqual(payload["altitude_m"], 3000)
        self.assertIsNone(payload["altitude_corrected_m"])

    def test_the_corrected_altitude_travels_beside_the_reported_one(self):
        reference = BarometricReference(enabled=True, elevation_m=0.0)
        reference.update(995.0)
        payload = self._contact(reference)
        # Both, always. The corrected value never replaces what was received.
        self.assertEqual(payload["altitude_m"], 3000)
        self.assertIsNotNone(payload["altitude_corrected_m"])
        self.assertLess(payload["altitude_corrected_m"], 3000)
        self.assertEqual(payload["altitude_type"], "pressure")

    def test_a_geometric_contact_is_published_uncorrected(self):
        reference = BarometricReference(enabled=True, elevation_m=0.0)
        reference.update(995.0)
        payload = self._contact(reference, mavlink.ALTITUDE_TYPE_GEOMETRIC)
        self.assertEqual(payload["altitude_type"], "geometric")
        self.assertEqual(payload["altitude_m"], 3000)
        self.assertIsNone(payload["altitude_corrected_m"])


if __name__ == "__main__":
    unittest.main()
