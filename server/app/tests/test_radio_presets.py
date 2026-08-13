"""Airband presets, shared per station across the organisation.

These moved out of each browser's localStorage: the tower and ATIS frequencies
for a site belong to the site, not to whoever is looking at it. So the questions
worth asking are that a set saved by one caller is read back by the next, that
the shape the console indexes into is always exactly four slots however odd the
stored value is, and that a station in another tenancy is not reachable at all.
"""

from __future__ import annotations

import uuid

from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization

TOWER = {"hz": 118_300_000, "name": "Tower"}
GROUND = {"hz": 121_900_000, "name": "Ground"}


def url(station) -> str:
    return f"/api/stations/{station.id}/radio/presets"


class TestReading:
    def test_a_station_nobody_has_set_reads_as_four_empty_slots(self, client, station):
        """Always four, so the console can index the list directly rather than
        guarding every slot."""
        response = client.get(url(station))
        assert response.status_code == 200, response.text
        assert response.json() == [None, None, None, None]

    def test_another_tenants_station_is_not_available(self, client, db):
        other_org = Organization(id=uuid.uuid4(), name="Another Tenant")
        db.add(other_org)
        db.flush()
        other = GroundStation(
            id=uuid.uuid4(),
            organization_id=other_org.id,
            name="Somebody Else's",
            timezone="UTC",
        )
        db.add(other)
        db.commit()

        assert client.get(url(other)).status_code == 404
        assert client.put(url(other), json=[TOWER, None, None, None]).status_code == 404


class TestWriting:
    def test_a_saved_set_is_read_back(self, client, station):
        saved = client.put(url(station), json=[TOWER, None, GROUND, None])
        assert saved.status_code == 200, saved.text
        assert saved.json() == [TOWER, None, GROUND, None]
        # The point of the whole change: the next reader sees it, not just the
        # browser that saved it.
        assert client.get(url(station)).json() == [TOWER, None, GROUND, None]

    def test_it_is_stored_on_the_station_itself(self, client, station, db):
        client.put(url(station), json=[TOWER, None, None, None])
        db.rollback()
        assert db.get(GroundStation, station.id).radio_presets[0] == TOWER

    def test_the_whole_list_is_replaced(self, client, station):
        client.put(url(station), json=[TOWER, GROUND, None, None])
        response = client.put(url(station), json=[None, None, None, GROUND])
        assert response.json() == [None, None, None, GROUND]

    def test_more_slots_than_the_console_shows_are_trimmed(self, client, station):
        """An older build saved five. That is a truncation, not an error — an
        operator should not get a 422 because of a build they no longer run."""
        response = client.put(
            url(station), json=[TOWER, GROUND, TOWER, GROUND, TOWER],
        )
        assert response.status_code == 200, response.text
        assert len(response.json()) == 4

    def test_a_frequency_outside_airband_is_refused(self, client, station):
        assert client.put(
            url(station), json=[{"hz": 88_500_000, "name": "FM"}, None, None, None],
        ).status_code == 422

    def test_a_name_longer_than_the_button_is_refused(self, client, station):
        assert client.put(
            url(station), json=[{"hz": TOWER["hz"], "name": "x" * 40}, None, None, None],
        ).status_code == 422


class TestStoredValueIsNotTrusted:
    def test_junk_in_the_column_reads_as_empty_slots(self, client, station, db):
        """It is JSON in a database and the console indexes the result, so a
        value the endpoint never wrote must degrade rather than 500."""
        record = db.get(GroundStation, station.id)
        record.radio_presets = ["nonsense", {"hz": "not a number"}, {}, None, 7]
        db.add(record)
        db.commit()

        response = client.get(url(station))
        assert response.status_code == 200, response.text
        assert response.json() == [None, None, None, None]

    def test_an_out_of_band_stored_frequency_is_dropped(self, client, station, db):
        record = db.get(GroundStation, station.id)
        record.radio_presets = [{"hz": 5_000, "name": "bogus"}, TOWER, None, None]
        db.add(record)
        db.commit()

        assert client.get(url(station)).json() == [None, TOWER, None, None]
