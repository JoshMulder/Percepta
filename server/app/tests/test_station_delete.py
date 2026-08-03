"""Deleting a station record that never became a station.

The endpoint is tidying, not decommissioning: it refuses while anything can
still authenticate as the station, and it is for the typos, abandoned plans and
duplicates that accumulate because a record is created in the console before
anyone is standing at the hardware.

The rows that describe a station go with it. The rows that describe what people
did do not — `audit_log` holds `ground_station_id` as a plain column with no
foreign key, so "who let that box onto our platform, and when" survives the row
it refers to. That distinction is what these check.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from backend.database.models.audit_log import AuditLog
from backend.database.models.device import Device
from backend.database.models.ground_station import GroundStation


def test_a_record_with_no_credential_is_deleted(client, station, db):
    response = client.delete(f"/api/stations/{station.id}/config")
    assert response.status_code == 204, response.text
    assert db.get(GroundStation, station.id) is None


def test_a_station_with_devices_is_still_deletable(client, station, db, org):
    """The regression. Every child table of `ground_stations` was created ON
    DELETE CASCADE except `devices`, which got a plain reference — so this
    raised a foreign-key violation, and a 500, on the one operation whose whole
    purpose is tidying up.

    A device row is meaningless without its station. It is inventory, not
    history.
    """
    db.add(Device(
        id=uuid.uuid4(),
        organization_id=org.id,
        ground_station_id=station.id,
        kind="camera",
        slug="cam-north",
        name="North camera",
    ))
    db.commit()

    response = client.delete(f"/api/stations/{station.id}/config")
    assert response.status_code == 204, response.text

    assert db.get(GroundStation, station.id) is None
    remaining = db.execute(
        select(Device).where(Device.ground_station_id == station.id)
    ).scalars().all()
    assert remaining == [], "the device outlived its station"


def test_the_audit_row_outlives_the_station(client, station, db):
    """Deliberately not a cascade. `audit_log.ground_station_id` is a plain
    column with no foreign key, so the trail is not something a delete can
    quietly take with it — which is the whole reason that table exists.
    """
    station_id = station.id
    assert client.delete(f"/api/stations/{station_id}/config").status_code == 204

    rows = db.execute(
        select(AuditLog).where(AuditLog.ground_station_id == station_id)
    ).scalars().all()
    assert any(row.action == "station.deleted" for row in rows), \
        "the deletion left no trace"


def test_a_station_that_can_still_authenticate_is_refused(client, station, db, org):
    """Once a box holds a live credential this stops being tidying: it is out
    there able to publish against a station id the platform would no longer
    know. Revoking is the decommissioning step, and it is what makes the record
    deletable afterwards.
    """
    from datetime import UTC, datetime, timedelta

    from backend.database.models.station_credential import StationCredential

    db.add(StationCredential(
        id=uuid.uuid4(),
        organization_id=org.id,
        ground_station_id=station.id,
        secret_hash="not-a-real-hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    ))
    db.commit()

    response = client.delete(f"/api/stations/{station.id}/config")
    assert response.status_code == 409
    assert "revoke" in response.json()["detail"].lower()
    assert db.get(GroundStation, station.id) is not None


def test_deleting_something_that_is_not_there_is_a_404(client):
    response = client.delete(f"/api/stations/{uuid.uuid4()}/config")
    assert response.status_code == 404
