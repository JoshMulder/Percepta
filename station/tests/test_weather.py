"""The Airmar, and the fields it has no sensor for.

The point of these tests is the absence: a 110WX with no RH module must never
produce a humidity, and no 110WX ever produces rainfall. A zero would be a
number an operator can act on and cannot tell is invented.
"""

import unittest

from gsu.devices.airmar import AirmarWeather
from gsu.devices.nmea import AirmarDecoder, checksum, parse_sentence


def sentence(body: str) -> str:
    return f"${body}*{checksum(body):02X}"


WIND = sentence("WIMWV,225.0,T,12.5,N,A")
COMPOSITE = sentence(
    "WIMDA,29.7373,I,1.0070,B,14.2,C,,,53.2,,4.9,C,225.0,T,220.0,M,12.5,N,6.4,M"
)
TRANSDUCERS = sentence("WIXDR,C,14.2,C,AirTemp,P,100700,P,Baro,H,53.2,P,RelHum")


class _Source:
    def __init__(self, text: str = "") -> None:
        self.data = text.encode()

    def read(self) -> bytes:
        data, self.data = self.data, b""
        return data

    def close(self) -> None:
        pass


class SentenceTests(unittest.TestCase):
    def test_checksum_is_enforced(self):
        good = sentence("WIMWV,225.0,T,12.5,N,A")
        self.assertIsNotNone(parse_sentence(good))
        self.assertIsNone(parse_sentence(good[:-2] + "00"))

    def test_a_sentence_without_a_checksum_is_refused(self):
        self.assertIsNone(parse_sentence("$WIMWV,225.0,T,12.5,N,A"))

    def test_void_status_is_not_data(self):
        decoder = AirmarDecoder()
        decoder.feed_line(sentence("WIMWV,225.0,T,12.5,N,V"))
        self.assertIsNone(decoder.state.wind_kt)

    def test_wind_units_are_converted(self):
        decoder = AirmarDecoder()
        decoder.feed_line(sentence("WIMWV,090.0,T,10.0,M,A"))   # 10 m/s
        self.assertAlmostEqual(decoder.state.wind_kt, 19.4384, places=3)
        decoder.feed_line(sentence("WIMWV,090.0,T,18.52,K,A"))  # 18.52 km/h
        self.assertAlmostEqual(decoder.state.wind_kt, 10.0, places=3)

    def test_composite_sentence(self):
        decoder = AirmarDecoder()
        decoder.feed_line(COMPOSITE)
        state = decoder.state
        self.assertAlmostEqual(state.pressure_hpa, 1007.0, places=1)
        self.assertAlmostEqual(state.temperature_c, 14.2, places=1)
        self.assertAlmostEqual(state.humidity_pct, 53.2, places=1)
        self.assertAlmostEqual(state.wind_dir_deg, 225.0, places=1)
        self.assertAlmostEqual(state.wind_kt, 12.5, places=1)

    def test_transducer_sentence(self):
        decoder = AirmarDecoder()
        decoder.feed_line(TRANSDUCERS)
        self.assertAlmostEqual(decoder.state.pressure_hpa, 1007.0, places=1)
        self.assertAlmostEqual(decoder.state.temperature_c, 14.2, places=1)
        self.assertAlmostEqual(decoder.state.humidity_pct, 53.2, places=1)


class DriverTests(unittest.TestCase):
    def test_nothing_heard_is_no_reading(self):
        driver = AirmarWeather(_Source())
        self.assertIsNone(driver.read(1.0))
        self.assertEqual(driver.status, "silent")

    def test_without_the_humidity_module_there_is_no_humidity(self):
        driver = AirmarWeather(_Source(WIND + "\r\n" + COMPOSITE + "\r\n"),
                               humidity_module=False)
        reading = driver.read(1.0)
        payload = reading.to_payload()
        self.assertIsNone(reading.humidity_pct)
        self.assertNotIn(
            "humidity_pct", payload,
            "an instrument with no RH module must publish no humidity at all",
        )

    def test_with_the_module_humidity_is_published(self):
        driver = AirmarWeather(_Source(COMPOSITE + "\r\n"), humidity_module=True)
        self.assertIn("humidity_pct", driver.read(1.0).to_payload())

    def test_rain_and_visibility_are_absent_not_zero(self):
        driver = AirmarWeather(_Source(WIND + "\r\n" + COMPOSITE + "\r\n"))
        payload = driver.read(1.0).to_payload()
        for field in ("rain_rate_mmh", "rain_mm_today", "visibility_km", "sky"):
            self.assertNotIn(
                field, payload,
                f"{field} has no sensor on a 110WX and must not be published",
            )

    def test_gust_is_the_peak_of_the_window(self):
        driver = AirmarWeather(_Source(sentence("WIMWV,225.0,T,10.0,N,A") + "\r\n"))
        driver.read(1.0)
        driver.source.data = (sentence("WIMWV,225.0,T,25.0,N,A") + "\r\n").encode()
        driver.read(1.0)
        driver.source.data = (sentence("WIMWV,225.0,T,11.0,N,A") + "\r\n").encode()
        reading = driver.read(1.0)
        self.assertAlmostEqual(reading.wind_kt, 11.0, places=1)
        self.assertAlmostEqual(reading.gust_kt, 25.0, places=1)

    def test_relative_wind_is_corrected_by_the_mast_offset(self):
        driver = AirmarWeather(
            _Source(sentence("WIMWV,010.0,R,12.0,N,A") + "\r\n"), mast_offset_deg=90.0
        )
        self.assertAlmostEqual(driver.read(1.0).wind_dir_deg, 100.0, places=1)

    def test_partial_payload_still_carries_what_was_measured(self):
        driver = AirmarWeather(_Source(WIND + "\r\n" + COMPOSITE + "\r\n"))
        payload = driver.read(1.0).to_payload()
        self.assertEqual(payload["kind"], "weather")
        self.assertIn("wind_kt", payload)
        self.assertIn("temperature_c", payload)
        self.assertIn("pressure_hpa", payload)


if __name__ == "__main__":
    unittest.main()
