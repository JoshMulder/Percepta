"""Device rediscovery, and the camera it must never leak.

The wedge this file guards against, from the first real station's journal:
rediscovery rebuilt the camera slot while the video thread held the old driver
mid-capture. The rebuild closed the old instance, but close() was a relinquish —
the in-flight capture reopened picamera2 on an object nothing referenced any
more, and that acquisition outlived every later rebuild. The box churned
`Camera in Running state trying acquire()` until somebody restarted it.

Three properties close it now, and each is tested on its own:

* there is no long-lived camera handle to leak at all — the in-process
  libcamera backend is gone, and with it the only thing that can produce
  `Camera in Acquired state trying acquire()`;
* ownership is a lease with a token, so a stale driver cannot release the hold
  its successor was granted — the failure a boolean flag could not refuse;
* the camera slot is not rebuilt at all while the live stream holds the
  sensor, because a capture failing under the encoder's hold is contention,
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
    """retire() is terminal, and there is no longer a handle to leak.

    These used to exercise a `close()`/`retire()` distinction over a
    long-lived picamera2 object: `close()` relinquished and the next capture
    reopened, `retire()` was terminal. That distinction was the previous fix
    and it was not sufficient, because the object it guarded could leak its
    acquisition without either verb ever being called — a `Picamera2()` that
    raised after acquiring left nothing for `close()` to close.

    There is no such object any more (`camera/picsi.py`). Every capture is a
    subprocess that owns the sensor for its own lifetime and nothing between
    them, so what is worth testing is different: that a capture holds the
    lease and gives it back, that a retired driver stops, and that a stale
    driver cannot interfere with its successor.
    """

    def camera(self, lease=None) -> tuple[PiCsiCamera, list]:
        """The real driver, with only the subprocess replaced."""
        calls: list = []
        driver = PiCsiCamera(sensor_lease=lease) if lease else PiCsiCamera()
        driver._backend = "rpicam"
        driver._tool = "rpicam-jpeg"

        def fake_capture():
            calls.append(driver.sensor_lease.holder)
            return b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9"

        driver._capture_cli = fake_capture
        return driver, calls

    def test_the_driver_has_no_in_process_libcamera_backend_at_all(self):
        """The wedge was `Camera in Acquired state trying acquire()`, which
        only a process that acquires twice can produce. This station now
        contains no libcamera: the only backends are a subprocess and none."""
        self.assertIn(PiCsiCamera().backend, ("rpicam", "none"))
        self.assertFalse(hasattr(PiCsiCamera, "_open_picamera2"))
        self.assertFalse(hasattr(PiCsiCamera(), "_camera"))

    def test_a_capture_holds_the_sensor_and_gives_it_back(self):
        driver, calls = self.camera()
        self.assertIsNotNone(driver.capture())
        self.assertEqual(calls, ["the camera preview"],
                         "the subprocess ran without the lease held")
        self.assertTrue(driver.sensor_lease.free,
                        "the lease was not released after the capture")

    def test_a_capture_is_refused_while_something_else_holds_the_sensor(self):
        driver, calls = self.camera()
        token = driver.sensor_lease.acquire("the live stream")
        self.assertIsNone(driver.capture())
        self.assertEqual(calls, [], "the camera was opened under another holder")
        # Contention, named — not a camera fault. The distinction is the whole
        # reason a black preview used to be undiagnosable.
        self.assertIn("the live stream", driver.unavailable_reason)
        self.assertEqual(driver._failures, 0, "contention was counted as a fault")
        driver.sensor_lease.release(token)
        self.assertIsNotNone(driver.capture())

    def test_a_retired_driver_never_captures_again(self):
        driver, calls = self.camera()
        self.assertIsNotNone(driver.capture())
        driver.retire()
        self.assertIsNone(driver.capture())
        self.assertIsNone(driver.capture())
        self.assertEqual(len(calls), 1, "a retired driver opened the sensor")
        self.assertIn("replaced", driver.unavailable_reason)

    def test_a_retired_driver_cannot_free_its_successors_hold(self):
        """The zombie release, which a boolean lock could not have refused.

        Rediscovery builds the replacement while the outgoing driver may still
        be one line into a capture. Under a plain flag the old instance's
        release frees the *new* one's hold and the two then run at once — the
        bug wearing the fix as a disguise.
        """
        from gsu.camera.ownership import SensorLease

        lease = SensorLease("camera")
        old, _ = self.camera(lease)
        new, calls = self.camera(lease)

        stale = lease.acquire("the outgoing driver")
        old.retire()
        successor = lease.acquire("the live stream")
        self.assertIsNone(successor, "the lease was handed out twice")

        lease.release(stale)
        successor = lease.acquire("the live stream")
        self.assertIsNotNone(successor)
        # The stale token is now worthless and must stay worthless.
        self.assertFalse(lease.release(stale))
        self.assertEqual(lease.holder, "the live stream")
        self.assertIsNone(new.capture(), "the successor's hold was broken")
        self.assertEqual(calls, [])


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
