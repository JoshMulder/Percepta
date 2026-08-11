"""The station half of remote update: record a target, report the version, and
refuse a target the host could not act on. The privileged work is the host
updater's, outside the sandbox (DECISIONS.md item 48), and is not exercised here.
"""

import json
import tempfile
import unittest
from pathlib import Path

from gsu.commands import CommandRouter, build_handlers
from gsu.update import UpdateCoordinator

_DIGEST = "sha256:" + "a" * 64


class UpdateCoordinatorTests(unittest.TestCase):
    def _coord(self, directory, version="2.3.1"):
        return UpdateCoordinator(version, Path(directory) / "update")

    def test_signing_keys_are_written_and_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = self._coord(directory)
            keys_dir = Path(directory) / "update" / "signing-keys"

            coord.store_signing_keys(("KEY-A", "KEY-B"))
            self.assertEqual(
                sorted(p.read_text() for p in keys_dir.glob("*.pub")),
                ["KEY-A", "KEY-B"])
            # A rotation: keep one, add one, drop one.
            coord.store_signing_keys(("KEY-B", "KEY-C"))
            self.assertEqual(
                sorted(p.read_text() for p in keys_dir.glob("*.pub")),
                ["KEY-B", "KEY-C"])
            # An empty set — a platform that ships none — clears them, so nothing
            # verifies and nothing runs.
            coord.store_signing_keys(())
            self.assertEqual(list(keys_dir.glob("*.pub")), [])

    def test_the_enrolment_carries_and_persists_the_signing_keys(self):
        from gsu.credentials import Enrolment
        body = {
            "station_id": "s1",
            "credential": {
                "secret": "x",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "renew_after": "2098-01-01T00:00:00+00:00",
            },
            "broker": {"url": "wss://example/broker"},
            "station": {},
            "config_version": 0,
            "update_signing_keys": ["KEY-A", "KEY-B"],
        }
        enrol = Enrolment.from_response(body)
        self.assertEqual(enrol.update_signing_keys, ("KEY-A", "KEY-B"))
        # Survives the on-disk round-trip the store uses.
        restored = Enrolment.from_json(enrol.to_json())
        self.assertEqual(restored.update_signing_keys, ("KEY-A", "KEY-B"))

    def test_a_request_writes_the_target_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = self._coord(directory)
            coord.request(image="reg/percepta-gsu", tag="2.4.0", digest=_DIGEST)
            marker = json.loads(
                (Path(directory) / "update" / "update-request.json").read_text())
        self.assertEqual(marker["image"], "reg/percepta-gsu")
        self.assertEqual(marker["tag"], "2.4.0")
        self.assertEqual(marker["digest"], _DIGEST)
        self.assertEqual(marker["from_version"], "2.3.1")
        self.assertFalse(marker["force"])

    def test_a_malformed_digest_is_refused_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = self._coord(directory)
            with self.assertRaises(ValueError):
                coord.request(image="reg/x", tag="2.4.0", digest="not-a-digest")
            self.assertFalse(
                (Path(directory) / "update" / "update-request.json").exists())

    def test_a_missing_image_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self._coord(directory).request(image="", tag="2.4.0", digest=_DIGEST)

    def test_state_reports_running_then_running_and_desired(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = self._coord(directory, version="2.3.1")
            self.assertEqual(coord.state(), {"running_version": "2.3.1"})
            coord.request(image="reg/x", tag="2.4.0", digest=_DIGEST)
            state = coord.state()
        self.assertEqual(state["running_version"], "2.3.1")
        self.assertEqual(state["desired_version"], "2.4.0")

    def test_a_desired_equal_to_running_is_not_reported_as_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = self._coord(directory, version="2.4.0")
            coord.request(image="reg/x", tag="2.4.0", digest=_DIGEST)
            self.assertNotIn("desired_version", coord.state())

    def test_state_surfaces_the_host_updaters_last_result(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = self._coord(directory, version="2.4.0")
            updir = Path(directory) / "update"
            updir.mkdir(parents=True)
            (updir / "update-status.json").write_text(json.dumps({
                "last_result": "rolled_back", "last_version": "2.4.0",
                "at": "2026-08-05T00:00:00Z",
            }))
            state = coord.state()
        self.assertEqual(state["update_last_result"], "rolled_back")
        self.assertEqual(state["update_last_version"], "2.4.0")


class SystemUpdateCommandTests(unittest.TestCase):
    """The command records the target and never does the update itself."""

    def _router(self, coord):
        return CommandRouter(build_handlers(None, None, None, updates=coord))

    def test_the_command_records_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = UpdateCoordinator("2.3.1", Path(directory) / "update")
            applied = self._router(coord).dispatch({
                "kind": "system.update", "image": "reg/x",
                "tag": "2.4.0", "digest": _DIGEST,
            })
            self.assertTrue(applied)
            self.assertEqual(coord.desired_version, "2.4.0")

    def test_a_bad_target_is_a_handled_failure_not_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = UpdateCoordinator("2.3.1", Path(directory) / "update")
            # dispatch catches the ValueError request() raises and reports
            # not-applied, rather than taking down the command loop.
            applied = self._router(coord).dispatch(
                {"kind": "system.update", "image": "reg/x", "digest": "bad"})
        self.assertFalse(applied)

    def test_no_coordinator_means_the_command_is_ignored(self):
        # Same as any command for a thing that is not there: ignored, not
        # rejected, so an older station and a newer platform coexist.
        router = CommandRouter(build_handlers(None, None, None))
        self.assertFalse(router.dispatch({"kind": "system.update", "digest": _DIGEST}))


if __name__ == "__main__":
    unittest.main()
