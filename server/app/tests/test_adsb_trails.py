"""Trails: where each aircraft has just been, so a map opens with tracks drawn.

The frames were always arriving — the ingest receives ADS-B at 1 Hz from every
station whether or not anybody has a page open — and the previous positions were
simply overwritten. So a console could only draw a trail it had watched
accumulate: open the wall, see lone chevrons, wait two minutes for tails.

What is pinned here is mostly the BOUNDS. This is a cache on the ingest hot path
that grows with airspace activity, and every one of these tests is really the
same question: can it grow without end?
"""

from __future__ import annotations

from backend.services.adsb_trails import (
    CONTACT_GONE_AFTER,
    TRAIL_POINTS,
    geometry,
    update,
)


def contact(icao="C81234", lat=-43.5, lon=172.6):
    return {"icao": icao, "latitude": lat, "longitude": lon}


def test_a_trail_accumulates_in_geojson_order():
    trails: dict = {}
    update(trails, [contact(lon=172.6)], now=1.0)
    update(trails, [contact(lon=172.7)], now=2.0)
    # [longitude, latitude] — GeoJSON's order, so the client can feed these
    # straight into a LineString. Storing them the other way means every
    # consumer has to remember to swap and one of them will not.
    assert geometry(trails["C81234"]) == [[172.6, -43.5], [172.7, -43.5]]


def test_a_trail_is_capped():
    trails: dict = {}
    for i in range(TRAIL_POINTS + 50):
        update(trails, [contact(lon=172.6 + i * 0.01)], now=float(i))
    assert len(trails["C81234"]) == TRAIL_POINTS
    # The OLDEST are dropped: a trail describes where an aircraft is going, and
    # keeping the first points would describe where it was before anybody looked.
    assert geometry(trails["C81234"])[-1][0] > geometry(trails["C81234"])[0][0]


def test_a_stationary_transponder_does_not_grow_a_trail():
    """A parked aircraft, a ground vehicle, a test rig on a bench.

    Without the movement threshold each writes TRAIL_POINTS identical points and
    its "trail" is a dot drawn a hundred and twenty times — the most expensive
    way to render nothing.
    """
    trails: dict = {}
    for i in range(50):
        update(trails, [contact()], now=float(i))
    assert len(trails["C81234"]) == 1


def test_a_contact_gone_quiet_is_dropped():
    trails: dict = {}
    update(trails, [contact()], now=0.0)
    update(trails, [], now=CONTACT_GONE_AFTER + 1.0)
    assert "C81234" not in trails


def test_a_single_missed_frame_does_not_clear_a_trail():
    """ADS-B loses individual messages constantly.

    Dropping on absence from one frame rather than on age would clear every
    trail on the map every few seconds on a marginal receiver — and look like
    the feature simply not working.
    """
    trails: dict = {}
    update(trails, [contact(lon=172.6)], now=0.0)
    update(trails, [], now=1.0)
    update(trails, [contact(lon=172.7)], now=2.0)
    assert len(trails["C81234"]) == 2


def test_a_contact_with_no_position_is_ignored():
    # The crash this guards: a missing coordinate reaching MapLibre as NaN threw
    # "Invalid LngLat object" and took the whole wall down.
    trails: dict = {}
    update(trails, [{"icao": "C81234"}], now=0.0)
    update(trails, [{"icao": "C81234", "latitude": None, "longitude": 172.6}], now=1.0)
    assert trails == {}


def test_latitude_zero_is_a_real_place():
    trails: dict = {}
    update(trails, [contact(lat=0.0, lon=0.0)], now=0.0)
    assert trails["C81234"] == [[0.0, 0.0, 0.0]]


def test_several_aircraft_are_kept_apart():
    trails: dict = {}
    update(trails, [contact("AAA", lon=1.0), contact("BBB", lon=2.0)], now=0.0)
    update(trails, [contact("AAA", lon=1.1), contact("BBB", lon=2.1)], now=1.0)
    assert geometry(trails["AAA"]) == [[1.0, -43.5], [1.1, -43.5]]
    assert geometry(trails["BBB"]) == [[2.0, -43.5], [2.1, -43.5]]


def test_malformed_entries_cost_themselves_and_nothing_else():
    trails: dict = {}
    update(trails, ["not a dict", None, {"no": "icao"}, contact()], now=0.0)
    assert set(trails) == {"C81234"}


def test_the_timestamp_is_not_sent_to_the_client():
    # Kept in Redis because ageing needs it; dropped on the wire because the
    # client draws a shape. Sending it would inflate every trail by a third to
    # carry something nothing reads.
    trails: dict = {}
    update(trails, [contact()], now=123.0)
    assert all(len(p) == 2 for p in geometry(trails["C81234"]))
