"""Device rediscovery, and the camera it must never leak.

The wedge this file guards against, from the first real station's journal:
rediscovery rebuilt the camera slot while the video thread held the old driver
mid-capture. The rebuild closed the old instance, but close() is a relinquish —
the in-flight capture reopened picamera2 on an object nothing referenced any
more, and that acquisition outlived every later rebuild. The box churned
`Camera in Running state trying acquire()` until somebody restarted it.

Two properties close it, and each is tested on its own:

* a replaced driver is *retired* — terminal, serialized with capture, never
  reopens — and retirement happens before the replacement is built;
* the camera slot is not rebuilt at all while the live stream holds the
  sensor, because a snapshot failing under the encoder's hold is contention,
  not a broken camera.
"""

import tempfile
import unittest
from pathlib import Path

from gsu.agent import Agent
from gsu.camera.picsi import PiCsiCamera
from gsu.config import AgentConfig
from gsu.devices.inventory import Inventory, SlotReport


def agent_in(directory: str) -> Agent:
    return Agent(AgentConfig(
        home=Path(directory), setup_enabled=False, single_instance=False,
    ))


class RecordingDriver:
    """A stand-in driver that remembers how it was let go of."""

    def __init__(self, log: list, name: str) -> None:
        self._log = log
        self._name = name
        self.retired = False
        self.closed = False

    def retire(self) -> None:
        self.retired = True
        self._log.append(("retire", self._name))

    def close(self) -> None:
        self.closed = True
        self._log.append(("close", self._name))

    def describe(self):
        from gsu.sensors import Device

        return Device(id=self._name, kind=self._name, present=True,
                      detail="recording stand-in", simulated=True)


class RebuildOrderTests(unittest.TestCase):
    """The outgoing driver is off the hardware before the replacement opens."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.agent = agent_in(self._dir.name)
        self.addCleanup(self.agent.shutdown)

    def test_every_outgoing_driver_is_retired_before_any_replacement_is_built(self):
        log: list = []
        for slot in ("adsb", "weather", "power", "light", "camera"):
            self.agent.inventory.drivers[slot] = RecordingDriver(log, slot)

        original_build = Inventory.build

        def recording_build(inventory, slot, context):
            log.append(("build", slot))
            return original_build(inventory, slot, context)

        Inventory.build = recording_build
        try:
            self.agent.build_devices()
        finally:
            Inventory.build = original_build

        first_build = next(i for i, (verb, _) in enumerate(log) if verb == "build")
        retired = {slot for verb, slot in log[:first_build] if verb == "retire"}
        self.assertEqual(
            retired, {"adsb", "weather", "power", "light", "camera"},
            f"drivers still attached when the first replacement was built: {log}",
        )

    def test_a_driver_without_retire_is_still_closed(self):
        log: list = []

        class CloseOnly:
            def close(self):
                log.append("closed")

        self.agent.inventory.drivers["weather"] = CloseOnly()
        self.agent.build_devices()
        self.assertIn("closed", log)

    def test_a_retire_that_raises_does_not_stop_the_rebuild(self):
        class Broken:
            def retire(self):
                raise RuntimeError("sensor wedged")

        self.agent.inventory.drivers["camera"] = Broken()
        self.agent.build_devices()   # must not raise
        self.assertIsNotNone(self.agent.camera)


class RetiredCameraTests(unittest.TestCase):
    """retire() is terminal; close() stays a relinquish. Both on the real
    driver class, with the picamera2 open recorded rather than performed."""

    def camera(self) -> tuple[PiCsiCamera, list]:
        opens: list = []
        driver = PiCsiCamera()
        driver._backend = "picamera2"          # force the fast path

        class FakePicamera2:
            def capture_file(self, buffer, format):
                # A recognisable complete JPEG.
                buffer.write(b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9")

            def stop(self):
                pass

            def close(self):
                pass

        def fake_open():
            opens.append(1)
            return FakePicamera2()

        driver._open_picamera2 = fake_open
        return driver, opens

    def test_close_alone_lets_the_next_capture_reopen(self):
        # The relinquish semantics the stream path depends on: this is the
        # *documented* behaviour, and it is exactly why rediscovery needs the
        # stronger verb.
        driver, opens = self.camera()
        self.assertIsNotNone(driver.capture())
        self.assertEqual(len(opens), 1)
        driver.close()
        self.assertIsNotNone(driver.capture())
        self.assertEqual(len(opens), 2, "close() then capture must reopen")

    def test_a_retired_driver_never_reopens(self):
        driver, opens = self.camera()
        self.assertIsNotNone(driver.capture())
        driver.retire()
        self.assertIsNone(driver.capture())
        self.assertIsNone(driver.capture())
        self.assertEqual(len(opens), 1, "a retired driver reopened the sensor")
        self.assertIn("replaced", driver.unavailable_reason)

    def test_retire_closes_whatever_was_open(self):
        closed: list = []
        driver, _ = self.camera()
        frame = driver.capture()
        self.assertIsNotNone(frame)
        driver._camera.close = lambda: closed.append(1)
        driver.retire()
        self.assertTrue(closed, "retire() left the sensor held")
        self.assertIsNone(driver._camera)


class MidStreamTests(unittest.TestCase):
    """While the encoder has the sensor, rediscovery leaves the camera alone."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.agent = agent_in(self._dir.name)
        self.addCleanup(self.agent.shutdown)

    def test_the_camera_slot_is_not_rebuilt_while_streaming(self):
        for state in ("streaming", "starting"):
            with self.subTest(state=state):
                self.agent.stream.state = state
                before = self.agent.camera
                driver_before = self.agent.inventory.drivers.get("camera")
                other_before = self.agent.adsb
                self.agent.build_devices()
                self.assertIs(self.agent.camera, before,
                              "the camera was rebuilt under the encoder")
                self.assertIs(self.agent.inventory.drivers.get("camera"),
                              driver_before)
                self.assertIsNot(self.agent.adsb, other_before,
                                 "the other slots must still rebuild")
        self.agent.stream.state = "idle"

    def test_the_camera_is_rebuilt_again_once_the_stream_ends(self):
        self.agent.stream.state = "streaming"
        held = self.agent.camera
        self.agent.build_devices()
        self.assertIs(self.agent.camera, held)
        self.agent.stream.state = "idle"
        self.agent.build_devices()
        self.assertIsNot(self.agent.camera, held)

    def test_a_streaming_camera_is_not_retired(self):
        log: list = []
        self.agent.inventory.drivers["camera"] = RecordingDriver(log, "camera")
        self.agent.stream.state = "streaming"
        self.agent.build_devices()
        self.agent.stream.state = "idle"
        self.assertNotIn(("retire", "camera"), log)
        self.assertNotIn(("close", "camera"), log)

    def test_a_failing_camera_does_not_trigger_rediscovery_mid_stream(self):
        # The circular trigger from the journal: captures fail *because* the
        # stream holds the sensor, the slot reports failed, and rediscovery
        # then rebuilds against the one working consumer of the camera.
        report = SlotReport(
            slot="camera", type_id="raspberry-pi-csi",
            label="Raspberry Pi camera (CSI ribbon)", connection="csi",
            configured=True, detected=False, driver_available=True,
            status="configured_absent", detail="Camera in Running state",
            simulated=False, provides=("video",), absent=(),
            telemetry_kind="video",
        )
        # The simulated receiver is "absent" until it has been pumped once;
        # this test is about the camera, so give it its first frames.
        self.agent.adsb.pump()
        others = [r for r in self.agent.inventory.report() if r.slot != "camera"]
        self.agent.inventory.report = lambda: [*others, report]

        self.agent.stream.state = "streaming"
        self.assertFalse(self.agent._anything_missing(),
                         "a busy sensor was read as a missing device")
        self.agent.stream.state = "idle"
        self.assertTrue(self.agent._anything_missing(),
                        "with no stream, a failed camera is a real fault")


if __name__ == "__main__":
    unittest.main()
