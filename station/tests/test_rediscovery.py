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
from gsu.config import AgentConfig
from gsu.devices.inventory import Inventory, SlotReport


def agent_in(directory: str) -> Agent:
    return Agent(AgentConfig(
        home=Path(directory), setup_enabled=False, single_instance=False, demo=True))


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


# RetiredCameraTests is gone with the CSI camera driver.
#
# It guarded a real and expensive bug: the driver held a long-lived
# `picamera2` object, and a `Picamera2()` that raised *after* acquiring the
# sensor leaked the acquisition with nothing left for `close()` to close — so
# the camera stayed wedged for the life of the process and every plausible fix
# looked correct and changed nothing.
#
# The Pi 2B and its CSI ribbon are out of scope, and with them the only driver
# that ever held a sensor handle across captures. A network camera is a URL.
# The lease behaviour these tests also covered — a capture takes the lease and
# gives it back, a retired driver stops — belongs to `camera/ownership.py` and
# is exercised by MidStreamTests below and by test_video.py.


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

    def test_a_forced_rebuild_replaces_the_camera_even_mid_stream(self):
        # The deliberate camera change from the setup page. Deferring it is what
        # rediscovery must do and a save must not: the deferral discharges when
        # the stream ends, and a platform viewer that keeps reconnecting keeps
        # the stream up for ever, so the new camera would never arrive without a
        # restart. `force_camera` overrides the deferral for that one caller.
        for state in ("streaming", "starting"):
            with self.subTest(state=state):
                self.agent.stream.state = state
                held = self.agent.camera
                self.agent.build_devices(force_camera=True)
                self.assertIsNot(self.agent.camera, held,
                                 "the forced change was deferred under the stream")
                self.assertFalse(self.agent._camera_rebuild_owed,
                                 "a forced rebuild left a debt to discharge later")
        self.agent.stream.state = "idle"

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
            slot="camera", type_id="onvif-network-camera",
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
