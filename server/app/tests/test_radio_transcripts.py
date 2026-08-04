"""The airband-transcript endpoint that feeds the radio panel's history popout.

These are `radio.transmission` events read straight from the ledger, newest
first — not a downsample, because a transmission has no newer version.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from backend.database.models.station_event import StationEvent


def _event(org, station, *, seq, message, type="radio.transmission",
           clock="synced", secs_ago=0):
    now = datetime.now(UTC)
    when = now - timedelta(seconds=secs_ago)
    return StationEvent(
        id=uuid.uuid4(),
        created_at=now,
        updated_at=now,
        organization_id=org.id,
        ground_station_id=station.id,
        event_id=str(uuid.uuid4()),
        seq=seq,
        at=when,
        received_at=when,
        clock=clock,
        type=type,
        severity="info",
        message=message,
    )


def test_transcripts_come_back_newest_first(client, station, db, org):
    db.add(_event(org, station, seq=1, message="older over", secs_ago=60))
    db.add(_event(org, station, seq=2, message="newer over", secs_ago=1))
    db.commit()

    body = client.get(f"/api/stations/{station.id}/radio/transcripts").json()

    assert [p["message"] for p in body] == ["newer over", "older over"]


def test_only_transcription_events_are_returned(client, station, db, org):
    db.add(_event(org, station, seq=1, message="118.700 MHz, 3s: cleared to land"))
    db.add(_event(
        org, station, seq=2, message="floodlight drew no current",
        type="light.no_draw",
    ))
    db.commit()

    body = client.get(f"/api/stations/{station.id}/radio/transcripts").json()

    assert [p["message"] for p in body] == ["118.700 MHz, 3s: cleared to land"]


def test_the_unsynced_clock_flag_travels(client, station, db, org):
    # A box with no battery-backed clock still logs; the console needs to know
    # the time cannot be trusted rather than draw it confidently in the wrong hour.
    db.add(_event(org, station, seq=1, message="x", clock="unsynced"))
    db.commit()

    body = client.get(f"/api/stations/{station.id}/radio/transcripts").json()

    assert body[0]["clock"] == "unsynced"
