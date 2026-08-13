"""A station's identity, position and basemap extent.

Three rules carry most of the weight here, and all of them are about who owns a
fact.

**Position** is settled at enrolment and frozen by it: a box that has moved is
recommissioned rather than edited, or its history silently describes two
different places and every bearing it ever reported becomes unattributable.

**Name and timezone** are the platform's to set, and are deliberately *not*
frozen. They reach the box in its enrolment record, so an edit here is followed
by a `config.refresh` nudge that makes the station adopt it now rather than at
its next scheduled renewal — one owner, and the two ends stay in step without a
re-enrolment.

And **whether a station's data is synthetic** is not settable at all. The
station reports it per device and the ingest writes it from the health frame, so
a value typed into the console was overwritten by the box within half a minute —
a control that silently did nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

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


class TestNamePushesThroughToTheStation:
    """A rename used to be refused after enrolment, which left the platform's
    label and the box's own name free to disagree with no way to reconcile them
    short of re-enrolling. Both live in the enrolment record, so the platform
    owns them and a `config.refresh` makes the box adopt the change now.
    """

    def dispatches(self):
        """Capture the command channel. `publish_roster_sync` is mocked with it
        because a rename also nudges the consoles, and that is not what these
        are about."""
        return (
            mock.patch("backend.api.station_config.publish_sync", return_value=True),
            mock.patch("backend.api.station_config.publish_roster_sync"),
        )

    def test_an_enrolled_station_can_be_renamed(self, client, station, db, org):
        enrol(db, station, org)
        publish, roster = self.dispatches()
        with publish, roster:
            response = client.put(
                f"/api/stations/{station.id}/config", json=body(name="Kennels Road"),
            )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Kennels Road"

    def test_a_rename_nudges_the_station_to_refresh(self, client, station, db, org):
        enrol(db, station, org)
        publish, roster = self.dispatches()
        with publish as pub, roster:
            client.put(
                f"/api/stations/{station.id}/config", json=body(name="Kennels Road"),
            )
        _, command = pub.call_args.args
        assert command == {"kind": "config.refresh"}

    def test_a_timezone_change_nudges_it_too(self, client, station, db, org):
        enrol(db, station, org)
        publish, roster = self.dispatches()
        with publish as pub, roster:
            client.put(
                f"/api/stations/{station.id}/config", json=body(timezone="UTC"),
            )
        _, command = pub.call_args.args
        assert command == {"kind": "config.refresh"}

    def test_an_unenrolled_station_is_not_nudged(self, client, station):
        """There is no box to tell yet — it will read the record when it enrols."""
        publish, roster = self.dispatches()
        with publish as pub, roster:
            client.put(
                f"/api/stations/{station.id}/config", json=body(name="Not Yet Built"),
            )
        pub.assert_not_called()

    def test_an_unrelated_edit_does_not_nudge(self, client, station, db, org):
        """The basemap extent is the platform's alone; the box has no copy of it
        to bring into step, and a renewal per zoom edit is noise."""
        enrol(db, station, org)
        publish, roster = self.dispatches()
        with publish as pub, roster:
            client.put(
                f"/api/stations/{station.id}/config", json=body(map_radius_km=50.0),
            )
        pub.assert_not_called()

    def test_an_offline_station_does_not_fail_the_save(
        self, client, station, db, org
    ):
        """The nudge is an optimisation, not the mechanism: the scheduled
        renewal still delivers the change. A box that cannot be reached must not
        turn a saved edit into an error."""
        enrol(db, station, org)
        with mock.patch(
            "backend.api.station_config.publish_sync", return_value=False
        ), mock.patch("backend.api.station_config.publish_roster_sync"):
            response = client.put(
                f"/api/stations/{station.id}/config", json=body(name="Off The Air"),
            )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Off The Air"
