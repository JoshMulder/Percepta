"""A station's identity, position and basemap extent.

Two rules carry most of the weight here, and both are about who owns a fact.

Position and name are settled at enrolment and frozen by it: a box that has
moved is recommissioned rather than edited, or its history silently describes
two different places and every bearing it ever reported becomes unattributable.

And whether a station's data is synthetic is not settable at all. The station
reports it per device and the ingest writes it from the health frame, so a
value typed into the console was overwritten by the box within half a minute —
a control that silently did nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.database.models.station_credential import StationCredential


def body(**overrides) -> dict:
    """A complete, valid update. The endpoint takes the whole object, so a test
    changing one field still has to send the rest."""
    payload = {
        "name": "Bench Station",
        "timezone": "Pacific/Auckland",
        "latitude": None,
        "longitude": None,
        "elevation_m": None,
        "map_min_zoom": 5,
        "map_max_zoom": 14,
        "map_radius_km": 25.0,
    }
    payload.update(overrides)
    return payload


def enrol(db, station, org) -> None:
    """Give the station a credential, which is what `_has_enrolled` reads."""
    db.add(StationCredential(
        id=uuid.uuid4(),
        organization_id=org.id,
        ground_station_id=station.id,
        secret_hash="not-a-real-hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    ))
    db.commit()


class TestPosition:
    """Where a station is, which until recently could not be set anywhere.

    The console showed it read-only and pointed at the station's own setup
    page; that page says coordinates are settled at commissioning and offers no
    field for them. Each side deferred to the other, so the fact went unset,
    with nowhere at all to record where a station is.
    """

    def test_it_can_be_set_before_enrolment(self, client, station):
        response = client.put(
            f"/api/stations/{station.id}/config",
            json=body(latitude=-43.48972, longitude=172.53194, elevation_m=37.0),
        )
        assert response.status_code == 200, response.text
        out = response.json()
        assert out["latitude"] == pytest.approx(-43.48972)
        assert out["longitude"] == pytest.approx(172.53194)
        assert out["elevation_m"] == pytest.approx(37.0)

    def test_it_is_frozen_once_the_station_can_authenticate(
        self, client, station, db, org
    ):
        client.put(
            f"/api/stations/{station.id}/config",
            json=body(latitude=-43.48972, longitude=172.53194),
        )
        enrol(db, station, org)

        response = client.put(
            f"/api/stations/{station.id}/config",
            json=body(latitude=-36.84846, longitude=174.76333),
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "latitude" in detail and "longitude" in detail
        assert "re-enrol" in detail.lower()

    def test_an_unchanged_position_is_not_a_change(self, client, station, db, org):
        """The freeze compares values, not intent. An enrolled station whose
        basemap radius is edited sends its position back unaltered, and
        refusing that would make every other setting on the pane unsaveable.
        """
        client.put(
            f"/api/stations/{station.id}/config",
            json=body(latitude=-43.48972, longitude=172.53194),
        )
        enrol(db, station, org)

        response = client.put(
            f"/api/stations/{station.id}/config",
            json=body(latitude=-43.48972, longitude=172.53194, map_radius_km=50.0),
        )
        assert response.status_code == 200, response.text
        assert response.json()["map_radius_km"] == pytest.approx(50.0)

    def test_a_typo_is_rejected_by_range_rather_than_stored(self, client, station):
        response = client.put(
            f"/api/stations/{station.id}/config", json=body(latitude=91.0),
        )
        assert response.status_code == 422


class TestEditability:
    """The form has to know which state the record is in.

    A field that looks editable and then 409s is a worse way to learn the rule
    than one that never offered, so the response says.
    """

    def test_a_fresh_record_reports_itself_editable(self, client, station):
        response = client.get(f"/api/stations/{station.id}/config")
        assert response.status_code == 200, response.text
        assert response.json()["enrolled"] is False

    def test_an_enrolled_one_does_not(self, client, station, db, org):
        enrol(db, station, org)
        response = client.get(f"/api/stations/{station.id}/config")
        assert response.json()["enrolled"] is True


class TestSyntheticIsNotSettable:
    """`is_simulated` is reported, never accepted.

    `_reconcile_simulated` writes it from the station's own health frame
    whenever it changes. A console that also set it was a second writer of one
    fact, and the loser: anything typed there was overwritten by the box within
    half a minute.
    """

    def test_it_is_still_reported(self, client, station):
        assert "is_simulated" in client.get(
            f"/api/stations/{station.id}/config"
        ).json()

    def test_sending_it_does_not_set_it(self, client, station, db):
        response = client.put(
            f"/api/stations/{station.id}/config", json=body(is_simulated=True),
        )
        # Accepted — an unknown field is ignored rather than rejected, so an
        # older console does not break — but not applied.
        assert response.status_code == 200, response.text
        assert response.json()["is_simulated"] is False
        db.refresh(station)
        assert station.is_simulated is False


class TestBasemapExtent:
    def test_a_minimum_above_the_maximum_is_refused(self, client, station):
        response = client.put(
            f"/api/stations/{station.id}/config",
            json=body(map_min_zoom=14, map_max_zoom=5),
        )
        assert response.status_code == 422

    def test_an_unknown_timezone_is_refused_here_not_on_the_station(
        self, client, station
    ):
        # A station's local time is derived from this. An unparseable zone would
        # otherwise surface as a wrong clock on a remote site.
        response = client.put(
            f"/api/stations/{station.id}/config", json=body(timezone="Mars/Olympus"),
        )
        assert response.status_code == 422
