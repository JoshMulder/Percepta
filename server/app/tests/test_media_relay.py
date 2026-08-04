"""The media relay's group-of-pictures cache, driven without a station or a
browser.

The relay must hand a viewer that attaches mid-stream a *decodable* run — the
init segment, the last keyframe, and the frames since — so a picture appears at
once instead of waiting for the next keyframe. Keeping only the most recent
fragment did not deliver that: it is almost always a delta, which decodes as
nothing on its own. These drive `MediaRelay` directly; there is no database in
this path, so each test is a plain coroutine run to completion.
"""

from __future__ import annotations

import asyncio
import uuid

from backend.realtime.media import (
    GOP_CACHE_MAX_BYTES,
    GOP_CACHE_MAX_FRAGMENTS,
    MediaRelay,
)


async def _primed() -> tuple[MediaRelay, uuid.UUID]:
    relay = MediaRelay()
    station_id, organization_id = uuid.uuid4(), uuid.uuid4()
    await relay.publisher_connected(station_id, organization_id)
    await relay.publish(station_id, b"INIT", is_init=True)
    return relay, station_id


def test_a_new_viewer_gets_the_init_the_keyframe_and_the_frames_since() -> None:
    async def scenario() -> list[bytes]:
        relay, station_id = await _primed()
        await relay.publish(station_id, b"K1", keyframe=True)
        await relay.publish(station_id, b"p1")
        await relay.publish(station_id, b"p2")
        return relay.get(station_id).snapshot_for_new_viewer()

    assert asyncio.run(scenario()) == [b"INIT", b"K1", b"p1", b"p2"]


def test_a_new_keyframe_starts_a_fresh_group() -> None:
    async def scenario() -> list[bytes]:
        relay, station_id = await _primed()
        await relay.publish(station_id, b"K1", keyframe=True)
        await relay.publish(station_id, b"p1")
        await relay.publish(station_id, b"K2", keyframe=True)
        await relay.publish(station_id, b"p2")
        return relay.get(station_id).snapshot_for_new_viewer()

    # Only the latest group — the old one is history a live viewer never needs.
    assert asyncio.run(scenario()) == [b"INIT", b"K2", b"p2"]


def test_a_delta_before_any_keyframe_is_not_cached() -> None:
    async def scenario() -> list[bytes]:
        relay, station_id = await _primed()
        await relay.publish(station_id, b"p0")  # arrives before any keyframe
        return relay.get(station_id).snapshot_for_new_viewer()

    # A lone delta decodes as nothing, so only the init segment is offered.
    assert asyncio.run(scenario()) == [b"INIT"]


def test_an_oversized_group_is_dropped_rather_than_held_unbounded() -> None:
    async def scenario() -> list[bytes]:
        relay, station_id = await _primed()
        await relay.publish(station_id, b"K", keyframe=True)
        # A delta that pushes the group past the cap clears it; a joiner then
        # waits for the next keyframe, exactly as it did before the cache.
        await relay.publish(station_id, b"x" * (GOP_CACHE_MAX_BYTES + 1))
        return relay.get(station_id).snapshot_for_new_viewer()

    assert asyncio.run(scenario()) == [b"INIT"]


def test_a_group_longer_than_the_fragment_cap_is_dropped_whole() -> None:
    async def scenario() -> list[bytes]:
        relay, station_id = await _primed()
        await relay.publish(station_id, b"K", keyframe=True)
        for i in range(GOP_CACHE_MAX_FRAGMENTS + 5):
            await relay.publish(station_id, b"p%d" % i)
        return relay.get(station_id).snapshot_for_new_viewer()

    # The whole group replays to a joiner in one burst, and the viewer's staging
    # queue has to hold it intact — so an over-long group is dropped rather than
    # truncated to a keyframe-less run. The joiner waits for the next keyframe.
    assert asyncio.run(scenario()) == [b"INIT"]


def test_a_group_within_the_cap_is_kept_intact_from_its_keyframe() -> None:
    async def scenario() -> list[bytes]:
        relay, station_id = await _primed()
        await relay.publish(station_id, b"K", keyframe=True)
        for i in range(GOP_CACHE_MAX_FRAGMENTS - 1):
            await relay.publish(station_id, b"p%d" % i)
        return relay.get(station_id).snapshot_for_new_viewer()

    snapshot = asyncio.run(scenario())
    assert snapshot[0] == b"INIT" and snapshot[1] == b"K"
    assert len(snapshot) == GOP_CACHE_MAX_FRAGMENTS + 1  # init + the full group


def test_a_fresh_publisher_session_clears_the_group() -> None:
    async def scenario() -> list[bytes]:
        relay, station_id = await _primed()
        await relay.publish(station_id, b"K", keyframe=True)
        # A new encoder session: the old group describes a stream, and an init
        # segment, that no longer exist.
        await relay.publisher_connected(station_id, uuid.uuid4())
        return relay.get(station_id).snapshot_for_new_viewer()

    assert asyncio.run(scenario()) == []


# --- the warm linger -----------------------------------------------------


def _recording_relay(linger: float) -> tuple[MediaRelay, list[str]]:
    calls: list[str] = []

    async def on_demand(stream, wanted: bool) -> None:  # noqa: ANN001
        calls.append("start" if wanted else "stop")

    relay = MediaRelay()
    relay.on_demand_changed = on_demand
    relay.linger_seconds = linger
    return relay, calls


def test_with_no_linger_the_stream_stops_the_instant_the_last_viewer_leaves() -> None:
    async def scenario() -> list[str]:
        relay, calls = _recording_relay(0)
        station_id, organization_id = uuid.uuid4(), uuid.uuid4()
        _, queue = await relay.attach(station_id, organization_id)
        await relay.detach(station_id, queue)
        return calls

    assert asyncio.run(scenario()) == ["start", "stop"]


def test_the_linger_defers_the_stop_and_it_fires_if_nobody_returns() -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        relay, calls = _recording_relay(0.05)
        station_id, organization_id = uuid.uuid4(), uuid.uuid4()
        _, queue = await relay.attach(station_id, organization_id)
        await relay.detach(station_id, queue)
        during = list(calls)  # the stop has not been sent yet
        await asyncio.sleep(0.15)  # the window elapses with nobody back
        return during, calls

    during, after = asyncio.run(scenario())
    assert during == ["start"]
    assert after == ["start", "stop"]


def test_a_viewer_returning_inside_the_window_cancels_the_stop() -> None:
    async def scenario() -> list[str]:
        relay, calls = _recording_relay(0.1)
        station_id, organization_id = uuid.uuid4(), uuid.uuid4()
        _, queue = await relay.attach(station_id, organization_id)
        await relay.detach(station_id, queue)
        await asyncio.sleep(0.02)  # still inside the window
        await relay.attach(station_id, organization_id)  # back again
        await asyncio.sleep(0.15)  # past where the stop would have fired
        return calls

    # The stream never stopped, so the encoder never had to spin up again.
    assert "stop" not in asyncio.run(scenario())
