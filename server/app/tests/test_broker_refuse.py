"""The broker's in-band credential refusal.

A 4401 *close code* does not survive every proxy — Cloudflare strips it — so a
station behind one never learns the close meant "credential refused" and
hot-loops reconnecting instead of renewing. `_refuse` sends an in-band
`{"type":"unauthorized"}` frame that rides the same accepted socket and does
survive, then closes 4401 anyway (for direct connections that preserve it).

These exercise `_refuse` directly against a fake socket — no DB, no Redis — so
they run without the Postgres the rest of this suite needs.
"""

import asyncio
import json

from backend.api.broker import _refuse


class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []
        self.closed_code: int | None = None

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


def test_sends_unauthorized_frame_then_closes_4401():
    ws = _FakeWS()
    asyncio.run(_refuse(ws, "credential revoked"))
    assert len(ws.sent) == 1, "the in-band frame must go before the close"
    frame = json.loads(ws.sent[0])
    assert frame["type"] == "unauthorized"
    assert frame["reason"] == "credential revoked"
    assert ws.closed_code == 4401


def test_close_still_happens_when_the_send_fails():
    # A dying socket may reject the frame; the 4401 close must still go out so a
    # direct connection that does preserve the code still learns of the refusal.
    class _SendFails(_FakeWS):
        async def send_text(self, text: str) -> None:
            raise RuntimeError("socket already gone")

    ws = _SendFails()
    asyncio.run(_refuse(ws, "no bearer credential"))
    assert ws.sent == []
    assert ws.closed_code == 4401
