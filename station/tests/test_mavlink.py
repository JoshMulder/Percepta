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


class FlagsAreNotZeroesTests(unittest.TestCase):
    """A cleared validity flag means null. It has never meant zero.

    Every field below can legitimately BE zero — a squawk of 0000, an aircraft
    stopped on a taxiway, a heading of due north, level flight. The flag is the
    receiver telling us which of the two it means, and it is the only copy of
    that fact anywhere in the system.
    """

    def test_every_value_zero_and_every_flag_clear_yields_nulls(self):
        payload = mavlink.encode_adsb_vehicle(
            icao=0x424242, flags=0,
            lat_e7=0, lon_e7=0, altitude_mm=0, heading_cdeg=0, hor_cms=0,
            ver_cms=0, squawk=0, callsign="",
        )
        vehicle = mavlink.decode_adsb_vehicle(payload)
        for name in (
            "latitude", "longitude", "altitude_m", "heading_deg", "speed_kt",
            "vertical_speed_ms", "callsign", "squawk", "altitude_type",
        ):
            self.assertIsNone(
                getattr(vehicle, name),
                f"{name} came through as {getattr(vehicle, name)!r}, not null",
            )

    def test_a_squawk_of_zero_is_not_the_same_as_no_squawk(self):
        # The distinction in one test. 0000 is a code an aircraft can be
        # assigned; "the receiver reported no squawk" is not a code at all.
        reported = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(
                icao=1, flags=mavlink.FLAG_VALID_SQUAWK, squawk=0
            )
        )
        silent = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(icao=1, flags=0, squawk=0)
        )
        self.assertEqual(reported.squawk, 0)
        self.assertIsNone(silent.squawk)

    def test_a_squawk_travels_as_the_digits_not_as_octal(self):
        vehicle = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(
                icao=1, flags=mavlink.FLAG_VALID_SQUAWK, squawk=7700
            )
        )
        self.assertEqual(vehicle.squawk, 7700)

    def test_level_flight_is_not_the_same_as_no_vertical_velocity(self):
        level = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(
                icao=1, flags=mavlink.FLAG_VERTICAL_VELOCITY_VALID, ver_cms=0
            )
        )
        silent = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(icao=1, flags=0, ver_cms=0)
        )
        self.assertEqual(level.vertical_speed_ms, 0.0)
        self.assertIsNone(silent.vertical_speed_ms)

    def test_vertical_velocity_is_positive_climbing_in_metres_per_second(self):
        vehicle = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(
                icao=1, flags=mavlink.FLAG_VERTICAL_VELOCITY_VALID, ver_cms=-640
            )
        )
        self.assertAlmostEqual(vehicle.vertical_speed_ms, -6.4, places=6)

    def test_the_nulls_survive_all_the_way_to_the_payload(self):
        # The decoder was already honest about this; the driver was not, and
        # dropped most of these fields before anything could see them.
        payload = mavlink.encode_adsb_vehicle(
            icao=0x515151, flags=mavlink.FLAG_VALID_COORDS,
            lat_e7=-425000000, lon_e7=1726000000,
            altitude_mm=0, heading_cdeg=0, hor_cms=0, ver_cms=0, squawk=0,
            callsign="",
        )
        source = _Source(mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload))
        driver = PingRxAdsb(source, latitude=-43.5, longitude=172.6)
        contact = driver.poll(1.0)[0].to_payload()
        for name in (
            "callsign", "altitude", "track", "speed", "squawk",
            "vertical_speed", "altitude_type", "altitude_corrected_m",
            "on_ground",
        ):
            self.assertIsNone(contact[name], f"{name} was published as {contact[name]!r}")


class EverythingTheReceiverSaidTests(unittest.TestCase):
    def test_emitter_type_travels_unmapped(self):
        # Including a value this build has never heard of: naming it is a
        # display decision, so an unknown one must survive rather than be
        # flattened to "no information".
        for emitter in (0, 3, 7, 14, 19, 200):
            vehicle = mavlink.decode_adsb_vehicle(
                mavlink.encode_adsb_vehicle(icao=1, flags=0, emitter_type=emitter)
            )
            self.assertEqual(vehicle.emitter_type, emitter)

    def test_only_surface_emitters_claim_the_ground_and_nobody_claims_the_air(self):
        for emitter in (17, 18, 19):
            vehicle = mavlink.decode_adsb_vehicle(
                mavlink.encode_adsb_vehicle(icao=1, flags=0, emitter_type=emitter)
            )
            self.assertIs(vehicle.on_ground, True)
        for emitter in (0, 1, 3, 7, 14):
            vehicle = mavlink.decode_adsb_vehicle(
                mavlink.encode_adsb_vehicle(icao=1, flags=0, emitter_type=emitter)
            )
            # None and not False: ADSB_VEHICLE has no airborne/surface bit, so
            # "airborne" would be an inference, and an aircraft holding short
            # would be the contact it got wrong.
            self.assertIsNone(vehicle.on_ground)

    def test_the_source_flag_names_the_band(self):
        uat = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(icao=1, flags=mavlink.FLAG_SOURCE_UAT)
        )
        es = mavlink.decode_adsb_vehicle(mavlink.encode_adsb_vehicle(icao=1, flags=0))
        self.assertEqual(uat.source, "uat")
        self.assertEqual(es.source, "adsb")

    def test_the_altitude_datum_is_named_not_numbered(self):
        for raw, expected in (
            (mavlink.ALTITUDE_TYPE_PRESSURE, "pressure"),
            (mavlink.ALTITUDE_TYPE_GEOMETRIC, "geometric"),
            (7, None),   # a value the enumeration does not define
        ):
            vehicle = mavlink.decode_adsb_vehicle(
                mavlink.encode_adsb_vehicle(
                    icao=1, flags=mavlink.FLAG_VALID_ALTITUDE,
                    altitude_mm=1_000_000, altitude_type=raw,
                )
            )
            self.assertEqual(vehicle.altitude_type, expected)

    def test_an_altitude_that_does_not_exist_has_no_datum(self):
        vehicle = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(
                icao=1, flags=0, altitude_mm=1_000_000,
                altitude_type=mavlink.ALTITUDE_TYPE_PRESSURE,
            )
        )
        self.assertIsNone(vehicle.altitude_m)
        self.assertIsNone(vehicle.altitude_type)

    def test_the_two_remaining_flags_are_decoded_rather_than_defined_and_ignored(self):
        vehicle = mavlink.decode_adsb_vehicle(
            mavlink.encode_adsb_vehicle(
                icao=1, flags=mavlink.FLAG_BARO_VALID | mavlink.FLAG_SIMULATED
            )
        )
        self.assertTrue(vehicle.baro_valid)
        self.assertTrue(vehicle.simulated)

    def test_time_since_last_contact_reaches_the_payload(self):
        payload = mavlink.encode_adsb_vehicle(
            icao=0x616161, flags=mavlink.FLAG_VALID_COORDS,
            lat_e7=-425000000, lon_e7=1726000000, tslc=42,
        )
        source = _Source(mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload))
        contact = PingRxAdsb(source, latitude=-43.5, longitude=172.6).poll(1.0)[0]
        self.assertEqual(contact.to_payload()["seconds_since_contact"], 42)

    def test_the_driver_forwards_the_whole_decoded_contact(self):
        payload = mavlink.encode_adsb_vehicle(
            icao=0x7C1B2D,
            flags=(
                ALL_VALID | mavlink.FLAG_VALID_SQUAWK
                | mavlink.FLAG_VERTICAL_VELOCITY_VALID
                | mavlink.FLAG_SIMULATED | mavlink.FLAG_SOURCE_UAT
            ),
            lat_e7=-425000000, lon_e7=1726000000, altitude_mm=2_500_000,
            heading_cdeg=9000, hor_cms=12000, ver_cms=512, squawk=4321,
            callsign="TEST12", emitter_type=7, tslc=3,
            altitude_type=mavlink.ALTITUDE_TYPE_GEOMETRIC,
        )
        source = _Source(mavlink.build_frame(mavlink.MSG_ADSB_VEHICLE, payload))
        contact = PingRxAdsb(source, latitude=-43.5, longitude=172.6).poll(1.0)[0]
        got = contact.to_payload()
        self.assertEqual(got["squawk"], 4321)
        self.assertEqual(got["emitter_type"], 7)
        self.assertEqual(got["altitude_type"], "geometric")
        self.assertAlmostEqual(got["vertical_speed"], 5.1, places=6)
        self.assertEqual(got["seconds_since_contact"], 3)
        self.assertIs(got["simulated"], True)
        self.assertEqual(got["source"], "uat")
        self.assertIsNone(got["on_ground"])


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
        self.assertGreater(parser.false_starts, 0)

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


class SimulationCoverageTests(unittest.TestCase):
    """The simulated sky has to be worth building a console against.

    A simulator that emits one emitter type, always a squawk, and never a
    cleared flag lets exactly the bugs this change is about through: an icon
    switch with one branch, and a squawk field that has never been null.
    """

    def _many(self, count: int = 400) -> list:
        driver = SimulatedPingRx(latitude=-43.5, longitude=172.6)
        driver._source._rng.seed(11)
        driver._source._contacts = [
            driver._source._new_contact() for _ in range(count)
        ]
        return driver.poll(1.0)

    def test_it_emits_a_spread_of_emitter_types(self):
        seen = {contact.emitter_type for contact in self._many()}
        # Everything ADSB_EMITTER_TYPE defines except the unassigned values
        # (8, 13, 16) and `space` (15).
        expected = {0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 14, 17, 18, 19}
        self.assertEqual(seen, expected)

    def test_surface_emitters_appear_and_report_the_ground(self):
        contacts = self._many()
        surface = [c for c in contacts if c.emitter_type in (17, 18, 19)]
        self.assertTrue(surface)
        self.assertTrue(all(c.on_ground is True for c in surface))
        self.assertTrue(
            all(c.on_ground is None for c in contacts if c.emitter_type not in (17, 18, 19))
        )

    def test_some_contacts_report_no_squawk_and_no_vertical_speed(self):
        contacts = self._many()
        self.assertTrue(any(c.squawk is None for c in contacts))
        self.assertTrue(any(c.squawk is not None for c in contacts))
        self.assertTrue(any(c.vertical_speed is None for c in contacts))
        self.assertTrue(any(c.vertical_speed is not None for c in contacts))

    def test_every_squawk_it_emits_is_octal(self):
        # Squawk digits run 0-7. A simulator that emitted 1984 would be
        # teaching whatever renders it a shape no transponder can send.
        for contact in self._many():
            if contact.squawk is None:
                continue
            self.assertNotIn(
                "8", f"{contact.squawk:04d}", f"{contact.squawk} is not a Mode A code"
            )
            self.assertNotIn("9", f"{contact.squawk:04d}")

    def test_both_altitude_datums_and_both_source_bands_occur(self):
        contacts = self._many()
        self.assertEqual(
            {c.altitude_type for c in contacts}, {"pressure", "geometric"}
        )
        self.assertEqual({c.source for c in contacts}, {"adsb", "uat"})

    def test_no_callsign_it_emits_is_truncated_on_the_wire(self):
        # ADS-B carries eight characters, and the encoder clips silently. A
        # profile prefix one letter too long produces "BALLOON8", which reads
        # as a decoder bug rather than as a balloon. Every callsign this source
        # emits is a prefix followed by exactly three digits, so anything else
        # is something that got cut.
        for contact in self._many():
            if contact.callsign is None:
                continue
            self.assertRegex(
                contact.callsign, r"^[A-Z]{3,5}[0-9]{3}$",
                f"{contact.callsign!r} was clipped by the eight-character limit",
            )

    def test_every_contact_says_it_is_simulated(self):
        # The one field that must never be missing from this source: a test
        # injection that could be read as traffic is the failure the flag
        # exists to prevent.
        self.assertTrue(all(c.simulated is True for c in self._many()))


if __name__ == "__main__":
    unittest.main()


class ReceiverLivenessTests(unittest.TestCase):
    """A receiver under clear sky must not look like a dead one.

    The ping RX Pro sends ADSB_VEHICLE only when there is an aircraft, and
    sends no HEARTBEAT. Its other three messages were failing the checksum for
    want of a CRC_EXTRA byte, so with nothing overhead the station saw zero
    frames and 61 false starts and called the device absent — the same thing it
    would say about an unplugged dongle.
    """

    def test_the_receivers_own_messages_validate(self):
        for msgid in (66, 202, 203):
            parser = mavlink.MavlinkParser()
            frames = list(parser.feed(mavlink.build_frame(msgid, b"\x01\x02\x03")))
            self.assertEqual([f.msgid for f in frames], [msgid])
            self.assertEqual(parser.false_starts, 0, msgid)

    def test_request_data_stream_uses_the_published_byte(self):
        # 148 is REQUEST_DATA_STREAM's value in common.xml. It agreeing with
        # what the hardware produced is what validates deriving the other two
        # the same way.
        self.assertEqual(mavlink.CRC_EXTRA[66], 148)

    def test_they_prove_the_link_without_being_decoded(self):
        # Liveness only. Nothing reads their payloads, and the contact table
        # must stay empty.
        rx = PingRxAdsb(
            _Source(b"".join(
                mavlink.build_frame(m, b"\x00\x00") for m in (66, 202, 203)
            )),
            latitude=-42.4, longitude=173.68,
        )
        self.assertEqual(rx.poll(1.0), [])
        self.assertEqual(rx.status, "streaming")

    def test_a_silent_port_is_still_absent(self):
        # The fix must not make every configured port look connected.
        rx = PingRxAdsb(_Source(b""), latitude=-42.4, longitude=173.68)
        self.assertIsNone(rx.poll(1.0))
        self.assertEqual(rx.status, "absent")

    def test_a_wrong_crc_on_one_of_them_is_still_rejected(self):
        bad = bytearray(mavlink.build_frame(202, b"\x00\x00"))
        bad[-1] ^= 0xFF
        parser = mavlink.MavlinkParser()
        self.assertEqual(list(parser.feed(bytes(bad))), [])
        self.assertGreater(parser.false_starts, 0)
