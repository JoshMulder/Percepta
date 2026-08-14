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
from backend.database.session import (
    PrivilegedSessionLocal,
    set_request_org_context,
)
from backend.services import alert_engine
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
                GroundStation.name,
            ).where(GroundStation.is_active.is_(True))
        ).all()
    return [
        (row.id, row.organization_id, row.last_seen_at, row.name) for row in rows
    ]


async def _scan(alerted: set[uuid.UUID]) -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=DARK_AFTER_SECONDS)
    rows = await asyncio.to_thread(_active_stations)

    dark: dict[uuid.UUID, tuple[uuid.UUID, datetime, str]] = {
        station_id: (organization_id, last_seen, name)
        for station_id, organization_id, last_seen, name in rows
        # A null last_seen_at is a station that has never once connected — a
        # provisioning state, not a death — so it is left to enrolment rather
        # than announced as gone dark.
        if last_seen is not None and last_seen < cutoff
    }

    for station_id, (organization_id, last_seen, name) in dark.items():
        minutes = int((now - last_seen).total_seconds() // 60)

        # Durable first. `open_or_touch` is idempotent on
        # (station, 'platform.station.dark'), so a station that has been dark
        # for a week produces ONE row whose occurrence count goes up, and a
        # restart of this process cannot re-announce it.
        #
        # That is the bug this replaces. The `alerted` set below lived in
        # memory: every deploy forgot it, and every currently-dark station was
        # announced afresh as though it had just died. On a platform redeployed
        # several times an afternoon, that is the alarm that teaches everybody
        # to ignore alarms.
        try:
            await asyncio.to_thread(
                _record_dark, organization_id, station_id, name, minutes
            )
        except Exception:  # noqa: BLE001 - never let alerting end the scan
            log.warning("Could not record a dark alert for %s.", station_id,
                        exc_info=True)

        if station_id in alerted:
            continue
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

    # Heard from again, or removed. The in-memory set still exists because it
    # is what suppresses the repeated LIVE announcement on the org channel;
    # the durable half is closed here so the rail stops showing a fault that
    # has ended.
    for station_id in [s for s in alerted if s not in dark]:
        alerted.discard(station_id)
        try:
            await asyncio.to_thread(_clear_dark, station_id)
        except Exception:  # noqa: BLE001
            log.warning("Could not close the dark alert for %s.", station_id,
                        exc_info=True)


def _record_dark(
    organization_id: uuid.UUID, station_id: uuid.UUID, name: str, minutes: int
) -> None:
    with PrivilegedSessionLocal() as db:
        set_request_org_context(db, organization_id=organization_id, bypass=True)
        alert_engine.on_dark(
            db,
            organization_id=organization_id,
            station_id=station_id,
            station_name=name,
            minutes=minutes,
        )
        db.commit()


def _clear_dark(station_id: uuid.UUID) -> None:
    with PrivilegedSessionLocal() as db:
        # The station's own organisation, looked up rather than passed in: a
        # station that has RECOVERED is by definition no longer in the dark map
        # the caller is iterating, so its org is not to hand. One extra query on
        # a rare path.
        #
        # Not None: set_request_org_context stringifies whatever it is given
        # into a `::uuid` cast, and "None" is not a uuid — the row-level policy
        # would error rather than fall through to the bypass clause.
        organization_id = db.execute(
            select(GroundStation.organization_id).where(
                GroundStation.id == station_id
            )
        ).scalar_one_or_none()
        if organization_id is None:
            return
        set_request_org_context(db, organization_id=organization_id, bypass=True)
        alert_engine.on_heard_again(db, station_id=station_id)
        db.commit()


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
