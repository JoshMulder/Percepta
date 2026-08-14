"""Announce a station that has gone dark — the silent death the platform must
notice on its own.

A station that loses its link *cleanly* is announced offline at once, from the
broker's own disconnect (api/broker.py), and the console paints its dot. But a
box that simply stops — power lost, or its credential lapsed with no renewal,
the enrolment design's "truck roll" case (contract/enrolment.md §6) — sends no
goodbye and produces no further event to announce. Its dot goes offline and then
nothing happens, on a station list an operator may not be looking at. The one
thing this platform exists to do is watch unattended sites, and a site going
quiet unnoticed is the failure that matters most.

So this watches every active station's `last_seen_at` (written a few times a
minute by station_ingest) and, once a station has been silent far past the
offline window, raises an alarm on the org-wide status channel — the same
channel the console already turns into a drawer entry. It fires **once per dark
spell**: raised when the station crosses the line, forgotten when it is heard
from again, so a box that recovers and later dies is announced afresh rather
than never (the console pushes an alert for every alarm it receives, so the
de-duplication has to live here).

Runs on the single-worker deployment the video relay already requires (see
start_app.py). On more than one worker each would scan and announce
independently — one more reason the platform runs one.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backend.database.models.ground_station import GroundStation
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.hub import hub
from backend.services.station_status import DARK_AFTER

log = logging.getLogger(__name__)

#: Silence past which a station is called dark. Well beyond the console's 120 s
#: offline window (api/stations.OFFLINE_AFTER_SECONDS), so an ordinary Starlink
#: obstruction dropout — the thing that window exists to tolerate — never trips
#: it. This is for a box that is not coming back on its own.
#: Kept as a name for this module's own readability; the value is the shared one
#: in services/station_status.py, so the alarm cannot drift from the tile and the
#: map marker describing the same station on the same screen.
DARK_AFTER_SECONDS = int(DARK_AFTER.total_seconds())

#: How often the scan runs. Coarse on purpose: a site going dark is a slow, rare
#: condition an operator does not need to the second, and each scan reads every
#: station.
POLL_SECONDS = 60


def _active_stations() -> list[tuple[uuid.UUID, uuid.UUID, datetime | None]]:
    # Privileged (owner) session: a platform-wide scan across every org, above
    # RLS — exactly like the ingest writer that fills last_seen_at in the first
    # place. Only the three columns the decision needs leave the database.
    with PrivilegedSessionLocal() as db:
        rows = db.execute(
            select(
                GroundStation.id,
                GroundStation.organization_id,
                GroundStation.last_seen_at,
            ).where(GroundStation.is_active.is_(True))
        ).all()
    return [(row.id, row.organization_id, row.last_seen_at) for row in rows]


async def _scan(alerted: set[uuid.UUID]) -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=DARK_AFTER_SECONDS)
    rows = await asyncio.to_thread(_active_stations)

    dark: dict[uuid.UUID, tuple[uuid.UUID, datetime]] = {
        station_id: (organization_id, last_seen)
        for station_id, organization_id, last_seen in rows
        # A null last_seen_at is a station that has never once connected — a
        # provisioning state, not a death — so it is left to enrolment rather
        # than announced as gone dark.
        if last_seen is not None and last_seen < cutoff
    }

    for station_id, (organization_id, last_seen) in dark.items():
        if station_id in alerted:
            continue
        minutes = int((now - last_seen).total_seconds() // 60)
        await hub.publish_status(
            organization_id,
            station_id,
            {
                "alarm": f"Gone dark — no contact for {minutes} min; the site may "
                "need a visit",
                "severity": "warning",
            },
        )
        alerted.add(station_id)
        log.warning(
            "Station %s has gone dark: %d min since last contact.",
            station_id, minutes,
        )

    # Forget any that have since been heard from (or been removed), so a station
    # that recovers and dies again is announced again rather than swallowed as a
    # duplicate.
    for station_id in [s for s in alerted if s not in dark]:
        alerted.discard(station_id)


async def watch() -> None:
    """Announce stations that have gone dark, once each, forever."""
    alerted: set[uuid.UUID] = set()
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            await _scan(alerted)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad scan must not end the loop and take every future dark
            # announcement down with it.
            log.exception("Station-dark scan failed; continuing.")
