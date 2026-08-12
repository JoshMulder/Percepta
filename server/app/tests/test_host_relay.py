"""The host relay's pairing of a station helper and a browser terminal.

A host session is a byte bridge, not request/response, so the relay's whole job
is pairing: register the helper's socket, attach one terminal, supersede an older
one, and take the terminal down when the helper's socket goes. These drive
`HostRelay` directly — no database, plain coroutines, like `test_media_relay.py`.
"""

from __future__ import annotations

import asyncio
import uuid

from backend.realtime.host import HostRelay


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_a_terminal_attaches_to_a_registered_helper() -> None:
    async def scenario() -> bool:
        relay = HostRelay()
        station_id, org = uuid.uuid4(), uuid.uuid4()
        ingest = _FakeWS()
        link = relay.register_ingest(station_id, org, ingest)
        viewer = _FakeWS()
        attached = relay.attach_viewer(station_id, viewer)
        return attached is link and link.viewer is viewer

    assert asyncio.run(scenario()) is True


def test_a_terminal_with_no_helper_gets_nothing() -> None:
    relay = HostRelay()
    assert relay.attach_viewer(uuid.uuid4(), _FakeWS()) is None


def test_a_second_helper_supersedes_and_closes_the_first() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        relay = HostRelay()
        station_id, org = uuid.uuid4(), uuid.uuid4()
        first_ingest, first_viewer = _FakeWS(), _FakeWS()
        relay.register_ingest(station_id, org, first_ingest)
        relay.attach_viewer(station_id, first_viewer)
        second_ingest = _FakeWS()
        second = relay.register_ingest(station_id, org, second_ingest)
        await asyncio.sleep(0)  # let the displaced sockets be closed
        return (
            relay.get(station_id) is second,
            first_ingest.closed,
            first_viewer.closed,
        )

    is_second, ingest_closed, viewer_closed = asyncio.run(scenario())
    assert is_second and ingest_closed and viewer_closed


def test_a_second_terminal_supersedes_the_first() -> None:
    async def scenario() -> tuple[bool, bool]:
        relay = HostRelay()
        station_id, org = uuid.uuid4(), uuid.uuid4()
        relay.register_ingest(station_id, org, _FakeWS())
        first = _FakeWS()
        relay.attach_viewer(station_id, first)
        second = _FakeWS()
        link = relay.attach_viewer(station_id, second)
        await asyncio.sleep(0)
        return link.viewer is second, first.closed

    is_second, first_closed = asyncio.run(scenario())
    assert is_second and first_closed


def test_the_helper_leaving_takes_the_terminal_down() -> None:
    async def scenario() -> bool:
        relay = HostRelay()
        station_id, org = uuid.uuid4(), uuid.uuid4()
        ingest = _FakeWS()
        link = relay.register_ingest(station_id, org, ingest)
        viewer = _FakeWS()
        relay.attach_viewer(station_id, viewer)
        relay.ingest_gone(station_id, link)
        await asyncio.sleep(0)
        # A terminal wired to a PTY that no longer exists is a frozen prompt, so
        # it is closed rather than left looking like a working shell.
        return viewer.closed and relay.get(station_id) is None

    assert asyncio.run(scenario()) is True


def test_wait_for_returns_the_link_when_the_helper_connects() -> None:
    async def scenario() -> bool:
        relay = HostRelay()
        station_id, org = uuid.uuid4(), uuid.uuid4()
        waiter = asyncio.create_task(relay.wait_for(station_id, timeout=1.0))
        await asyncio.sleep(0)
        link = relay.register_ingest(station_id, org, _FakeWS())
        return (await waiter) is link

    assert asyncio.run(scenario()) is True


def test_wait_for_times_out_when_the_helper_does_not_connect() -> None:
    async def scenario():
        return await HostRelay().wait_for(uuid.uuid4(), timeout=0.05)

    assert asyncio.run(scenario()) is None
