"""Persist a downsample of weather telemetry, so the trend charts can look back.

The twin of `power_history.py`, for the weather stream: one row per station per
minute, recorded off the fan-out bus whether or not anyone is watching. See that
module's header for why it sits here rather than behind the ingest, why it holds
its own Redis connection, and why the pattern is `telemetry`, not `weather`.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy.dialects.postgresql import insert

from backend.core.config import settings
from backend.database.models.weather_sample import WeatherSample
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.bus import group_channel
from backend.realtime.groups import station_group_pattern

log = logging.getLogger(__name__)

#: A week plus a day of slack, matching the power recorder so the two windows
#: line up.
RETENTION = timedelta(days=8)
PRUNE_EVERY = timedelta(hours=6)

TELEMETRY_PATTERN = group_channel(station_group_pattern(stream="telemetry"))


def _minute(when: datetime) -> datetime:
    return when.replace(second=0, microsecond=0)


class WeatherHistory:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._redis: aioredis.Redis | None = None
        self._last_prune = datetime.now(UTC)
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
                await pubsub.psubscribe(TELEMETRY_PATTERN)
                log.info("Weather history recorder started.")
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
                log.exception("Weather history recorder failed; retrying in 5s.")
                await asyncio.sleep(5)

    async def _handle(self, message: dict) -> None:
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode()
        if not isinstance(data, str):
            return
        # Cheaper than parsing to find out — weather is one kind in five, so most
        # frames are rejected on a substring test. The parsed check below is the
        # authoritative one.
        if '"kind":"weather"' not in data and '"kind": "weather"' not in data:
            return
        try:
            frame = json.loads(data)
        except ValueError:
            return

        payload = frame.get("payload") or {}
        if payload.get("kind") != "weather":
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
                    insert(WeatherSample)
                    .values(
                        id=uuid.uuid4(),
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                        organization_id=org_id,
                        ground_station_id=station_id,
                        at=minute,
                        # `.get` leaves an unfitted sensor None, which is what the
                        # chart wants — a sensor that is not there, not a zero.
                        temperature_c=payload.get("temperature_c"),
                        humidity_pct=payload.get("humidity_pct"),
                        pressure_hpa=payload.get("pressure_hpa"),
                        wind_kt=payload.get("wind_kt"),
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_weather_sample_station_minute"
                    )
                )
                db.execute(stmt)
                db.commit()
        except Exception:
            log.exception("Could not record a weather sample for %s.", station_id)

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
                db.execute(delete(WeatherSample).where(WeatherSample.at < before))
                db.commit()
        except Exception:
            log.exception("Weather history prune failed.")


weather_history = WeatherHistory()
