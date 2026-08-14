"""station_watch announces a dark station once, and afresh after it recovers.

_scan is driven directly with a stubbed station list and a captured publish, so
these need no database and no running loop of their own: the dark decision and
its de-duplication are pure given those two seams.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from backend.services import station_watch


def _capture(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def fake_publish(organization_id, station_id, payload):
        calls.append((organization_id, station_id, payload))
        return 1

    monkeypatch.setattr(station_watch.hub, "publish_status", fake_publish)
    return calls


def _stations(monkeypatch, rows) -> None:
    """Stand in for the roster query.

    Rows are (station_id, organization_id, last_seen, name). The name was added
    when dark alerts became durable — the alert's message names the site, since
    "a station has gone dark" is not actionable and "Kennels Road has gone dark"
    is. Callers may pass 3-tuples; a name is filled in, so the tests that do not
    care about it stay readable.
    """
    filled = [r if len(r) == 4 else (*r, "Test Station") for r in rows]
    monkeypatch.setattr(station_watch, "_active_stations", lambda: filled)


def _no_database(monkeypatch) -> None:
    """These tests exercise the LIVE announcement, not the durable alert.

    The durable half opens a real transaction against a station that does not
    exist in this test's database, and its failure is already swallowed by
    design (alerting must never end the scan). Stubbing it keeps the failure out
    of the logs and keeps these tests about the thing they are named for.
    """
    monkeypatch.setattr(station_watch, "_record_dark", lambda *a, **k: None)
    monkeypatch.setattr(station_watch, "_clear_dark", lambda *a, **k: None)


def test_a_dark_station_is_announced_once(monkeypatch):
    org, station = uuid.uuid4(), uuid.uuid4()
    dark_since = datetime.now(UTC) - timedelta(minutes=30)
    calls = _capture(monkeypatch)
    _no_database(monkeypatch)
    _stations(monkeypatch, [(station, org, dark_since)])

    alerted: set[uuid.UUID] = set()
    asyncio.run(station_watch._scan(alerted))
    assert len(calls) == 1
    org_id, station_id, payload = calls[0]
    assert (org_id, station_id) == (org, station)
    assert "dark" in payload["alarm"].lower()
    assert station in alerted

    # A second scan while it is still dark must not re-announce it — the console
    # pushes an alert for every alarm, so a repeat here is a repeat in the drawer.
    asyncio.run(station_watch._scan(alerted))
    assert len(calls) == 1


def test_a_live_or_never_seen_station_is_not_announced(monkeypatch):
    org = uuid.uuid4()
    recent = (uuid.uuid4(), org, datetime.now(UTC) - timedelta(seconds=30))
    never = (uuid.uuid4(), org, None)  # provisioning, not a death
    calls = _capture(monkeypatch)
    _no_database(monkeypatch)
    _stations(monkeypatch, [recent, never])

    asyncio.run(station_watch._scan(set()))
    assert calls == []


def test_a_recovered_station_can_go_dark_again(monkeypatch):
    org, station = uuid.uuid4(), uuid.uuid4()
    dark_since = datetime.now(UTC) - timedelta(minutes=30)
    calls = _capture(monkeypatch)
    _no_database(monkeypatch)
    alerted: set[uuid.UUID] = set()

    _stations(monkeypatch, [(station, org, dark_since)])
    asyncio.run(station_watch._scan(alerted))
    assert len(calls) == 1

    # Heard from again: forgotten, so a later death is a fresh alarm rather than
    # one swallowed as a duplicate.
    _stations(monkeypatch, [(station, org, datetime.now(UTC))])
    asyncio.run(station_watch._scan(alerted))
    assert station not in alerted
    assert len(calls) == 1  # a live station raises nothing

    _stations(monkeypatch, [(station, org, dark_since)])
    asyncio.run(station_watch._scan(alerted))
    assert len(calls) == 2  # announced afresh
