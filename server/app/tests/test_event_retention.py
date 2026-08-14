"""What survives a prune, and what a prune that deletes nothing looks like.

The failure this suite is really about is not "the prune deleted too much". It is
"the prune deleted NOTHING and said so cheerfully" — which is what happens if the
RLS bypass is dropped, because station_events is FORCE ROW LEVEL SECURITY and a
DELETE that matches no rows commits perfectly happily. So every test here asserts
on the SURVIVING SET rather than on a return code, and one asserts a real row
count came back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database.models.ground_station import GroundStation
from backend.database.models.station_event import StationEvent
from backend.services.event_retention import (
    INFO_RETENTION,
    LONG_RETENTION,
    EventRetention,
)

NOW = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)


def _event(
    db: Session,
    station: GroundStation,
    *,
    key: str,
    type_: str,
    severity: str,
    age: timedelta,
) -> str:
    when = NOW - age
    # `seq` is the station's own monotonic counter and is NOT NULL. Derived from
    # the key so each row differs; nothing here depends on its ordering.
    db.add(
        StationEvent(
            id=uuid.uuid4(),
            seq=abs(hash(key)) % 1_000_000,
            organization_id=station.organization_id,
            ground_station_id=station.id,
            event_id=key,
            type=type_,
            severity=severity,
            message=key,
            at=when,
            received_at=when,
            clock="ok",
        )
    )
    return key


@pytest.fixture()
def seeded(db: Session, station: GroundStation) -> list[str]:
    """One row on each side of each horizon, for each kind of row.

    Named by what they are, so a failure reads as "the 500-day warning was
    deleted" rather than as a uuid mismatch.
    """
    keys = [
        _event(db, station, key="info-fresh", type_="video.stream_started",
               severity="info", age=timedelta(days=10)),
        _event(db, station, key="info-old", type_="video.stream_started",
               severity="info", age=INFO_RETENTION + timedelta(days=1)),
        _event(db, station, key="warn-mid", type_="uplink.down",
               severity="warning", age=INFO_RETENTION + timedelta(days=60)),
        _event(db, station, key="warn-ancient", type_="uplink.down",
               severity="warning", age=LONG_RETENTION + timedelta(days=1)),
        _event(db, station, key="platform-old", type_="platform.station.updated",
               severity="info", age=INFO_RETENTION + timedelta(days=30)),
        _event(db, station, key="platform-ancient", type_="platform.station.updated",
               severity="info", age=LONG_RETENTION + timedelta(days=1)),
    ]
    db.commit()
    return keys


def _surviving(db: Session) -> set[str]:
    db.expire_all()
    return set(db.execute(select(StationEvent.event_id)).scalars())


def test_the_two_horizons_keep_exactly_the_right_rows(
    db: Session, seeded: list[str]
) -> None:
    EventRetention()._prune(NOW)

    assert _surviving(db) == {
        # Inside the short horizon.
        "info-fresh",
        # Warning: past 90 days but nowhere near 400. Evidence outlives the
        # shift that produced it — this is the row the whole severity split
        # exists to keep.
        "warn-mid",
        # platform.* keeps the long horizon whatever its severity: it records
        # what WE did to a customer's system.
        "platform-old",
    }


def test_a_prune_reports_how_many_rows_it_removed(
    db: Session, seeded: list[str]
) -> None:
    """Zero is the exact symptom of the RLS bypass being dropped.

    Under FORCE ROW LEVEL SECURITY a DELETE with no org context matches nothing,
    commits, and logs success. A test that only checked "no exception" would pass
    against a prune that had silently stopped working — so this one insists a
    real count came back.
    """
    removed = EventRetention()._delete_batched(
        db,
        "received_at < :before",
        {"before": NOW - LONG_RETENTION},
        what="test",
    )
    assert removed == 2  # warn-ancient and platform-ancient


def test_nothing_is_deleted_when_nothing_has_aged(
    db: Session, station: GroundStation
) -> None:
    _event(db, station, key="today", type_="uplink.up", severity="info",
           age=timedelta(hours=1))
    db.commit()
    EventRetention()._prune(NOW)
    assert _surviving(db) == {"today"}


def test_the_first_prune_does_not_wait_a_whole_interval() -> None:
    """power_history stamps _last_prune in its constructor, so its first prune is
    six hours after start — and on a box redeployed several times a day that
    prune has never once run. This one is due five minutes after start."""
    retention = EventRetention()
    assert retention._last_prune is None
