"""The station as the platform sees it: channels, commands, and the schemas.

Everything here runs offline against `contract/schemas/`, so a schema change
lands as a test failure on this side rather than as a surprise in conformance.
"""

import json
import tempfile
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
        airband_traffic=traffic,
    )
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
        for kind in ("adsb", "power", "radio", "light", "weather", "health"):
            for payload in self.by_kind(kind)[:5]:
                errors = sorted(TELEMETRY.iter_errors(payload), key=str)
                self.assertFalse(errors, f"{kind}: {[e.message for e in errors]}")

    def test_audio_matches_the_schema_and_is_gated(self):
        audio = self.by_kind("audio")
        self.assertTrue(audio, "the busy profile should have produced audio")
        for payload in audio[:3]:
            errors = sorted(AUDIO.iter_errors(payload), key=str)
            self.assertFalse(errors, [e.message for e in errors])

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
            build_handlers(self.agent.radio, self.agent.light, self.agent._apply_config),
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

    def test_every_command_in_the_schema_has_a_handler(self):
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
