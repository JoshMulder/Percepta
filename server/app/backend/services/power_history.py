"""Persist a downsample of power telemetry, so the battery chart can look back.

The console used to buffer state of charge in the browser. That reset on every
reload and could never reach further back than the moment the tab was opened,
which makes a 12-hour or 7-day view meaningless. This keeps one row per station
per minute instead.

It subscribes to the fan-out bus rather than sitting in the ingest path, because
the ingest path does not exist yet - stations publish straight onto the bus. When
a real enrolment and ingest path lands, this should move behind it, and the
subscription here becomes the thing to delete.

Deliberately its own Redis connection, and a pattern subscription: the hub only
subscribes to groups that have a live viewer, and history has to be recorded
whether or not anyone happens to be watching.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy.dialects.postgresql import insert

from backend.core.config import settings
from backend.database.models.power_sample import PowerSample
from backend.database.session import PrivilegedSessionLocal

log = logging.getLogger(__name__)

#: Samples older than this are dropped. A week of history plus a day of slack,
#: so the 7-day window is always fully covered.
RETENTION = timedelta(days=8)
PRUNE_EVERY = timedelta(hours=6)


def _minute(when: datetime) -> datetime:
    return when.replace(second=0, microsecond=0)


class PowerHistory:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._redis: aioredis.Redis | None = None
        self._last_prune = datetime.now(UTC)
        # Last minute written per station, so a 1 Hz stream does not attempt
        # sixty inserts a minute only for fifty-nine to be discarded.
        self._written: dict[uuid.UUID, datetime] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    async def _run(self) -> None:
        while True:
            try:
                self._redis = aioredis.Redis.from_url(settings.redis_url)
                pubsub = self._redis.pubsub()
                await pubsub.psubscribe("rt:g:*")
                log.info("Power history recorder started.")
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=5.0
                    )
                    if message is not None:
                        await self._handle(message)
                    await self._maybe_prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Power history recorder failed; retrying in 5s.")
                await asyncio.sleep(5)

    async def _handle(self, message: dict) -> None:
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode()
        if not isinstance(data, str):
            return
        try:
            frame = json.loads(data)
        except ValueError:
            return

        payload = frame.get("payload") or {}
        if payload.get("kind") != "power":
            return
        try:
            station_id = uuid.UUID(str(frame["station_id"]))
        except (KeyError, TypeError, ValueError):
            return

        minute = _minute(datetime.now(UTC))
        if self._written.get(station_id) == minute:
            return
        self._written[station_id] = minute

        await asyncio.to_thread(self._write, station_id, minute, payload)

    def _write(self, station_id: uuid.UUID, minute: datetime, payload: dict) -> None:
        try:
            with PrivilegedSessionLocal() as db:
                # The station's org comes from the registry, never from the
                # payload - same rule as everywhere else a station is trusted.
                from sqlalchemy import select

                from backend.database.models.ground_station import GroundStation

                org_id = db.execute(
                    select(GroundStation.organization_id).where(
                        GroundStation.id == station_id
                    )
                ).scalar_one_or_none()
                if org_id is None:
                    return

                stmt = (
                    insert(PowerSample)
                    .values(
                        id=uuid.uuid4(),
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                        organization_id=org_id,
                        ground_station_id=station_id,
                        at=minute,
                        soc_pct=float(payload.get("soc_pct", 0.0)),
                        battery_v=payload.get("battery_v"),
                        pv_w=payload.get("pv_w"),
                        load_w=payload.get("load_w"),
                    )
                    # Two workers may both see the same minute; the first wins
                    # and the second is a no-op rather than an error.
                    .on_conflict_do_nothing(
                        constraint="uq_power_sample_station_minute"
                    )
                )
                db.execute(stmt)
                db.commit()
        except Exception:
            log.exception("Could not record a power sample for %s.", station_id)

    async def _maybe_prune(self) -> None:
        now = datetime.now(UTC)
        if now - self._last_prune < PRUNE_EVERY:
            return
        self._last_prune = now
        await asyncio.to_thread(self._prune, now - RETENTION)

    def _prune(self, before: datetime) -> None:
        try:
            from sqlalchemy import delete

            with PrivilegedSessionLocal() as db:
                db.execute(delete(PowerSample).where(PowerSample.at < before))
                db.commit()
        except Exception:
            log.exception("Power history prune failed.")


power_history = PowerHistory()
