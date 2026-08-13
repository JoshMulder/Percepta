"""The station's own host stats, for the settings overview.

Health only exists on the live fan-out — nothing stores it — so the ingest keeps
each station's last frame in a short-lived Redis key and this endpoint reads that
one key. Redis is mocked here: what is being checked is that a cached frame is
surfaced, and that every way of *not* having one degrades to "nothing to report"
rather than to stale numbers or a 500.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from unittest import mock

from backend.database.models.ground_station import GroundStation

FRAME = {
    "kind": "health",
    "status": "ok",
    "agent_version": "0.2.2",
    "software": {"running_version": "v0.2.2"},
    "system": {
        "cpu_percent": 12.5,
        "load_1m": 0.4,
        "temperature_c": 58.0,
        "uptime_s": 93600,
        "memory": {"total_mb": 8000, "used_mb": 2000, "used_percent": 25.0},
    },
}


def cached(*blobs):
    """Stand in for the ingest's Redis snapshots, one blob per key asked for."""
    return mock.patch(
        "backend.api.stations.read_latest_sync", return_value=list(blobs)
    )


def seen_now(db, station) -> None:
    station.last_seen_at = datetime.now(UTC)
    db.add(station)
    db.commit()


class TestHealth:
    def test_a_cached_frame_is_surfaced(self, client, db, station):
        seen_now(db, station)
        with cached(json.dumps(FRAME)):
            response = client.get(f"/api/stations/{station.id}/health")
        assert response.status_code == 200, response.text
        out = response.json()
        assert out["online"] is True
        assert out["status"] == "ok"
        assert out["system"]["temperature_c"] == 58.0
        assert out["system"]["memory"]["used_percent"] == 25.0

    def test_a_station_that_has_not_reported_says_so(self, client, station):
        """Nulls, not stale numbers. The panel renders this as "no recent
        telemetry", which is the honest answer for a box that has gone quiet."""
        with cached(None):
            response = client.get(f"/api/stations/{station.id}/health")
        assert response.status_code == 200, response.text
        out = response.json()
        assert out["online"] is False
        assert out["system"] is None
        assert out["status"] is None

    def test_an_unreadable_blob_is_not_a_500(self, client, station):
        with cached("{not json at all"):
            response = client.get(f"/api/stations/{station.id}/health")
        assert response.status_code == 200, response.text
        assert response.json()["system"] is None

    def test_an_empty_system_object_reads_as_nothing_to_report(
        self, client, db, station
    ):
        """A host that can supply no stat at all sends `{}` — which must not
        render as a card full of blanks."""
        seen_now(db, station)
        with cached(json.dumps({"kind": "health", "status": "ok", "system": {}})):
            response = client.get(f"/api/stations/{station.id}/health")
        assert response.status_code == 200, response.text
        assert response.json()["system"] is None
        assert response.json()["status"] == "ok"

    def test_another_tenants_station_is_not_available(self, client, db):
        """404, the same as everywhere else: the API never distinguishes "not
        yours" from "does not exist"."""
        other = GroundStation(
            id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            name="Somebody Else's",
            timezone="UTC",
        )
        db.add(other)
        db.commit()

        with cached(json.dumps(FRAME)):
            response = client.get(f"/api/stations/{other.id}/health")
        assert response.status_code == 404, response.text


class TestRunningVersion:
    """The station list carries what each box reports running, so the console can
    show an update-available pill without a request per row."""

    def test_it_comes_from_the_cached_frame(self, client, station):
        with cached(json.dumps(FRAME)):
            response = client.get("/api/stations")
        assert response.status_code == 200, response.text
        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["running_version"] == "v0.2.2"

    def test_an_older_agent_falls_back_to_its_build_constant(self, client, station):
        """A station too old to report `software` still reports `agent_version`,
        and a pill is better driven from that than from nothing."""
        frame = {"kind": "health", "agent_version": "0.1.9"}
        with cached(json.dumps(frame)):
            response = client.get("/api/stations")
        assert response.json()[0]["running_version"] == "0.1.9"

    def test_no_cached_frame_leaves_it_null(self, client, station):
        with cached(None):
            response = client.get("/api/stations")
        assert response.json()[0]["running_version"] is None

    def test_redis_being_unavailable_does_not_break_the_list(self, client, station):
        """read_latest_sync returns [] on any Redis failure. The list is what the
        station switcher is built from, so it must survive that."""
        with mock.patch("backend.api.stations.read_latest_sync", return_value=[]):
            response = client.get("/api/stations")
        assert response.status_code == 200, response.text
        assert response.json()[0]["running_version"] is None
