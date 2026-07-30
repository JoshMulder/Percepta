"""The floodlight's current sense: the model, the faults, and the option.

Three claims, each with the failure it exists to catch:

    measured, not assumed   - the sensor reads the lamp circuit, so a dead
                              lamp behind a closed contact and a welded
                              contact behind an off command are both visible
    two volumes             - no draw when commanded on is a warning; still
                              drawing when commanded off is critical, because
                              that one is spending the battery at a site
                              nobody is at
    the reported state      - `light.on` may follow the relay (default) or
                              the measured current, per light, and the
                              telemetry stays inside the contract either way

The amps themselves stay off the wire until the schema carries them —
CONTRACT-QUESTIONS.md item 15 — so the payload checks here assert their
*absence* as deliberately as the health checks assert the faults' presence.
"""

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from gsu.agent import LIGHT_SETTLE_SECONDS, Agent
from gsu.config import AgentConfig
from gsu.sensors.simulated import SimulatedFloodlight

SCHEMAS = Path(__file__).resolve().parent.parent.parent / "contract" / "schemas"
TELEMETRY = Draft202012Validator(
    json.loads((SCHEMAS / "telemetry.schema.json").read_text())
)


class FloodlightModelTests(unittest.TestCase):
    """The simulated light on its own, no agent."""

    def settle(self, light: SimulatedFloodlight, seconds: float = 1.0) -> None:
        for _ in range(int(seconds * 10)):
            light.step(0.1)

    def test_the_current_follows_the_lamp_not_the_command(self):
        light = SimulatedFloodlight()
        self.assertEqual(light.measured_a, 0.0)
        light.request(True)
        self.settle(light)
        self.assertAlmostEqual(light.measured_a, 1.25)   # 60 W at 48 V
        light.request(False)
        self.settle(light)
        self.assertEqual(light.measured_a, 0.0)

    def test_no_sensor_is_none_never_zero(self):
        # "Nothing is measuring" and "measured, nothing flows" are different
        # statements, and the fault checks only run on the second.
        light = SimulatedFloodlight(sense_source="none")
        light.request(True)
        self.settle(light)
        self.assertIsNone(light.measured_a)
        self.assertIn("state read back", light.describe().detail)

    def test_a_dead_lamp_draws_nothing_behind_a_closed_contact(self):
        light = SimulatedFloodlight()
        light.lamp_failed = True
        light.request(True)
        self.settle(light)
        self.assertTrue(light.commanded)
        self.assertEqual(light.measured_a, 0.0)
        self.assertEqual(light.load_w, 0.0)

    def test_a_welded_relay_keeps_drawing_after_the_command(self):
        light = SimulatedFloodlight()
        light.request(True)
        self.settle(light)
        light.relay_welded = True
        light.request(False)
        self.settle(light)
        self.assertFalse(light.commanded)
        self.assertAlmostEqual(light.measured_a, 1.25)

    def test_state_source_current_reports_the_lamp_not_the_coil(self):
        light = SimulatedFloodlight(state_source="current")
        light.lamp_failed = True
        light.request(True)
        self.settle(light)
        self.assertFalse(light.on, "no draw is not 'on', whatever the relay says")
        relay = SimulatedFloodlight(state_source="relay")
        relay.lamp_failed = True
        relay.request(True)
        self.settle(relay)
        self.assertTrue(relay.on, "the default reports the relay, as today")

    def test_a_typo_in_state_source_falls_back_to_the_relay(self):
        light = SimulatedFloodlight(state_source="curent")
        self.assertEqual(light.state_source, "relay")

    def test_the_amps_reach_the_datastream_field_and_the_detail(self):
        light = SimulatedFloodlight()
        light.request(True)
        self.settle(light)
        self.assertIn("1.25 A measured", light.raw_sample()[0])
        self.assertIn("1.25 A measured", light.describe().detail)


class LightFaultTests(unittest.TestCase):
    """The agent's fault checks, driven through real steps."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.agent = Agent(AgentConfig(
            home=Path(self._dir.name), setup_enabled=False, single_instance=False, demo=True))
        self.addCleanup(self.agent.shutdown)
        self.sent: list[dict] = []
        self.agent._publish = lambda topic, payload: self.sent.append(payload) or True
        self.light = self.agent.light

    def steps(self, count: int) -> None:
        for _ in range(count):
            self.agent.step(1.0)

    def conditions(self) -> dict:
        return {c["id"]: c for c in self.agent.health.to_list()}

    def settle_steps(self) -> int:
        return int(LIGHT_SETTLE_SECONDS) + 1

    def light_payloads(self) -> list[dict]:
        return [p for p in self.sent if p.get("kind") == "light"]

    def test_a_healthy_light_raises_nothing(self):
        self.light.request(True)
        self.steps(self.settle_steps() + 2)
        self.assertNotIn("light.no_draw", self.conditions())
        self.assertNotIn("light.stuck_on", self.conditions())

    def test_ordinary_switching_is_not_a_fault(self):
        # The contactor takes 0.4 s: for that moment the light is commanded
        # on and drawing nothing, which is exactly the dead-lamp signature.
        # The settle window is what keeps every switch from being an alarm.
        self.light.request(True)
        self.steps(1)
        self.assertNotIn("light.no_draw", self.conditions())
        self.light.request(False)
        self.steps(1)
        self.assertNotIn("light.stuck_on", self.conditions())

    def test_a_dead_lamp_is_a_warning_with_the_words_that_matter(self):
        self.light.request(True)
        self.steps(self.settle_steps())
        self.light.lamp_failed = True
        self.steps(1)
        condition = self.conditions()["light.no_draw"]
        self.assertEqual(condition["severity"], "warning")
        for word in ("commanded on", "Lamp, fuse or wiring"):
            self.assertIn(word, condition["detail"])
        kinds = [e.kind for e in self.agent.store.recent_events(10)]
        self.assertIn("light.no_draw", kinds)

    def test_a_welded_relay_is_critical_because_it_burns_the_battery(self):
        self.light.request(True)
        self.steps(self.settle_steps())
        self.light.relay_welded = True
        self.light.request(False)
        self.steps(self.settle_steps())
        condition = self.conditions()["light.stuck_on"]
        self.assertEqual(condition["severity"], "critical")
        self.assertIn("welded", condition["detail"])
        self.assertIn("battery", condition["detail"])
        self.assertNotIn("light.no_draw", self.conditions())

    def test_recovery_clears_the_condition_and_says_so(self):
        self.light.request(True)
        self.steps(self.settle_steps())
        self.light.lamp_failed = True
        self.steps(1)
        self.assertIn("light.no_draw", self.conditions())
        self.light.lamp_failed = False
        self.steps(1)
        self.assertNotIn("light.no_draw", self.conditions())
        kinds = [e.kind for e in self.agent.store.recent_events(10)]
        self.assertIn("light.recovered", kinds)

    def test_no_sensor_means_no_judgement(self):
        self.light.sense_source = "none"
        self.light.lamp_failed = True
        self.light.request(True)
        self.steps(self.settle_steps() + 2)
        self.assertNotIn("light.no_draw", self.conditions())

    def test_a_zero_threshold_judges_nothing(self):
        self.light.sense_threshold_a = 0.0
        self.light.lamp_failed = True
        self.light.request(True)
        self.steps(self.settle_steps() + 2)
        self.assertNotIn("light.no_draw", self.conditions())

    def test_the_reported_state_can_come_from_the_current(self):
        self.light.state_source = "current"
        self.light.lamp_failed = True
        self.light.request(True)
        self.steps(self.settle_steps())
        payload = self.light_payloads()[-1]
        self.assertIs(payload["on"], False)
        # And the payload stays inside the contract: no invented amps field
        # until the schema carries one (CONTRACT-QUESTIONS.md item 15).
        errors = sorted(TELEMETRY.iter_errors(payload), key=str)
        self.assertFalse(errors, [error.message for error in errors])
        self.assertNotIn("measured_a", payload)

    def test_the_faults_travel_in_the_health_frame(self):
        self.light.request(True)
        self.steps(self.settle_steps())
        self.light.lamp_failed = True
        self.steps(1)
        payload = self.agent.health_payload()
        errors = sorted(TELEMETRY.iter_errors(payload), key=str)
        self.assertFalse(errors, [error.message for error in errors])
        self.assertIn("light.no_draw", [c["id"] for c in payload["conditions"]])
        light = [d for d in payload["devices"] if d["slot"] == "light"][0]
        self.assertIn("A measured", light["detail"])


if __name__ == "__main__":
    unittest.main()
