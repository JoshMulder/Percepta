"""Where each aircraft has just been, so a trail is there the moment a map opens.

THE PROBLEM THIS SOLVES. ADS-B reaches the platform as a position list at 1 Hz
and nothing keeps the previous ones, so a console could only ever draw a trail it
had watched accumulate. Open the wall and every aircraft is a lone chevron with
no history; wait two minutes and the trails appear. The information existed the
whole time — the frames were arriving whether or not anybody had the page open —
it was simply being overwritten.

Built HERE, on the ingest, for exactly that reason: the leader already receives
every frame from every station regardless of viewers, so a trail costs no extra
station bytes, no extra link traffic, and nothing at all on the hardware. It is
assembled from data already paid for.

NOT A HISTORY, AND DELIBERATELY NOT PERSISTED. There is no ADS-B table in this
platform and this does not add one. A trail lives in Redis beside the snapshot it
belongs to, ages out on the same argument (`adsb_snapshot_key`: a fix from a
station that has since gone quiet must not linger on the map as traffic that is
no longer there), and is gone when the contact is. Anyone wanting replay,
"what flew over on Tuesday", or a heatmap is asking for a different feature with
its own table and its own retention decision — at roughly a hundred thousand rows
per busy site per day.

BOUNDED IN BOTH DIRECTIONS, because unbounded is how a cache becomes an outage:
`TRAIL_POINTS` caps how far back one aircraft is remembered, and a contact absent
for `CONTACT_GONE_AFTER` is dropped entirely rather than kept as a stale tail
somebody might read as current.
"""

from __future__ import annotations

import time

#: Positions kept per aircraft. At the station's 1 Hz that is about two minutes
#: of flight — long enough to read a turn or an approach at a glance, short
#: enough that twenty contacts stay a few tens of kilobytes.
#:
#: A trail is a shape, not a record. Doubling this would not make the picture
#: twice as useful; it would make the oldest half describe where an aircraft was
#: before the operator sat down.
TRAIL_POINTS = 120

#: How long a contact may go unheard before its trail is dropped.
#:
#: Longer than one frame on purpose — ADS-B drops individual messages constantly
#: and a gap of a few seconds is normal reception, not a departure. Shorter than
#: the snapshot TTL so a trail never outlives the fix it belongs to.
CONTACT_GONE_AFTER = 30.0

#: Below this, two fixes are the same place and the second is noise. Roughly ten
#: metres of latitude.
#:
#: Without it, a stationary transponder — a parked aircraft, a ground vehicle, a
#: test rig on a bench — writes 120 identical points and its "trail" is a dot
#: drawn a hundred and twenty times.
MIN_MOVE_DEG = 1e-4


def update(
    trails: dict[str, list[list[float]]],
    aircraft: list,
    *,
    now: float | None = None,
) -> dict[str, list[list[float]]]:
    """Fold one ADS-B frame into the trails, and return them.

    Mutates and returns the same dict: this runs on the ingest hot path, once
    per station per second, and rebuilding the structure per frame would be
    real work to achieve nothing.

    Each point is `[longitude, latitude, seen_at]` — LONGITUDE FIRST, because
    that is GeoJSON's order and the client feeds these straight into a
    LineString. Storing them the other way round means every consumer has to
    remember to swap, and one of them will not.
    """
    stamp = time.time() if now is None else now

    seen: set[str] = set()
    for contact in aircraft:
        if not isinstance(contact, dict):
            continue
        icao = contact.get("icao")
        lat, lon = contact.get("latitude"), contact.get("longitude")
        if not isinstance(icao, str):
            continue
        # The same guard the map's own code needs: a contact without a usable
        # position is not a contact at position zero. `isinstance` rather than a
        # truthiness test, because latitude 0 is a real place.
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue

        seen.add(icao)
        points = trails.setdefault(icao, [])
        if points:
            last = points[-1]
            if (
                abs(last[0] - float(lon)) < MIN_MOVE_DEG
                and abs(last[1] - float(lat)) < MIN_MOVE_DEG
            ):
                # Same place: refresh the age so the contact is not judged gone,
                # but do not lengthen a trail that is not going anywhere.
                last[2] = stamp
                continue
        points.append([float(lon), float(lat), stamp])
        if len(points) > TRAIL_POINTS:
            # Slice rather than pop(0) in a loop: one allocation, and the list
            # can only ever be one over.
            del points[: len(points) - TRAIL_POINTS]

    # Contacts that have stopped being heard. Dropped by AGE rather than by
    # absence from this one frame — ADS-B loses individual messages constantly,
    # and treating a single miss as a departure would clear a trail every few
    # seconds on a marginal receiver.
    for icao in [k for k in trails if k not in seen]:
        points = trails[icao]
        if not points or stamp - points[-1][2] > CONTACT_GONE_AFTER:
            del trails[icao]

    return trails


def geometry(points: list[list[float]]) -> list[list[float]]:
    """A trail as bare `[lon, lat]` pairs, for the wire and for GeoJSON.

    The timestamp is kept in Redis, because ageing needs it, and dropped here,
    because the client does not: it draws the shape. Sending it would inflate
    every trail by a third to carry something nothing reads.
    """
    return [[p[0], p[1]] for p in points]
