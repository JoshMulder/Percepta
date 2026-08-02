"""The station as the platform sees it: channels, commands, and the schemas.

Everything here runs offline against `contract/schemas/`, so a schema change
lands as a test failure on this side rather than as a surprise in conformance.
"""

import json
import math
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

from gsu.agent import Agent
from gsu.commands import CommandRouter, build_handlers
from gsu.config import AgentConfig
from gsu.credentials import CredentialStore, Enrolment
from gsu.devices import registry
from gsu.enrolment import Renewer
from gsu.health import Health

SCHEMAS = Path(__file__).resolve().parent.parent.parent / "contract" / "schemas"
TELEMETRY = Draft202012Validator(json.loads((SCHEMAS / "telemetry.schema.json").read_text()))
AUDIO = Draft202012Validator(json.loads((SCHEMAS / "audio.schema.json").read_text()))
COMMANDS = json.loads((SCHEMAS / "command.schema.json").read_text())

STATION = "29ed8568-999e-4725-8daa-3ee3cea1751e"


def agent_in(directory: str, traffic: str = "low") -> Agent:
    config = AgentConfig(
        home=Path(directory), setup_enabled=False, single_instance=False,
        airband_traffic=traffic, demo=True)
    return Agent(config)


class PayloadTests(unittest.TestCase):
    """Every kind the station emits, against the contract's own schemas."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        # Busy airband so the audio path is exercised within the test window.
        cls.agent = agent_in(cls._dir.name, traffic="busy")
        cls.sent: list[dict] = []
        cls.agent._publish = lambda topic, payload: cls.sent.append(payload) or True
        # Somebody is listening. Audio is demand-driven — 512 kbit/s during an
        # over is the largest thing this station sends — so without this the
        # busy profile produces audio locally and publishes none of it, which
        # is the correct behaviour and not what this fixture is for.
        cls.agent.radio.want_audio(True, 3600)
        for index in range(60):
            cls.agent.step(1.0, weather_due=index % 5 == 0, health_due=index == 10)

    @classmethod
    def tearDownClass(cls):
        cls.agent.shutdown()
        cls._dir.cleanup()

    def by_kind(self, kind: str) -> list[dict]:
        return [payload for payload in self.sent if payload.get("kind") == kind]

    def test_every_contract_kind_is_published(self):
        for kind in ("adsb", "power", "radio", "light", "weather"):
            self.assertTrue(self.by_kind(kind), f"nothing published for {kind}")

    def test_telemetry_matches_the_schema(self):
        # `health` belongs in this list. It was left out when it was still a
        # kind the platform did not know, and stayed out after it was adopted —
        # which is how two schema violations in it went unnoticed: a `status`
        # using the condition-severity vocabulary instead of the summary one,
        # and a null `expires_at` before enrolment. Conformance did not catch
        # them either, because a health frame only lands inside its sample
        # window sometimes, and when it did the station happened to be healthy
        # and enrolled — the two states where the payload is valid.
        #
        # `adsb` is checked separately below: contract 1.0 renamed its fields
        # and this station has not caught up, which would otherwise mask the
        # five kinds that do still conform.
        for kind in ("power", "radio", "light", "weather", "health"):
            for payload in self.by_kind(kind)[:5]:
                errors = sorted(TELEMETRY.iter_errors(payload), key=str)
                self.assertFalse(errors, f"{kind}: {[e.message for e in errors]}")

    @unittest.expectedFailure
    def test_adsb_matches_the_schema(self):
        """KNOWN GAP against contract 1.0: the aircraft fields were renamed.

        Every other measured value in the contract carries its unit, and the
        contact object was the exception — `altitude` (metres) sat beside
        `altitude_corrected_m`, and `speed` (knots) beside `vertical_speed`
        (metres per second). Aviation convention is feet, so an unsuffixed
        `altitude` is a 3.28x error waiting to happen in the highest-volume
        payload in the system. 1.0 renamed them to `altitude_m`, `speed_kt`,
        `vertical_speed_ms`, `track_deg` and `bearing_deg` while a rename was
        still free.

        This station still emits the old names. Delete this decorator once the
        publisher is updated; until then the failure is the work, not a
        regression.
        """
        payloads = self.by_kind("adsb")
        self.assertTrue(payloads, "the busy profile should have produced adsb")
        for payload in payloads[:5]:
            errors = sorted(TELEMETRY.iter_errors(payload), key=str)
            self.assertFalse(errors, f"adsb: {[e.message for e in errors]}")

    @unittest.expectedFailure
    def test_audio_matches_the_schema_and_is_gated(self):
        """KNOWN GAP against contract 1.0: this station still sends PCM.

        The contract fixed the audio format as Opus before it was locked
        (`contract/schemas/audio.schema.json`), on the reasoning that freezing
        the most expensive payload on a metered link in a shape everybody
        already knew was wrong would buy a breaking change later. This station
        has not been built to it yet, so the assertion below is the work
        remaining rather than a regression.

        **When Opus lands, this starts passing and unittest reports an
        unexpected success** - which is the signal to delete this decorator.
        """
        audio = self.by_kind("audio")
        self.assertTrue(audio, "the busy profile should have produced audio")
        for payload in audio[:3]:
            errors = sorted(AUDIO.iter_errors(payload), key=str)
            self.assertFalse(errors, [e.message for e in errors])

    def test_audio_is_not_published_when_nobody_is_listening(self):
        """The second gate, and the expensive one.

        The squelch decides whether there is audio at all; this decides whether
        it goes up a metered link. Airband is silent most of the time but an
        over is 512 kbit/s, and a station at a site nobody has open should cost
        nothing — the spectrum has been demand-driven since it was written, for
        a cost two orders of magnitude smaller, and audio was the one left on.
        """
        with tempfile.TemporaryDirectory() as home:
            agent = agent_in(home, traffic="busy")
            self.addCleanup(agent.shutdown)
            sent: list[dict] = []
            agent._publish = lambda topic, payload: sent.append(payload) or True
            for _ in range(30):
                agent.step(1.0)
            self.assertEqual(
                [p for p in sent if p.get("kind") == "audio"], [],
                "audio was published with nobody listening",
            )
            # ...and it is the *link* that is spared, not the recording. A
            # transmission nobody had a console open for is still on the disk.
            self.assertTrue(agent.store.stats().get("recordings", 0) > 0
                            or agent.store.stats().get("audio_files", 0) > 0,
                            f"nothing was recorded either: {agent.store.stats()}")

            # Now ask, and it arrives.
            agent.radio.want_audio(True, 60)
            for _ in range(20):
                agent.step(1.0)
            self.assertTrue([p for p in sent if p.get("kind") == "audio"],
                            "audio did not resume when it was asked for")

    def test_no_payload_asserts_an_organisation(self):
        # contract/README.md rule 1. A station never says which tenant it is.
        for payload in self.sent:
            self.assertNotIn("organization_id", payload)
            self.assertNotIn("org_id", payload)

    def test_health_reports_config_version_and_devices(self):
        health = self.by_kind("health")
        self.assertTrue(health)
        self.assertIn("config_version", health[0])
        self.assertIn("devices", health[0])
        self.assertIn("unsourced_streams", health[0])

    def test_health_devices_carry_the_flag_the_demo_badge_is_built_on(self):
        # The platform badges a station DEMO from `devices[].simulated`. Every
        # slot must carry it, on a station that is entirely simulated as this
        # one is — a missing flag reads as real hardware.
        health = self.by_kind("health")[0]
        self.assertTrue(health["devices"])
        for device in health["devices"]:
            self.assertIn("simulated", device, device.get("slot"))
        configured = [d for d in health["devices"] if d["configured"]]
        self.assertTrue(all(d["simulated"] for d in configured),
                        "this fixture is all simulated devices")


class HealthPayloadTests(unittest.TestCase):
    """`health` against the schema in the states where its shape changes.

    Its shape varies with what is wrong at the time, which is exactly when
    nobody is watching. Both bugs this class was written for appeared only when
    the station was unhealthy or unenrolled.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.agent = agent_in(self._dir.name)

    def tearDown(self):
        self.agent.shutdown()
        self._dir.cleanup()

    def assert_valid(self, payload: dict, note: str) -> None:
        errors = sorted(TELEMETRY.iter_errors(payload), key=str)
        self.assertFalse(errors, f"{note}: {[e.message for e in errors]}")

    def test_valid_when_not_enrolled(self):
        # No credential at all: `expires_at` must be omitted, never null.
        payload = self.agent.health_payload()
        self.assertIsNone(self.agent.enrolment)
        self.assertNotIn("credential", payload)
        self.assert_valid(payload, "unenrolled")

    def test_valid_with_every_condition_severity(self):
        # `status` is a summary (ok | degraded | failing), deliberately not the
        # per-condition vocabulary (info | warning | critical). Publishing the
        # latter is what the schema caught.
        #
        # A fresh Health per case: the agent raises its own conditions for the
        # absent devices in this fixture, and the summary is of the *worst* of
        # them, so an info case would otherwise be measuring those instead.
        from gsu.health import Health

        for severity, expected in (("info", "ok"), ("warning", "degraded"),
                                   ("critical", "failing")):
            with self.subTest(severity=severity):
                self.agent.health = Health()
                self.agent.health.raise_condition("test.condition", severity, "x")
                payload = self.agent.health_payload()
                # health_payload re-evaluates device conditions, which can only
                # add severity — so assert the floor, and the exact value where
                # nothing else could have raised it higher.
                self.assertIn(payload["status"], ("ok", "degraded", "failing"))
                if severity == "critical":
                    self.assertEqual(payload["status"], "failing")
                self.assert_valid(payload, severity)

    def test_the_summary_mapping_itself(self):
        from gsu.health import Health

        health = Health()
        self.assertEqual(health.summary(), "ok")
        health.raise_condition("a", "info", "")
        self.assertEqual(health.summary(), "ok", "info is not a fault")
        health.raise_condition("b", "warning", "")
        self.assertEqual(health.summary(), "degraded")
        health.raise_condition("c", "critical", "")
        self.assertEqual(health.summary(), "failing")

    def test_the_two_vocabularies_do_not_overlap_by_accident(self):
        from gsu.health import SEVERITIES, Health

        summary = set(Health.SUMMARY.values())
        self.assertEqual(summary, {"ok", "degraded", "failing"})
        # If these ever collide, a future edit could pass one where the other
        # is meant and the schema would still accept it.
        self.assertFalse(summary & set(SEVERITIES) - {"ok"})


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.agent = agent_in(self._dir.name)
        self.router = CommandRouter(
            f"cmd/gsu/{STATION}",
            build_handlers(
                self.agent.radio, self.agent.light, self.agent._apply_config,
                self.agent.stream,
            ),
        )

    def tearDown(self):
        self.agent.shutdown()
        self._dir.cleanup()

    def telemetry(self) -> dict:
        sent: list[dict] = []
        self.agent._publish = lambda topic, payload: sent.append(payload) or True
        for _ in range(3):
            self.agent.step(1.0)
        return {payload["kind"]: payload for payload in sent}

    @unittest.expectedFailure
    def test_every_command_in_the_schema_has_a_handler(self):
        """KNOWN GAP against contract 1.0: `events.ack` has no handler here.

        Contract 1.0 added the events channel - the one channel that carries
        facts with no newer version, so the one that is acknowledged. The
        station owes the other half: buffer events across reboots, publish them
        oldest-first in capped batches, and delete up to the acknowledged seq.

        Delete this decorator once that exists; until then the failure is the
        work list and not a regression.
        """
        kinds = {
            option["properties"]["kind"]["const"] for option in COMMANDS["oneOf"]
        }
        self.assertTrue(kinds <= set(self.router.handlers), kinds - set(self.router.handlers))

    def test_the_channel_must_be_slash_separated(self):
        # The failure that broke the reference implementation: subscribed,
        # receiving, and dropping everything.
        self.assertFalse(
            self.router.dispatch(f"cmd:gsu:{STATION}", {"kind": "light.set", "on": True})
        )
        self.assertFalse(
            self.router.dispatch("cmd/gsu/somebody-else", {"kind": "light.set", "on": True})
        )
        self.assertTrue(
            self.router.dispatch(f"cmd/gsu/{STATION}", {"kind": "light.set", "on": True})
        )

    def test_unknown_commands_are_ignored_not_rejected(self):
        self.assertFalse(
            self.router.dispatch(f"cmd/gsu/{STATION}", {"kind": "future.thing", "x": 1})
        )
        self.assertEqual(self.router.ignored, 1)

    def test_transmit_is_not_implemented_anywhere(self):
        self.assertNotIn("radio.transmit", self.router.handlers)
        self.assertFalse(
            self.router.dispatch(f"cmd/gsu/{STATION}", {"kind": "radio.transmit", "on": True})
        )

    def test_each_command_is_observable_in_telemetry(self):
        cases = [
            ({"kind": "radio.tune", "freq_hz": 119_500_000}, "radio", "freq_hz", 119_500_000),
            ({"kind": "radio.auto_squelch", "on": False}, "radio", "auto_squelch", False),
            ({"kind": "radio.squelch", "db": -55.0}, "radio", "threshold_db", -55.0),
            ({"kind": "radio.monitor", "on": True}, "radio", "monitor", True),
            ({"kind": "radio.monitor", "on": False}, "radio", "monitor", False),
            ({"kind": "radio.auto_squelch", "on": True}, "radio", "auto_squelch", True),
            ({"kind": "radio.gain", "gain": 42.1}, "radio", "gain", 42.1),
            ({"kind": "radio.ppm", "ppm": 12}, "radio", "ppm", 12),
            ({"kind": "light.set", "on": True}, "light", "on", True),
            ({"kind": "light.set", "on": False}, "light", "on", False),
        ]
        for command, kind, field, expected in cases:
            self.router.dispatch(f"cmd/gsu/{STATION}", command)
            payload = self.telemetry()[kind]
            self.assertEqual(payload[field], expected, f"{command} was not reported back")

    def test_a_command_for_a_device_that_is_not_fitted_is_not_silently_accepted(self):
        router = CommandRouter(f"cmd/gsu/{STATION}", build_handlers(None, None, None))
        self.assertFalse(router.dispatch(f"cmd/gsu/{STATION}", {"kind": "light.set", "on": True}))
        self.assertEqual(router.applied, 0)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.agent = agent_in(self._dir.name)

    def tearDown(self):
        self.agent.shutdown()
        self._dir.cleanup()

    def test_defaults_are_all_simulated_and_say_so(self):
        for report in self.agent.inventory.report():
            if report.configured:
                self.assertTrue(report.simulated, f"{report.slot} claims to be real")

    def test_a_missing_serial_device_is_configured_but_not_detected(self):
        self.agent.inventory.set_device(
            "weather", "airmar-110wx", {"port": "/dev/does-not-exist", "baud": 4800}
        )
        self.agent.build_devices()
        report = {r.slot: r for r in self.agent.inventory.report()}["weather"]
        self.assertTrue(report.configured)
        self.assertFalse(report.detected)
        self.assertEqual(report.status, "configured_absent")
        self.assertIn("weather", self.agent.inventory.unsourced_streams())

    def test_an_absent_stream_declares_itself_unavailable_on_cadence(self):
        self.agent.inventory.set_device("adsb", "")
        self.agent.build_devices()
        sent: list[dict] = []
        self.agent._publish = lambda topic, payload: sent.append(payload) or True
        for _ in range(3):
            self.agent.step(1.0)

        adsb = [p for p in sent if p.get("kind") == "adsb"]
        self.assertEqual(
            len(adsb), 3,
            "a stream with no source keeps reporting: going quiet is what a "
            "failed station looks like",
        )
        for payload in adsb:
            self.assertIs(payload["available"], False)
            self.assertNotIn(
                "aircraft", payload,
                "an empty aircraft array from a station with no receiver reads "
                "as clear airspace",
            )
            self.assertTrue(payload["unavailable_reason"])
            self.assertLessEqual(len(payload["unavailable_reason"]), 200)
            errors = sorted(TELEMETRY.iter_errors(payload), key=str)
            self.assertFalse(errors, [e.message for e in errors])

    def test_unavailable_is_not_used_for_a_field_the_instrument_cannot_measure(self):
        """`available: false` means no source for the *stream*. A field the
        instrument does not measure is simply omitted — saying the weather
        station is missing when it is working would be worse than either."""
        self.agent.inventory.set_device(
            "weather", "airmar-110wx", {"port": "/dev/does-not-exist"}
        )
        self.agent.build_devices()
        sent: list[dict] = []
        self.agent._publish = lambda topic, payload: sent.append(payload) or True
        self.agent.step(1.0, weather_due=True)
        weather = [p for p in sent if p.get("kind") == "weather"][0]
        self.assertIs(weather["available"], False)   # no source at all

        from gsu.devices.airmar import AirmarWeather
        from gsu.devices.nmea import checksum

        body = "WIMWV,225.0,T,12.5,N,A"

        class _Source:
            data = f"${body}*{checksum(body):02X}\r\n".encode()

            def read(self):
                data, self.data = self.data, b""
                return data

            def close(self):
                pass

        driver = AirmarWeather(_Source(), humidity_module=False)
        payload = driver.read(1.0).to_payload()
        self.assertNotIn("available", payload, "a working instrument is available")
        self.assertNotIn("humidity_pct", payload, "unmeasured fields are omitted")

    def test_unavailable_payloads_for_every_stream_match_the_schema(self):
        for slot in ("adsb", "radio", "weather", "power", "light"):
            self.agent.inventory.set_device(slot, "")
        self.agent.build_devices()
        sent: list[dict] = []
        self.agent._publish = lambda topic, payload: sent.append(payload) or True
        self.agent.step(1.0, weather_due=True)
        kinds = {p["kind"] for p in sent if p.get("kind") != "health"}
        self.assertEqual(kinds, {"adsb", "radio", "weather", "power", "light"})
        for payload in sent:
            if payload["kind"] == "health":
                continue
            self.assertIs(payload["available"], False)
            errors = sorted(TELEMETRY.iter_errors(payload), key=str)
            self.assertFalse(errors, f"{payload['kind']}: {[e.message for e in errors]}")

    def test_a_stalled_device_reads_differently_from_one_never_fitted(self):
        never = self.agent.unavailable_payload("adsb", {})["unavailable_reason"]
        self.assertIn("no ADS-B receiver", never)
        self.agent.inventory.set_device(
            "weather", "airmar-110wx", {"port": "/dev/does-not-exist"}
        )
        self.agent.build_devices()
        configured = self.agent.unavailable_payload("weather")["unavailable_reason"]
        # A device that was chosen and is not answering names itself and the
        # fault, so an operator knows to check a cable rather than an order.
        self.assertIn("Airmar 110WX", configured)
        self.assertIn("/dev/does-not-exist", configured)
        self.assertNotIn("no weather station connected", configured)

    def test_two_slots_cannot_share_one_tuner(self):
        self.agent.inventory.set_device(
            "radio", "rtlsdr-airband", {}, resource="rtlsdr:00000001"
        )
        self.agent.inventory.set_device(
            "adsb", "rtlsdr-dump1090", {}, resource="rtlsdr:00000001"
        )
        conflicts = self.agent.inventory.conflicts()
        self.assertTrue(any("assigned to" in c for c in conflicts), conflicts)

    def test_the_registry_declares_capabilities_for_every_device(self):
        for device in registry.REGISTRY:
            self.assertIn(device.slot, registry.SLOTS)
            self.assertIn(device.connection, registry.CONNECTIONS)
            if device.slot != "camera":
                self.assertTrue(device.provides, f"{device.id} declares no capabilities")


class AltitudeCorrectionWiringTests(unittest.TestCase):
    """The barometer reaching the ADS-B stream, at the level where the two
    devices are actually joined together.

    `test_adsb_altitude.py` covers the arithmetic and the refusals. What is
    only testable here is the wiring: that the pressure the console is shown is
    the pressure the correction used, that switching it on at runtime takes
    effect without a rebuild, and that health says why when it is idle.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.agent = agent_in(self._dir.name)
        self.agent._publish = lambda topic, payload: True

    def tearDown(self):
        self.agent.shutdown()
        self._dir.cleanup()

    def _run(self, ticks: int = 6) -> None:
        for index in range(ticks):
            self.agent.step(1.0, weather_due=index % 2 == 0)

    def test_off_by_default_and_health_stays_quiet_about_it(self):
        self.assertFalse(self.agent.site.adsb_baro_correction)
        self._run()
        state = self.agent.health_payload()["adsb_altitude_correction"]
        self.assertFalse(state["enabled"])
        self.assertFalse(state["active"])
        ids = {c["id"] for c in self.agent.health.to_list()}
        self.assertNotIn(
            "adsb.altitude_correction", ids,
            "a setting nobody switched on is not a condition",
        )

    def test_switching_it_on_at_runtime_corrects_without_a_rebuild(self):
        self.agent.site.apply(
            {"adsb_baro_correction": True, "elevation_m": 120.0}, version=4
        )
        self._run()
        state = self.agent.health_payload()["adsb_altitude_correction"]
        self.assertTrue(state["active"], state["reason"])
        # The station's own barometer, and the sea-level figure derived from
        # it. If these were the same number the reduction has been dropped.
        self.assertIsNotNone(state["station_pressure_hpa"])
        self.assertNotEqual(
            state["station_pressure_hpa"], state["sea_level_pressure_hpa"]
        )

        contacts = self.agent.adsb.poll(1.0)
        corrected = [
            c for c in contacts
            if c.altitude_type == "pressure" and c.altitude_corrected_m is not None
        ]
        self.assertTrue(corrected, "nothing was corrected")
        for contact in corrected:
            # Both travel. The correction is a second reading of the same
            # aircraft, never a replacement for what the receiver said.
            self.assertIsNotNone(contact.altitude)
            self.assertNotEqual(contact.altitude, contact.altitude_corrected_m)

    def test_the_pressure_used_is_the_pressure_published(self):
        self.agent.site.apply(
            {"adsb_baro_correction": True, "elevation_m": 0.0}, version=4
        )
        sent: list[dict] = []
        self.agent._publish = lambda topic, payload: sent.append(payload) or True
        self._run()
        weather = [p for p in sent if p.get("kind") == "weather"][-1]
        state = self.agent.health_payload()["adsb_altitude_correction"]
        # At zero elevation the reduction is the identity, so the two figures
        # must agree exactly - which is the cheapest way to prove there is not
        # a second, separately-read barometer behind the correction.
        self.assertEqual(state["station_pressure_hpa"], weather["pressure_hpa"])
        self.assertEqual(state["sea_level_pressure_hpa"], weather["pressure_hpa"])

    def test_on_without_an_elevation_is_reported_rather_than_guessed(self):
        self.agent.site.apply({"adsb_baro_correction": True}, version=4)
        self.assertIsNone(self.agent.site.elevation_m)
        self._run()
        payload = self.agent.health_payload()
        state = payload["adsb_altitude_correction"]
        self.assertTrue(state["enabled"])
        self.assertFalse(state["active"])
        self.assertIn("elevation", state["reason"])
        condition = next(
            c for c in payload["conditions"] if c["id"] == "adsb.altitude_correction"
        )
        # Informational, not a warning: the station is doing its job and one
        # optional refinement is unavailable. `Health.SUMMARY` maps info to ok,
        # so a setting cannot degrade a station.
        self.assertEqual(condition["severity"], "info")
        self.assertEqual(payload["status"], "ok")

        contacts = self.agent.adsb.poll(1.0)
        self.assertTrue(contacts)
        self.assertTrue(all(c.altitude_corrected_m is None for c in contacts))

    def test_losing_the_weather_station_stops_the_correction(self):
        self.agent.site.apply(
            {"adsb_baro_correction": True, "elevation_m": 120.0}, version=4
        )
        self._run()
        self.assertTrue(self.agent.health_payload()["adsb_altitude_correction"]["active"])

        self.agent.inventory.set_device("weather", "")
        self.agent.build_devices()
        self._run()
        state = self.agent.health_payload()["adsb_altitude_correction"]
        self.assertFalse(
            state["active"],
            "a departed weather head must not leave its last pressure behind",
        )
        self.assertIn("no barometric reading", state["reason"])

    def test_the_correction_never_moves_the_proximity_alert(self):
        # Alerting is the one thing the station must still get right with the
        # platform unreachable; hanging it on a second sensor would mean a dead
        # barometer quietly shifts the threshold.
        self.agent.site.apply(
            {"adsb_baro_correction": True, "elevation_m": 120.0}, version=4
        )
        self._run()
        for contact in self.agent.adsb.poll(1.0):
            expected = (
                contact.range_km < self.agent.site.alert_range_km
                and contact.altitude is not None
                and contact.altitude < self.agent.site.alert_altitude_m
            )
            self.assertIs(contact.alert, expected)


class ReattachTests(unittest.TestCase):
    """A credential issued by another process must be picked up without a
    restart. Nobody is on site to provide one."""

    def test_a_credential_written_by_another_process_is_picked_up(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = agent_in(directory)
            try:
                self.assertIsNone(agent.enrolment)
                store = CredentialStore(Path(directory) / "credential.json")
                store.save(Enrolment.from_response(CredentialTests().response()))
                self.assertTrue(agent.reload_credential_if_changed())
                self.assertIsNotNone(agent.enrolment)
                self.assertEqual(agent.enrolment.station_id, STATION)
                # And it does not reattach again for the same file.
                self.assertFalse(agent.reload_credential_if_changed())
            finally:
                agent.shutdown()


class CredentialTests(unittest.TestCase):
    def response(self, secret="s3cret", hours=48, renew_hours=24) -> dict:
        now = datetime.now(UTC)
        return {
            "station_id": STATION,
            "credential": {
                "type": "bearer", "secret": secret,
                "expires_at": (now + timedelta(hours=hours)).isoformat(),
                "renew_after": (now + timedelta(hours=renew_hours)).isoformat(),
            },
            "broker": {
                "url": "redis://broker:6379/0",
                "username": f"gsu:{STATION}",
                "telemetry_topic": f"gsu/{STATION}/telemetry",
                "audio_topic": f"gsu/{STATION}/audio",
                "command_topic": f"cmd/gsu/{STATION}",
            },
            "station": {
                "name": "Test", "timezone": "Pacific/Auckland",
                "latitude": -43.5, "longitude": 172.6,
            },
            "config_version": 3,
        }

    def test_stored_credential_is_not_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.json"
            store = CredentialStore(path)
            store.save(Enrolment.from_response(self.response()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.load().credential.secret, "s3cret")

    def test_renewal_failure_becomes_a_health_condition(self):
        class Failing:
            def renew(self, secret):
                raise RuntimeError("no route to host")

        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / "credential.json")
            enrolment = Enrolment.from_response(self.response(hours=48, renew_hours=-1))
            health = Health()
            renewer = Renewer(Failing(), store, enrolment, health, poll_seconds=0.01)
            renewer.tick()
            conditions = {c.id: c for c in health.active()}
            self.assertIn("credential.renewal_failing", conditions)
            self.assertEqual(conditions["credential.renewal_failing"].severity, "warning")

    def test_renewal_failure_close_to_expiry_is_critical(self):
        class Failing:
            def renew(self, secret):
                raise RuntimeError("no route to host")

        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / "credential.json")
            enrolment = Enrolment.from_response(self.response(hours=2, renew_hours=-1))
            health = Health()
            renewer = Renewer(Failing(), store, enrolment, health, poll_seconds=0.01)
            renewer.tick()
            self.assertEqual(
                {c.id: c for c in health.active()}["credential.renewal_failing"].severity,
                "critical",
            )

    def test_a_revoked_credential_is_critical_and_not_fatal(self):
        """Something outside the station's control can revoke it mid-flight —
        an admin, or another box claiming its enrolment. The station must keep
        sensing and say so, never exit."""
        from gsu.enrolment import EnrolmentError

        class Rejecting:
            def renew(self, secret):
                raise EnrolmentError("The platform refused: 401.", status=401)

        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / "credential.json")
            enrolment = Enrolment.from_response(self.response(hours=48, renew_hours=-1))
            health = Health()
            renewer = Renewer(Rejecting(), store, enrolment, health, poll_seconds=0.01)
            renewer.tick()  # must not raise
            conditions = {c.id: c for c in health.active()}
            self.assertIn("credential.revoked", conditions)
            self.assertEqual(conditions["credential.revoked"].severity, "critical")
            self.assertTrue(renewer.revoked)

    def test_a_renewal_naming_another_station_is_refused(self):
        first = Enrolment.from_response(self.response())
        other = self.response()
        other["station_id"] = "00000000-0000-0000-0000-000000000000"
        with self.assertRaises(ValueError):
            first.with_credential(Enrolment.from_response(other))


class ClockTests(unittest.TestCase):
    def test_enrolling_with_an_implausible_clock_is_refused(self):
        from gsu import clock
        from gsu.enrolment import EnrolmentClient

        client = EnrolmentClient("http://127.0.0.1:1")
        original = clock.now
        clock.now = lambda: datetime(1970, 1, 1, tzinfo=UTC)
        try:
            with self.assertRaises(clock.ClockImplausible):
                client.claim("XXXX-XXXX-XXXX", {})
        finally:
            clock.now = original


if __name__ == "__main__":
    unittest.main()


class DemoSensorStampTests(unittest.TestCase):
    """Demo is a property of the sensor, not of the station.

    A bench box with a live camera and a demo weather head is the normal way to
    develop against one, and the station-wide flag this replaces had to be
    wrong about one half of it.
    """

    def agent_with(self, **slots):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        agent = Agent(AgentConfig(
            home=Path(directory.name), setup_enabled=False, single_instance=False, demo=True))
        self.addCleanup(agent.shutdown)
        for slot, type_id in slots.items():
            agent.inventory.set_device(slot, type_id, {})
        agent.build_devices()
        return agent

    def test_a_demo_sensors_stream_says_so(self):
        agent = self.agent_with(weather="simulated-weather")
        stamped = agent._stamp_simulated({"kind": "weather", "wind_kt": 4})
        self.assertIs(stamped["simulated"], True)

    def test_a_real_sensors_stream_carries_no_flag(self):
        # Absent means real. Adding `simulated: false` everywhere would be
        # noise on every frame from every real station.
        agent = self.agent_with(weather="airmar-110wx")
        stamped = agent._stamp_simulated({"kind": "weather", "wind_kt": 4})
        self.assertNotIn("simulated", stamped)

    def test_one_station_can_be_half_real(self):
        # The whole point of moving the flag off the station.
        agent = self.agent_with(
            weather="simulated-weather", camera="raspberry-pi-csi",
        )
        self.assertIs(
            agent._stamp_simulated({"kind": "weather"}).get("simulated"), True)
        self.assertNotIn(
            "simulated", agent._stamp_simulated({"kind": "video"}))

    def test_it_never_overwrites_a_flag_the_payload_already_set(self):
        # An aircraft's own `simulated` means a test target injected by a real
        # receiver — a different statement, and not this function's to make.
        agent = self.agent_with(adsb="uavionix-ping-rx-pro")
        payload = {"kind": "adsb", "simulated": False, "aircraft": []}
        self.assertIs(agent._stamp_simulated(payload)["simulated"], False)

    def test_every_slot_offers_a_demo_sensor(self):
        # An owner requirement: each slot has one in the list, so any station
        # can be brought up end to end with no hardware at all.
        for slot in registry.SLOTS:
            demo = [d for d in registry.by_slot(slot) if d.simulated]
            self.assertTrue(demo, f"{slot} has no demo sensor")
            self.assertTrue(
                any(d.label.startswith("Demo") for d in demo),
                f"{slot}'s demo sensor is not labelled Demo",
            )


class PowerSourcesTests(unittest.TestCase):
    """Four sources, and the difference between absent and zero.

    A site with no grid connection and a site whose grid has failed are
    completely different situations, and `mains_w: 0` describes both. So a
    source that is not fitted is omitted entirely — the same rule a weather
    head with no humidity module follows — and a source that is fitted reports,
    including reporting zero, which is then a measurement.
    """

    def payload(self, **kwargs):
        from gsu.sensors import PowerReading
        base = dict(soc_pct=80.0, battery_v=49.2, pv_w=400.0, load_w=120.0,
                    runtime_h=None)
        return PowerReading(**{**base, **kwargs}).to_payload()

    def test_a_site_with_no_grid_says_nothing_about_mains(self):
        payload = self.payload()
        self.assertNotIn("mains_w", payload)
        self.assertNotIn("mains_present", payload)

    def test_a_fitted_input_with_no_power_reports_zero_and_says_so(self):
        # The fault case: there IS a grid connection and it is dead. A console
        # cannot tell this from the absent case unless the station distinguishes
        # them, and every off-grid station would look like it had lost power.
        payload = self.payload(mains_present=False, mains_w=0.0)
        self.assertIs(payload["mains_present"], False)
        self.assertEqual(payload["mains_w"], 0.0)

    def test_a_generator_running_and_delivering_nothing_is_reportable(self):
        # It has started and failed to take the load, which is exactly the
        # state somebody needs to be told about — and is not the same as off.
        payload = self.payload(generator_running=True, generator_w=0.0)
        self.assertIs(payload["generator_running"], True)
        self.assertEqual(payload["generator_w"], 0.0)

    def test_battery_direction_is_sent_not_inferred(self):
        # With four sources a consumer cannot derive it without knowing about
        # conversion losses and which source is carrying the load.
        self.assertEqual(self.payload(battery_w=-240.0)["battery_w"], -240.0)
        self.assertEqual(self.payload(battery_w=310.0)["battery_w"], 310.0)

    def test_every_payload_validates(self):
        for extra in (
            {},
            {"mains_present": True, "mains_w": 900.0},
            {"generator_running": True, "generator_w": 1400.0},
            {"mains_present": False, "mains_w": 0.0,
             "generator_running": True, "generator_w": 1400.0},
        ):
            TELEMETRY.validate(self.payload(**extra))


class SimulatedPowerHandoverTests(unittest.TestCase):
    """The demo box has to show the sequence an operator most needs to read.

    Mains drops, the battery carries the load, the state of charge falls, the
    generator starts and takes over. A static demo never shows that, and it is
    the whole reason the console's flow display exists.
    """

    def source(self):
        from gsu.sensors.simulated import SimulatedPower
        return SimulatedPower(seed=7)

    def test_it_reports_all_four_sources(self):
        payload = self.source().read(1.0).to_payload()
        for field in ("pv_w", "load_w", "battery_w", "mains_w",
                      "mains_present", "generator_w", "generator_running"):
            self.assertIn(field, payload, field)

    def test_the_generator_does_not_hunt_around_one_threshold(self):
        # A single threshold makes a generator start and stop repeatedly around
        # it, which is hard on the machine and reads as a fault on a graph.
        from gsu.sensors.simulated import SimulatedPower
        self.assertGreater(SimulatedPower.GEN_STOP_SOC,
                           SimulatedPower.GEN_START_SOC)

    def test_the_generator_starts_when_the_battery_gets_low(self):
        power = self.source()
        power.soc = SimulatedPowerHandoverTests._start_soc() - 1
        reading = power.read(1.0)
        self.assertIs(reading.generator_running, True)

    def test_and_stops_once_it_has_charged_the_battery_back(self):
        power = self.source()
        power._gen_running = True
        power.soc = SimulatedPowerHandoverTests._stop_soc() + 1
        self.assertIs(power.read(1.0).generator_running, False)

    @staticmethod
    def _start_soc():
        from gsu.sensors.simulated import SimulatedPower
        return SimulatedPower.GEN_START_SOC

    @staticmethod
    def _stop_soc():
        from gsu.sensors.simulated import SimulatedPower
        return SimulatedPower.GEN_STOP_SOC

    def test_the_battery_discharges_when_nothing_else_covers_the_load(self):
        power = self.source()
        power._mains_down_for = 100.0     # grid out
        power._gen_running = False
        # Solar is (sin(t/240) + 1) / 2, so it is dark at t/240 = 3π/2. The
        # read below advances t by dt first, hence the offset.
        power._t = 240 * (3 * math.pi / 2) - 1.0
        reading = power.read(1.0)
        self.assertLess(reading.battery_w, 0)
        self.assertIsNotNone(reading.runtime_h)

    def test_a_charging_battery_reports_no_runtime(self):
        # "Indefinitely" is not a number the console should special-case.
        power = self.source()
        power._gen_running = True
        reading = power.read(1.0)
        self.assertGreater(reading.battery_w, 0)
        self.assertIsNone(reading.runtime_h)


class SimulatedPowerSizingTests(unittest.TestCase):
    """A demo box can be made to look like the site it stands in for.

    The array, the bank, the generator and the peak load are all parameters,
    because a demo of a 200 W repeater and a demo of a 5 kW compound are
    different demos and hardcoding one makes the other a lie.
    """

    def source(self, **kwargs):
        from gsu.sensors.simulated import SimulatedPower
        return SimulatedPower(seed=11, **kwargs)

    def test_the_array_sets_what_solar_can_deliver(self):
        small = self.source(solar_w=100)
        big = self.source(solar_w=4000)
        # Same seed, same point in the day cycle, so the only difference is
        # the array.
        self.assertLess(small.read(1.0).pv_w, big.read(1.0).pv_w)

    def test_the_load_never_exceeds_the_sites_peak(self):
        power = self.source(max_load_w=250)
        for _ in range(50):
            self.assertLessEqual(power.read(1.0).load_w, 250.0)

    def test_a_floodlight_cannot_push_a_site_past_its_peak(self):
        power = self.source(max_load_w=250)
        self.assertLessEqual(power.read(1.0, extra_load_w=9000).load_w, 250.0)

    def test_a_bigger_bank_moves_more_slowly(self):
        # The whole reason capacity is a parameter: a demo of a large site
        # should not have its battery visibly swinging every few seconds.
        small = self.source(battery_wh=100, solar_w=0, max_load_w=400)
        large = self.source(battery_wh=10000, solar_w=0, max_load_w=400)
        for source in (small, large):
            source._mains_down_for = 500.0     # nothing but the battery
            source.soc = 80.0
        for _ in range(20):
            small.read(1.0)
            large.read(1.0)
        self.assertLess(small.soc, large.soc)

    def test_a_zero_capacity_cannot_produce_a_nan(self):
        # It is a divisor and a form is a place people type zero.
        reading = self.source(battery_wh=0).read(1.0)
        self.assertFalse(math.isnan(reading.soc_pct))

    def test_a_site_with_no_generator_reports_none_at_all(self):
        # Absent and idle are different statements, and a diagram showing a
        # generator that never starts is inventing hardware.
        payload = self.source(generator=False).read(1.0).to_payload()
        self.assertNotIn("generator_w", payload)
        self.assertNotIn("generator_running", payload)

    def test_a_site_with_one_reports_it_even_while_stopped(self):
        payload = self.source(generator=True).read(1.0).to_payload()
        self.assertIn("generator_running", payload)

    def test_the_generator_carries_the_load_and_the_charge(self):
        # Neither grid source has a size: both are specified to carry the
        # site, so what the generator delivers is set by demand, not rating.
        power = self.source(solar=False, max_load_w=400, max_charge_w=1500)
        power._mains_down_for = 500.0
        power.soc = 10.0
        reading = power.read(1.0)
        self.assertAlmostEqual(reading.battery_w, 1500.0, places=3)
        self.assertAlmostEqual(
            reading.generator_w, reading.load_w + 1500.0, places=3,
        )

    def test_a_grid_source_charges_the_bank_at_its_full_rate(self):
        # The rule, and the reason the charge rate is the only size that
        # matters now: with mains or the generator up, what limits a recharge
        # is what the bank will accept, never what the supply can deliver.
        for off in ({"mains": True, "generator": False},
                    {"mains": False, "generator": True}):
            power = self.source(solar=False, max_charge_w=450, **off)
            power.soc = 30.0  # Low enough that the generator starts too.
            reading = power.read(1.0)
            self.assertAlmostEqual(reading.battery_w, 450.0, places=3, msg=off)

    def test_a_full_bank_takes_nothing_and_the_array_backs_off(self):
        # Curtailment, not spillage. Reporting the array's potential as though
        # it were flowing would put watts on the diagram that go nowhere.
        power = self.source(solar_w=2000, max_load_w=300)
        power.soc = 100.0
        reading = power.read(1.0)
        self.assertEqual(reading.battery_w, 0.0)
        self.assertLessEqual(reading.pv_w, reading.load_w + 1e-6)

    def test_on_solar_alone_the_bank_only_gets_the_surplus(self):
        # No grid source, so the charge rate is a ceiling rather than a target.
        power = self.source(mains=False, generator=False, solar_w=2000,
                            max_load_w=300, max_charge_w=100_000)
        power.soc = 50.0
        reading = power.read(1.0)
        self.assertAlmostEqual(
            reading.battery_w, reading.pv_w - reading.load_w, places=3,
        )

    def test_every_watt_is_accounted_for(self):
        # The console draws a conserved quantity: sources in must equal the
        # load plus whatever the battery takes. A shape that does not balance
        # is a diagram with power appearing from nowhere.
        shapes = (
            {}, {"solar": False}, {"mains": False}, {"generator": False},
            {"battery": False}, {"mains": False, "generator": False},
            {"max_charge_w": 0}, {"max_charge_w": 5000},
        )
        for shape in shapes:
            for soc in (10.0, 60.0, 100.0):
                power = self.source(**shape)
                power.soc = soc
                r = power.read(1.0)
                supplied = r.pv_w + (r.mains_w or 0.0) + (r.generator_w or 0.0)
                self.assertAlmostEqual(
                    supplied, r.load_w + r.battery_w, places=6,
                    msg=f"{shape} at {soc}%",
                )

    def test_neither_grid_source_has_a_size(self):
        # Both are specified to carry the site, so the interesting number is
        # what the bank will accept from them - which belongs to the battery.
        from gsu.devices import registry
        params = {p.name: p for p in registry.get("simulated-power").parameters}
        self.assertNotIn("mains_w", params)
        self.assertNotIn("generator_w", params)

    def test_the_registry_offers_a_switch_and_a_size_per_source(self):
        from gsu.devices import registry
        params = {p.name: p for p in registry.get("simulated-power").parameters}
        # A switch per source: "this site has no generator" is a fact about
        # the site, not something to say by setting an output to zero.
        for name in ("solar", "battery", "mains", "generator"):
            self.assertEqual(params[name].type, "bool", name)
        for name in ("solar_w", "battery_wh", "max_charge_w", "max_load_w"):
            self.assertEqual(params[name].type, "number", name)

    def test_a_source_can_be_switched_off_entirely(self):
        for off, absent in (
            ({"mains": False}, ("mains_w", "mains_present")),
            ({"generator": False}, ("generator_w", "generator_running")),
        ):
            payload = self.source(**off).read(1.0).to_payload()
            for field in absent:
                self.assertNotIn(field, payload, off)

    def test_a_site_with_no_solar_generates_none(self):
        self.assertEqual(self.source(solar=False).read(1.0).pv_w, 0.0)

    def test_a_site_with_no_bank_neither_charges_nor_discharges(self):
        # Nothing to absorb a surplus or cover a shortfall: it runs on what is
        # coming in.
        power = self.source(battery=False, solar_w=4000)
        self.assertEqual(power.read(1.0).battery_w, 0.0)


class LocalStreamViewerTests(unittest.TestCase):
    """The setup page watching the same encoder the platform watches.

    The camera is a single device with a single owner. Serving the setup page
    by starting a second encoder would be the two-readers bug that removed the
    snapshot channel, arriving again by a different door — so these are mostly
    tests that nothing forks.
    """

    def tee(self):
        from gsu.transport.stream import NullUplink, TeeUplink
        return TeeUplink(NullUplink())

    def test_a_viewer_joining_late_still_gets_the_init_segment(self):
        # The encoder emits one per session and will not produce another until
        # its parameters change. Without it a decoder has nothing to decode
        # against and the element sits black for as long as the session lasts.
        tee = self.tee()
        tee.begin("h264", b"INIT")
        from gsu.transport.stream import LocalViewer
        viewer = LocalViewer()
        tee.add(viewer)
        self.assertEqual(viewer.read(timeout=0.1), b"INIT")

    def test_a_viewer_waits_for_a_keyframe_before_any_picture(self):
        # A fragment that is not one decodes against a reference frame this
        # viewer never received. The alternative to waiting is a few hundred
        # milliseconds of macroblock soup, which reads as a broken camera.
        from gsu.transport.stream import LocalViewer
        tee = self.tee()
        viewer = LocalViewer()
        tee.add(viewer)
        tee.begin("h264", b"INIT")
        self.assertEqual(viewer.read(timeout=0.1), b"INIT")
        tee.send(b"mid-gop", keyframe=False)
        self.assertIsNone(viewer.read(timeout=0.05))
        tee.send(b"keyframe", keyframe=True)
        self.assertEqual(viewer.read(timeout=0.1), b"keyframe")

    def test_a_stalled_viewer_is_dropped_not_queued(self):
        # The same rule as everywhere else on this path: a buffered second of
        # 1080p is several megabytes of a picture that is already out of date.
        from gsu.transport.stream import LocalViewer
        tee = self.tee()
        viewer = LocalViewer()
        tee.add(viewer)
        tee.begin("h264", b"INIT")
        tee.send(b"key", keyframe=True)
        for i in range(LocalViewer.DEPTH * 3):
            tee.send(f"f{i}".encode(), keyframe=False)
        self.assertGreater(viewer.dropped, 0)
        self.assertLessEqual(len(viewer._queue), LocalViewer.DEPTH)

    def test_a_browser_falling_behind_is_not_the_platform_dropping_frames(self):
        # The primary's answer is the session's answer. A slow tab must not be
        # reported as the satellite link losing video.
        from gsu.transport.stream import LocalViewer
        tee = self.tee()
        viewer = LocalViewer()
        tee.add(viewer)
        tee.begin("h264", b"INIT")
        tee.send(b"key", keyframe=True)
        for i in range(LocalViewer.DEPTH * 3):
            tee.send(f"f{i}".encode(), keyframe=False)
        self.assertEqual(tee.primary.dropped, 0)

    def test_removing_a_viewer_closes_it(self):
        from gsu.transport.stream import LocalViewer
        tee = self.tee()
        viewer = LocalViewer()
        tee.add(viewer)
        self.assertEqual(tee.local_viewers, 1)
        tee.remove(viewer)
        self.assertEqual(tee.local_viewers, 0)
        self.assertTrue(viewer.closed)

    def test_the_platform_still_gets_everything(self):
        # The whole point of a tee: local viewers are additional, never
        # instead. Both the init segment and every fragment reach the primary.
        from gsu.transport.stream import LocalViewer, StreamUplink

        class Recording(StreamUplink):
            name = "recording"

            def __init__(self):
                super().__init__()
                self.log = []

            def open(self): return True
            def begin(self, codec, init): self.log.append(("begin", init)); return True
            def send(self, fragment, keyframe): self.log.append(("send", fragment)); return True
            def close(self): self.log.append(("close", None))

        from gsu.transport.stream import TeeUplink
        primary = Recording()
        tee = TeeUplink(primary)
        self.assertTrue(tee.open())
        tee.add(LocalViewer())
        tee.begin("h264", b"INIT")
        tee.send(b"key", keyframe=True)
        self.assertEqual(primary.log, [("begin", b"INIT"), ("send", b"key")])

    def test_the_setup_page_works_when_the_platform_link_is_down(self):
        # The moment somebody most needs to aim a camera is the moment the box
        # is not talking to the platform. A local preview that requires a
        # working uplink is a preview that is missing whenever it is wanted.
        from gsu.transport.stream import LocalViewer, StreamUplink, TeeUplink

        class Refusing(StreamUplink):
            name = "refusing"

            def open(self):
                self.reason = "no route to the platform"
                return False

            def begin(self, codec, init): raise AssertionError("not open")
            def send(self, fragment, keyframe): raise AssertionError("not open")
            def close(self): pass

        tee = TeeUplink(Refusing(), require_primary=False)
        self.assertTrue(tee.open(), "a local session must survive this")
        viewer = LocalViewer()
        tee.add(viewer)
        tee.begin("h264", b"INIT")
        tee.send(b"key", keyframe=True)
        self.assertEqual(viewer.read(timeout=0.1), b"INIT")
        self.assertEqual(viewer.read(timeout=0.1), b"key")
        # And says so, rather than reporting a stream going somewhere.
        self.assertFalse(tee.stats()["primary_open"])
        self.assertIn("nowhere off-box", tee.describe())

    def test_a_platform_stream_still_fails_when_it_cannot_send(self):
        # The other half. An encoder running with nowhere to send is the
        # expensive mistake this whole path exists to prevent, and a stream the
        # platform asked for has nobody local to justify it.
        from gsu.transport.stream import StreamUplink, TeeUplink

        class Refusing(StreamUplink):
            name = "refusing"

            def open(self): return False
            def begin(self, codec, init): return False
            def send(self, fragment, keyframe): return False
            def close(self): pass

        self.assertFalse(TeeUplink(Refusing(), require_primary=True).open())

    def test_the_platform_can_join_a_session_the_setup_page_started(self):
        # The encoder is already running and the init segment is held, so there
        # is nothing to restart — only a connection to make.
        from gsu.transport.stream import StreamUplink, TeeUplink

        class Late(StreamUplink):
            name = "late"

            def __init__(self):
                super().__init__()
                self.available = False
                self.log = []

            def open(self): return self.available
            def begin(self, codec, init): self.log.append(init); return True
            def send(self, fragment, keyframe): self.log.append(fragment); return True
            def close(self): pass

        primary = Late()
        tee = TeeUplink(primary, require_primary=False)
        self.assertTrue(tee.open())
        tee.begin("h264", b"INIT")
        tee.send(b"early", keyframe=True)
        self.assertEqual(primary.log, [], "nothing should have gone out yet")

        primary.available = True
        self.assertTrue(tee.open_primary())
        # Handed the init segment it missed, so it can decode what follows.
        self.assertEqual(primary.log, [b"INIT"])
        tee.send(b"later", keyframe=True)
        self.assertEqual(primary.log, [b"INIT", b"later"])

    def test_the_uplink_says_who_is_watching(self):
        from gsu.transport.stream import LocalViewer
        tee = self.tee()
        self.assertNotIn("setup page", tee.describe())
        tee.add(LocalViewer())
        self.assertIn("setup page", tee.describe())
        self.assertEqual(tee.stats()["local_viewers"], 1)


class RadioAudioLatencyTests(unittest.TestCase):
    """Audio is a stream; everything else on the sensing loop is a reading.

    At the sweep's one second the receiver was handed a whole second to
    demodulate at once — a second of latency before a syllable could leave the
    box, and the console's prebuffer then sized itself from the chunk it
    received, costing another 1.25 on top.
    """

    def test_the_sub_tick_is_well_inside_what_anybody_hears(self):
        from gsu.agent import AUDIO_TICK_S
        self.assertLessEqual(AUDIO_TICK_S, 0.2)
        # And not so small that the per-chunk overhead — a payload, a base64
        # encode, a broker publish — costs more than the latency it saves.
        self.assertGreaterEqual(AUDIO_TICK_S, 0.05)

    def test_a_chunk_is_a_sub_tick_of_audio_not_a_whole_second(self):
        # What actually reaches the console, in samples. The prebuffer over
        # there is 1.25x whatever arrives, so this number is most of the
        # latency budget.
        from gsu.agent import AUDIO_TICK_S
        from gsu.radio.audio import AUDIO_RATE
        self.assertLessEqual(AUDIO_TICK_S * AUDIO_RATE, 0.2 * AUDIO_RATE)

    def test_only_one_of_the_two_callers_reads_the_front_end(self):
        # The front end is a single device with a single reader. A sub-tick
        # having read it must stop the sweep reading it again, or the receiver
        # is asked for two seconds of samples in every one.
        #
        # Against the real Agent.step, not a stand-in for it: the whole claim
        # is about what that method does, and a stub of it would pass while it
        # drifted.
        reads: list[float] = []

        class Counting:
            freq_hz = 121_500_000

            def tick(self, dt):
                reads.append(dt)
                return {"freq_hz": self.freq_hz}, None

        with tempfile.TemporaryDirectory() as directory:
            agent = agent_in(directory)
            agent._publish = lambda topic, payload: True
            agent.radio = Counting()
            agent._radio_telemetry = None
            agent._radio_pumped = False

            agent._pump_radio(0.125)
            self.assertEqual(reads, [0.125])
            agent.step(1.0, weather_due=False, health_due=False)
            self.assertEqual(reads, [0.125], "the sweep read the front end again")
            # And with nothing having pumped, the sweep reads it itself: `step`
            # has to stand on its own, because it is what a single-shot run and
            # every other test in this file drive.
            agent.step(1.0, weather_due=False, health_due=False)
            self.assertEqual(reads, [0.125, 1.0])

    def test_the_sweep_publishes_the_newest_reading_the_pump_left(self):
        class Counting:
            freq_hz = 121_500_000

            def __init__(self):
                self.n = 0

            def tick(self, dt):
                self.n += 1
                return {"freq_hz": self.freq_hz, "n": self.n}, None

        with tempfile.TemporaryDirectory() as directory:
            sent: list[dict] = []
            agent = agent_in(directory)
            agent._publish = lambda topic, payload: sent.append(payload) or True
            agent.radio = Counting()
            agent._radio_telemetry = None
            agent._radio_pumped = False
            for _ in range(4):
                agent._pump_radio(0.125)
            self.assertEqual(agent._radio_telemetry["n"], 4)
            agent.step(1.0, weather_due=False, health_due=False)
            self.assertTrue(any(p.get("n") == 4 for p in sent),
                            "the newest reading was not the one published")
            # Consumed, so a sweep with no pump behind it does not republish a
            # stale reading as though it were new.
            self.assertIsNone(agent._radio_telemetry)


class StreamReportingHonestyTests(unittest.TestCase):
    """A remux applies none of the settings this station computed.

    The camera decided its resolution, rate and bitrate before the station
    connected and `-c copy` changes none of them. Telemetry has reported
    `requested` and `delivered` separately for a while; the log had not, and
    said "1920x1080 at 30 fps, 3000 kbit/s target" for a real camera sending
    1080p at 5.
    """

    def test_an_encoder_says_it_applies_its_settings(self):
        from gsu.camera.h264 import ProcessEncoder
        self.assertTrue(ProcessEncoder.enforces_settings)

    def test_a_remux_says_it_does_not(self):
        from gsu.camera.rtsp import RtspRemuxSource
        self.assertFalse(RtspRemuxSource.enforces_settings)

    def test_the_flag_survives_a_source_that_never_heard_of_it(self):
        # Read with a default, because a third-party or future source that
        # does not define it should be assumed to mean what it says rather
        # than silently reported as a remux.
        class Bare:
            pass
        self.assertTrue(getattr(Bare(), "enforces_settings", True))


class RelayTransportTests(unittest.TestCase):
    """The 443 broker transport.

    A message relay, not a Redis proxy: what goes over the socket is
    `{topic, payload}`, and the platform decides what a station may publish
    from the credential rather than from the frame. These cover the station's
    half — the platform's refusal check has its own tests.
    """

    def transport(self, url="wss://p.example/broker", **kw):
        from gsu.transport.relay import RelayTransport
        return RelayTransport(url, password="secret", **kw)

    def test_the_url_scheme_picks_it(self):
        from gsu.transport import build_transport
        from gsu.transport.relay import RelayTransport
        for url in ("ws://127.0.0.1:8000/broker", "wss://p.example/broker"):
            got = build_transport(url, None, "secret")
            self.assertIsInstance(got, RelayTransport, url)

    def test_a_trust_refusal_is_reported_rather_than_thrown_away(self):
        # `Refusal` is a RuntimeError, and `_connect` used to catch only
        # `(WebSocketError, OSError)` — so a station with nothing to verify the
        # relay against died in its own thread with `last_error` still None.
        # The console then showed a box that simply had no broker and no reason
        # given, which is the hardest possible version of this fault to
        # diagnose from a remote site.
        from gsu import tls
        relay = self.transport(
            trust=tls.Trust(mode=tls.TRUST_PINNED, path=None, purpose="broker"))
        self.assertFalse(relay._connect())
        self.assertIsNotNone(relay.last_error)
        self.assertIn("no broker CA", relay.last_error)
        # A certificate this station will not accept is a different fault from
        # a link that is down, and the agent raises a different condition for
        # each. The Redis transport always reported this; the relay did not.
        self.assertTrue(relay.tls_failed)
        # And it is not retried: a refusal is a decision, permanent until
        # somebody changes something, so reconnect noise must not bury it.
        self.assertTrue(relay._stop.is_set())

    def test_the_platform_stating_system_trust_lets_the_relay_connect(self):
        # The deployment case behind a public-CA proxy. Not a refusal, so the
        # thread stays alive and the failure below is an ordinary unreachable
        # host rather than a trust decision.
        from gsu import tls
        relay = self.transport(
            trust=tls.Trust(mode=tls.TRUST_SYSTEM, purpose="broker"))
        self.assertFalse(relay._connect())          # nothing is listening
        self.assertFalse(relay._stop.is_set())
        self.assertFalse(relay.tls_failed)

    def test_publishing_with_no_link_is_a_counted_drop(self):
        # Never queued. Telemetry is current state, not a ledger — replaying
        # stale readings into a live console after an outage is worse than a
        # gap, and a station quietly dropping everything must not look like a
        # quiet site.
        relay = self.transport()
        self.assertFalse(relay.publish("gsu/x/telemetry", {"a": 1}))
        self.assertFalse(relay.publish("gsu/x/telemetry", {"a": 2}))
        self.assertEqual(relay.dropped, 2)
        self.assertFalse(relay.connected)

    def test_an_unserialisable_payload_is_dropped_not_raised(self):
        relay = self.transport()
        relay._ready.set()
        relay._socket = _FakeSocket()
        self.assertFalse(relay.publish("gsu/x/telemetry", {"bad": object()}))
        self.assertEqual(relay.dropped, 1)

    def test_an_oversized_frame_is_dropped_before_it_is_sent(self):
        # A megabyte is a bug upstream, and finding it as a stalled socket is
        # worse than finding it as a log line.
        from gsu.transport.relay import MAX_FRAME_BYTES
        relay = self.transport()
        relay._ready.set()
        socket = _FakeSocket()
        relay._socket = socket
        relay.publish("gsu/x/telemetry", {"pad": "y" * (MAX_FRAME_BYTES + 100)})
        self.assertEqual(socket.sent, [])
        self.assertEqual(relay.dropped, 1)

    def test_a_published_frame_carries_topic_and_payload(self):
        relay = self.transport()
        relay._ready.set()
        socket = _FakeSocket()
        relay._socket = socket
        self.assertTrue(relay.publish("gsu/x/telemetry", {"a": 1}))
        self.assertEqual(json.loads(socket.sent[0]),
                         {"topic": "gsu/x/telemetry", "payload": {"a": 1}})

    def test_a_command_reaches_its_handler(self):
        relay = self.transport()
        got = []
        relay.subscribe("cmd/gsu/x", lambda t, p: got.append((t, p)))
        relay._on_message(1, json.dumps(
            {"topic": "cmd/gsu/x", "payload": {"kind": "light.set"}}).encode())
        self.assertEqual(got, [("cmd/gsu/x", {"kind": "light.set"})])

    def test_a_handler_that_raises_does_not_end_the_link(self):
        relay = self.transport()

        def bad(topic, payload):
            raise RuntimeError("no")

        relay.subscribe("cmd/gsu/x", bad)
        relay._on_message(1, json.dumps(
            {"topic": "cmd/gsu/x", "payload": {"kind": "x"}}).encode())
        # Still usable: one bad command must not take the transport down.
        self.assertEqual(relay.refusals, {})

    def test_malformed_frames_are_ignored(self):
        relay = self.transport()
        for frame in (b"not json", b"[]", b'{"topic": 1}', b'{"payload": {}}'):
            relay._on_message(1, frame)   # must not raise

    def test_a_refusal_is_reported_rather_than_retried(self):
        # An ACL fault and an unreachable broker both look like a failed
        # publish, and they are completely different problems.
        relay = self.transport()
        relay._on_message(1, json.dumps({
            "type": "refused", "topic": "gsu/other/telemetry",
            "reason": "not yours",
        }).encode())
        self.assertEqual(relay.refusals, {"gsu/other/telemetry": "not yours"})

    def test_it_will_not_send_a_credential_over_plaintext(self):
        # Everywhere else in the station refuses to fall back to an unverified
        # connection; a socket carrying the station's bearer token is the last
        # place to make an exception.
        relay = self.transport(url="ws://192.168.2.49:8000/broker")
        self.assertFalse(relay._connect())
        self.assertIn("plaintext", (relay.last_error or ""))

    def test_loopback_plaintext_is_allowed_for_a_test_harness(self):
        # No network, so nobody to be on it. It fails to connect here because
        # nothing is listening, not because it was refused.
        relay = self.transport(url="ws://127.0.0.1:1/broker")
        relay._connect()
        self.assertNotIn("plaintext", (relay.last_error or ""))


class _FakeSocket:
    connected = True

    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(text)
        return True

    def close(self, reason=""):
        self.connected = False
