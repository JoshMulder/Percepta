"""The relay's in-band credential-refusal handling.

The platform signals a refused credential two ways: a 4401 close code and an
in-band `{"type":"unauthorized"}` frame. The frame exists because a 4401 close
code does not survive every proxy — Cloudflare strips it — so a station behind
one would otherwise never learn the close was an auth refusal and would hot-loop
reconnecting instead of renewing. This checks the frame sets `credential_refused`
(the flag the agent turns into a renew-and-return) and that ordinary downward
frames do not.
"""

import json
import unittest

from gsu.transport.relay import RelayTransport


class UnauthorizedFrameTests(unittest.TestCase):
    def make(self) -> RelayTransport:
        return RelayTransport("wss://example.test/broker", secret="a-secret")

    def _deliver(self, relay: RelayTransport, obj: dict) -> None:
        relay._on_message(1, json.dumps(obj).encode("utf-8"))

    def test_an_unauthorized_frame_sets_credential_refused(self):
        relay = self.make()
        self.assertFalse(relay.credential_refused.is_set())
        self._deliver(relay, {"type": "unauthorized", "reason": "credential revoked"})
        self.assertTrue(
            relay.credential_refused.is_set(),
            "an in-band unauthorized frame must drive the renew-and-return path",
        )

    def test_a_stream_refusal_does_not_set_credential_refused(self):
        relay = self.make()
        self._deliver(relay, {"type": "refused", "stream": "c", "reason": "nope"})
        self.assertFalse(relay.credential_refused.is_set())
        self.assertEqual(relay._refusals.get("c"), "nope")

    def test_an_ordinary_command_does_not_set_credential_refused(self):
        relay = self.make()
        self._deliver(relay, {"stream": "c", "payload": {"kind": "noop"}})
        self.assertFalse(relay.credential_refused.is_set())

    def test_a_malformed_frame_is_ignored(self):
        relay = self.make()
        relay._on_message(1, b"{ not json")
        self.assertFalse(relay.credential_refused.is_set())
