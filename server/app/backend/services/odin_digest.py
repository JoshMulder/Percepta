"""One fleet snapshot, computed once, pushed to every wall.

The wall used to poll: every operator asked for the whole fleet every fifteen
seconds, and the server rebuilt it — a database query, a bulk Redis read and a
projection — once per operator per poll. That is O(operators x stations) of work
to answer a question whose answer is identical for all of them, and it gets worse
in the direction the product is meant to grow.

Here the vitals are kept in a plain dict, updated as frames arrive on the ingest
path that already carries every frame from every station, and a separate task
publishes the whole thing every few seconds. Cost is O(1) per operator and O(1)
frames per interval regardless of fleet size.

TWO RULES, and both matter more than they look.

FIRST: the update on the hot path is a dict assignment and nothing else. No
serialisation, no I/O, no await that can block. That path already carries every
telemetry frame from every station through one asyncio loop, and that loop — not
bandwidth — is the honest ceiling on how many stations this platform holds. The
join, the roster read and the JSON encoding all happen in the publisher task,
where being slow costs a late frame rather than backing up ingest for everyone.

SECOND: this runs in the ingest leader, so its single-instance property comes
from the Redis lease that already elects one ingest (services/station_ingest.py)
rather than from a new assumption nobody has tested. Two publishers would put two
frames on the channel every interval and every wall would render the fleet twice
per tick — a failure that looks like flapping data rather than like duplicate
processes, which is exactly the sort that gets diagnosed slowly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.bus import publish_sync
from backend.realtime.odin import WALL_CHANNEL
from backend.services.station_status import status_for

log = logging.getLogger(__name__)

#: How often a frame goes out. Not how often data arrives — stations report far
#: more often than this, and the wall does not want every one of them. Three
#: seconds is well inside the poll it replaces and well outside the rate at
#: which a wall watched from across a room can show anything.
PUBLISH_SECONDS = 3.0

#: The roster (which stations exist, what they are called, who owns them) moves
#: on human timescales — a station is enrolled or renamed, not several times a
#: minute — so it is read on its own slow cycle rather than every frame.
ROSTER_SECONDS = 30.0


class OdinDigest:
    """Per-station vitals, kept current by the ingest and published in one lump."""

    def __init__(self) -> None:
        #: station_id -> the latest vitals seen. Written on the hot path, read
        #: by the publisher. No lock: both run on the same event loop, and a
        #: dict assignment is not interruptible by it.
        self._vitals: dict[uuid.UUID, dict[str, Any]] = {}
        #: station_id -> the identity half of a FleetStation: everything the
        #: wall needs that does not come from telemetry. It must carry the SAME
        #: field set the polled endpoint returns, because the client swaps
        #: between the two sources and a field present in one and absent in the
        #: other is a crash rather than a gap.
        self._roster: dict[uuid.UUID, dict] = {}
        #: station_id -> last_seen, so the digest can derive liveness without
        #: asking the database for it every frame.
        self._seen: dict[uuid.UUID, datetime] = {}
        self._task: asyncio.Task | None = None
        self._roster_task: asyncio.Task | None = None
        self._running = False

    # --- the hot path ----------------------------------------------------

    def note(self, station_id: uuid.UUID, kind: str, payload: dict) -> None:
        """Record what this frame says. Called from the ingest, per frame.

        Deliberately synchronous and deliberately trivial: dict lookups, a few
        `.get`s and one assignment. Anything that could await belongs in the
        publisher, because this runs once per frame per station and a stall here
        is a stall in everybody's telemetry.
        """
        self._seen[station_id] = datetime.now(UTC)
        v = self._vitals.setdefault(station_id, {})

        if kind == "health":
            status = payload.get("status")
            v["health"] = status if isinstance(status, str) else None
            conditions = payload.get("conditions")
            v["condition_count"] = len(conditions) if isinstance(conditions, list) else 0
            uplink = payload.get("uplink")
            if isinstance(uplink, dict):
                connected = uplink.get("connected")
                v["uplink_connected"] = connected if isinstance(connected, bool) else None
            devices = payload.get("devices")
            if isinstance(devices, list):
                slots: dict[str, str] = {}
                simulated: list[str] = []
                for d in devices:
                    if not isinstance(d, dict):
                        continue
                    slot = d.get("slot")
                    if not isinstance(slot, str):
                        continue
                    state = d.get("status")
                    slots[slot] = state if isinstance(state, str) else "unknown"
                    if d.get("simulated") is True:
                        simulated.append(slot)
                v["slots"] = slots
                v["simulated_slots"] = simulated
        elif kind == "power":
            soc = payload.get("soc_pct")
            v["soc_pct"] = float(soc) if isinstance(soc, (int, float)) else None
            load = payload.get("load_w")
            v["load_w"] = float(load) if isinstance(load, (int, float)) else None
            mains, gen = payload.get("mains_w"), payload.get("generator_w")
            if isinstance(mains, (int, float)) or isinstance(gen, (int, float)):
                v["on_battery"] = (mains or 0) <= 0 and (gen or 0) <= 0

    def forget(self, station_id: uuid.UUID) -> None:
        self._vitals.pop(station_id, None)
        self._seen.pop(station_id, None)

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._refresh_roster()
        self._task = asyncio.create_task(self._publish_loop())
        self._roster_task = asyncio.create_task(self._roster_loop())
        log.info("Odin digest publishing every %.0fs.", PUBLISH_SECONDS)

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._roster_task):
            if task is not None:
                task.cancel()
        self._task = self._roster_task = None

    # --- the slow paths --------------------------------------------------

    def _read_roster(self) -> dict[uuid.UUID, dict]:
        with PrivilegedSessionLocal() as db:
            rows = db.execute(
                select(
                    GroundStation.id,
                    GroundStation.name,
                    GroundStation.organization_id,
                    Organization.name,
                    GroundStation.is_simulated,
                    GroundStation.last_seen_at,
                    # Position, and it is not optional decoration: the fleet map
                    # places a marker per station and a missing coordinate
                    # becomes NaN rather than "no position". Leaving these out
                    # of the digest crashed the whole platform view the moment
                    # the wall switched from the poll to the push.
                    GroundStation.latitude,
                    GroundStation.longitude,
                    GroundStation.locality,
                    GroundStation.region,
                    GroundStation.config_version,
                )
                .join(Organization, GroundStation.organization_id == Organization.id)
                .where(
                    GroundStation.is_active.is_(True), Organization.is_active.is_(True)
                )
            ).all()
        out: dict[uuid.UUID, dict] = {}
        for (
            sid, name, org_id, org_name, simulated, last_seen,
            lat, lon, locality, region, config_version,
        ) in rows:
            out[sid] = {
                "name": name,
                "organization_id": str(org_id),
                "organization_name": org_name,
                "is_simulated": bool(simulated),
                "latitude": lat,
                "longitude": lon,
                "locality": locality,
                "region": region,
                "model": None,
                "config_version": config_version,
            }
            # Seed liveness from the database for stations that have not sent a
            # frame since this process started. Without it a wall that comes up
            # after a restart shows every quiet station as "never seen" until it
            # next reports, which for a station that has gone dark is forever.
            if last_seen is not None and sid not in self._seen:
                self._seen[sid] = last_seen
        return out

    async def _refresh_roster(self) -> None:
        try:
            self._roster = await asyncio.to_thread(self._read_roster)
        except Exception:
            # Keeping the previous roster is right: a database blip should cost
            # a rename going unnoticed for thirty seconds, not the whole wall.
            log.warning("Odin digest could not refresh its roster.", exc_info=True)

    async def _roster_loop(self) -> None:
        while self._running:
            await asyncio.sleep(ROSTER_SECONDS)
            await self._refresh_roster()

    async def _publish_loop(self) -> None:
        while self._running:
            await asyncio.sleep(PUBLISH_SECONDS)
            try:
                await self._publish()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Odin digest failed to publish a frame.")

    def _frame(self) -> dict:
        now = datetime.now(UTC)
        stations = []
        for sid, identity in self._roster.items():
            status, dark = status_for(self._seen.get(sid), now=now)
            last_seen = self._seen.get(sid)
            stations.append({
                "id": str(sid),
                **identity,
                "status": status,
                "dark": dark,
                "last_seen_at": last_seen.isoformat() if last_seen else None,
                **self._vitals.get(sid, {}),
            })
        return {
            "type": "odin.digest",
            "at": now.isoformat(),
            "stations": stations,
        }

    async def _publish(self) -> None:
        """Build the frame here, put it on the wire on a worker thread.

        publish_sync rather than reaching into the async bus's private client:
        it is the documented way to put a payload on a raw channel, it already
        fails soft when Redis is absent, and the encode plus the round trip is
        exactly the kind of work that belongs off this loop — which is the same
        loop the whole fleet's ingest runs on.
        """
        frame = self._frame()
        await asyncio.to_thread(publish_sync, WALL_CHANNEL, frame)


digest = OdinDigest()
