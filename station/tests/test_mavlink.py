"""ADS-B over MAVLink: the units, the validity flags, and the framing.

Unit-scaling mistakes here put an aircraft at a plausible wrong altitude, which
is worse than an obvious failure — so every conversion is asserted against a
value worked out by hand from the message definition, not against the code.
"""

import unittest

from gsu.devices import mavlink
from gsu.devices.pingrx import PingRxAdsb, SimulatedPingRx


class _Source:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    def read(self) -> bytes:
        data, self.data = self.data, b""
        return data

    def close(self) -> None:
        pass


ALL_VALID = (
    mavlink.FLAG_VALID_COORDS
    | mavlink.FLAG_VALID_ALTITUDE
    | mavlink.FLAG_VALID_HEADING
    | mavlink.FLAG_VALID_VELOCITY
    | mavlink.FLAG_VALID_CALLSIGN
)


class UnitTests(unittest.TestCase):
    def test_units_are_converted_exactly_once(self):
        payload = mavlink.encode_adsb_vehicle(
            icao=0xABCDEF,
            flags=ALL_VALID,
            lat_e7=-435000000,          # -43.5 degrees
            lon_e7=1726000000,          # 172.6 degrees
            altitude_mm=10_668_000,     # 10 668 m, i.e. FL350
            heading_cdeg=27000,         # 270.00 degrees
            hor_cms=15000,              # 150 m/s
            callsign="ANZ123",
        )
        vehicle = mavlink.decode_adsb_vehicle(payload)
        self.assertEqual(vehicle.icao, "ABCDEF")
        self.assertAlmostEqual(vehicle.latitude, -43.5, places=7)
        self.assertAlmostEqual(vehicle.longitude, 172.6, places=7)
        self.assertAlmostEqual(vehicle.altitude_m, 10668.0, places=3)
        self.assertAlmostEqual(vehicle.heading_deg, 270.0, places=3)
        # 150 m/s = 150 * 3600 / 1852 knots = 291.577...
        self.assertAlmostEqual(vehicle.speed_kt, 291.5766738, places=5)
        self.assertEqual(vehicle.callsign, "ANZ123")

    def test_invalid_flags_produce_absence_not_zero(self):
        payload = mavlink.encode_adsb_vehicle(
            icao=0x111111, flags=mavlink.FLAG_VALID_ALTITUDE, altitude_mm=3_000_000,
            lat_e7=0, lon_e7=0, heading_cdeg=0, hor_cms=0, callsign="GHOST",
        )
        vehicle = mavlink.decode_adsb_vehicle(payload)
        self.assertIsNone(vehicle.latitude)
        self.assertIsNone(vehicle.longitude)
        self.assertIsNone(vehicle.heading_deg)
        self.assertIsNone(vehicle.speed_kt)
        self.assertIsNone(vehicle.callsign)
        self.assertEqual(vehicle.altitude_m, 3000.0)

    def test_invalid_sentinels_are_honoured_even_when_flagged(self):
        payload = mavlink.encode_adsb_vehicle(
            icao=1, flags=ALL_VALID,
            lat_e7=mavlink.INVALID_I32, lon_e7=mavlink.INVALID_I32,
            altitude_mm=mavlink.INVALID_I32, heading_cdeg=mavlink.INVALID_U16,
            hor_cms=mavlink.INVALID_U16,
        )
        vehicle = mavlink.decode_adsb_vehicle(payload)
        self.assertIsNone(vehicle.latitude)
        self.assertIsNone(vehicle.altitude_m)
        self.assertIsNone(vehicle.heading_deg)
        self.assertIsNone(vehicle.speed_kt)


class FramingTests(unittest.TestCase):
    def test_round_trip_through_the_parser(self):
        payload = mavlink.encode_adsb_vehicle(icao=7, flags=0)
        parser = mavlink.MavlinkParser()
        frames = list(parser.feed(mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload)))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].msgid, mavlink.MSG_ADSB_VEHICLE)

    def test_split_across_reads(self):
        frame = mavlink.build_frame(
            mavlink.MSG_ADSB_VEHICLE, mavlink.encode_adsb_vehicle(icao=7, flags=0)
        )
        parser = mavlink.MavlinkParser()
        got = list(parser.feed(frame[:9])) + list(parser.feed(frame[9:]))
        self.assertEqual(len(got), 1)

    def test_noise_does_not_stop_the_stream(self):
        frame = mavlink.build_frame(
            mavlink.MSG_ADSB_VEHICLE, mavlink.encode_adsb_vehicle(icao=7, flags=0)
        )
        parser = mavlink.MavlinkParser()
        got = list(parser.feed(b"\xfd\x02\x00rubbish\xfe\x99" + frame))
        self.assertEqual(len(got), 1, "the parser must resynchronise after noise")

    def test_a_corrupted_frame_is_dropped(self):
        frame = bytearray(
            mavlink.build_frame(
                mavlink.MSG_ADSB_VEHICLE, mavlink.encode_adsb_vehicle(icao=7, flags=0)
            )
        )
        frame[-1] ^= 0xFF
        parser = mavlink.MavlinkParser()
        self.assertEqual(list(parser.feed(bytes(frame))), [])
        self.assertGreater(parser.bad_frames, 0)

    def test_truncated_v2_payload_is_zero_padded(self):
        payload = mavlink.encode_adsb_vehicle(icao=7, flags=0).rstrip(b"\x00")
        frame = mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload)
        parser = mavlink.MavlinkParser()
        frames = list(parser.feed(frame))
        self.assertEqual(len(frames), 1)
        self.assertEqual(mavlink.decode_adsb_vehicle(frames[0].payload).icao, "000007")


class DriverTests(unittest.TestCase):
    def test_silence_is_not_an_empty_sky(self):
        driver = PingRxAdsb(_Source(), latitude=-43.5, longitude=172.6)
        self.assertIsNone(
            driver.poll(1.0),
            "a receiver that has never spoken must publish nothing, not []",
        )
        self.assertEqual(driver.status, "absent")

    def test_present_and_quiet_is_an_empty_list(self):
        source = _Source(mavlink.build_frame(mavlink.MSG_HEARTBEAT, bytes(9)))
        driver = PingRxAdsb(source, latitude=-43.5, longitude=172.6)
        self.assertEqual(driver.poll(1.0), [], "a live receiver with no contacts is clear sky")
        self.assertEqual(driver.status, "streaming")

    def test_contacts_without_a_position_are_not_published(self):
        payload = mavlink.encode_adsb_vehicle(
            icao=0x222222, flags=mavlink.FLAG_VALID_ALTITUDE, altitude_mm=1_000_000
        )
        source = _Source(mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload))
        driver = PingRxAdsb(source, latitude=-43.5, longitude=172.6)
        contacts = driver.poll(1.0)
        self.assertEqual(contacts, [])
        self.assertEqual(driver.positionless, 1)

    def test_range_and_bearing_from_the_station(self):
        # 1 degree of latitude north of the station: ~111 km, due north.
        payload = mavlink.encode_adsb_vehicle(
            icao=0x333333, flags=ALL_VALID,
            lat_e7=-425000000, lon_e7=1726000000, altitude_mm=1_000_000,
            heading_cdeg=0, hor_cms=1000, callsign="TEST",
        )
        source = _Source(mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload))
        driver = PingRxAdsb(source, latitude=-43.5, longitude=172.6)
        contact = driver.poll(1.0)[0]
        self.assertAlmostEqual(contact.range_km, 111.2, delta=1.0)
        self.assertAlmostEqual(contact.bearing, 0.0, delta=0.5)

    def test_the_simulation_goes_through_the_real_parser(self):
        driver = SimulatedPingRx(latitude=-43.5, longitude=172.6)
        contacts = driver.poll(1.0)
        self.assertIsNotNone(contacts)
        self.assertTrue(driver.describe().simulated)
        for contact in contacts:
            payload = contact.to_payload()
            self.assertIn("range_km", payload)
            self.assertIsNotNone(payload["altitude"])


if __name__ == "__main__":
    unittest.main()
