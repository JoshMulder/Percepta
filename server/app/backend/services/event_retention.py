"""The station event ledger is the only history table with no retention.

`power_samples` (0005) and `weather_samples` (0014) have been pruned since the
day they were added. `station_events` never was, and it is the table that grows
fastest and least predictably, because its contents are decided by what stations
choose to report rather than by a fixed sample rate.

WHAT IS ACTUALLY IN IT, measured on the live fleet rather than assumed:

    video.stream_started   1729   info      \\
    video.stream_stopped   1717   info      /  79% of every row, together
    radio.transmission      572   info         the transcripts
    adsb.proximity          181   warning
    uplink.up                94   info
    uplink.down              65   warning

94% of rows are `info`. Worth stating because the design expected the
transcripts to be the growth and they are not — the growth is video stream
lifecycle, a pair of rows every time anybody opens or closes a camera, and it
scales with how much the console is USED rather than with the fleet. A wall that
is watched harder writes more history, which is the wrong way round for a table
nobody prunes.

TWO HORIZONS, NOT ONE.

    90 days   info rows, except platform.*
    400 days  everything else

The split is about what the rows are FOR. An info row is an account of normal
operation and is worth having while somebody might still ask "what happened last
night". A warning or critical row is evidence, and evidence outlives the shift
that produced it — 400 days covers a full annual cycle plus the argument
afterwards. `platform.*` rows record what WE did to a customer's system, so they
keep the long horizon whatever their severity.

The two deletes are independent and their order does not affect what survives: a
warning aged 200 days is caught by neither, a warning aged 500 days by the
second, an info aged 100 days by the first. Order is chosen for cost, not
correctness.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from backend.database.models.station_event import StationEvent
from backend.database.session import PrivilegedSessionLocal

log = logging.getLogger(__name__)

#: Ordinary operation. Long enough to answer "what happened last night" a
#: season later, short enough that the video-stream churn above does not
#: accumulate for ever.
INFO_RETENTION = timedelta(days=90)

#: Evidence. A full annual cycle plus the argument afterwards.
LONG_RETENTION = timedelta(days=400)

PRUNE_EVERY = timedelta(hours=6)

#: Deliberately NOT six hours after boot.
#:
#: power_history stamps `_last_prune` in its constructor, so its first prune is
#: a full interval after start — which on any box that is redeployed more often
#: than that means the prune has never once run. This platform is redeployed
#: several times a day. A short delay instead: long enough to stay out of the
#: way of start-up, short enough that a prune actually happens.
FIRST_PRUNE_AFTER = timedelta(minutes=5)

#: Rows per DELETE. The ledger is on the ingest's write path, so a single
#: unbounded delete of a year's backlog would hold locks long enough to stall
#: telemetry across the fleet. Chunked, the prune is interruptible and each
#: statement is short; it simply takes several passes the first time.
BATCH = 10_000

#: Safety stop. Bounds one prune cycle rather than trusting the loop condition —
#: a predicate bug that matched everything would otherwise delete the ledger in
#: one pass, quietly and completely.
MAX_BATCHES = 200


class EventRetention:
    """Prunes station_events on a timer. Follows power_history's shape."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # `None`, not `now()`. See FIRST_PRUNE_AFTER.
        self._last_prune: datetime | None = None
        self._started_at = datetime.now(UTC)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        # Said out loud, with the horizons in it, because every other recorder
        # announces itself and because "is the prune actually running?" is the
        # question somebody asks in six months when the table looks large. A
        # service whose only evidence of life is the absence of old rows cannot
        # be distinguished from one that never started.
        log.info(
            "Event retention started: info %d days, everything else %d days, "
            "first pass in %d minutes.",
            INFO_RETENTION.days,
            LONG_RETENTION.days,
            FIRST_PRUNE_AFTER.total_seconds() // 60,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._maybe_prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never lets one failure end the loop: a prune that cannot run
                # tonight must still run tomorrow, and the table it guards is
                # the one that grows without bound.
                log.exception("Event retention cycle failed.")
            await asyncio.sleep(60)

    async def _maybe_prune(self) -> None:
        now = datetime.now(UTC)
        if self._last_prune is None:
            if now - self._started_at < FIRST_PRUNE_AFTER:
                return
        elif now - self._last_prune < PRUNE_EVERY:
            return
        self._last_prune = now
        await asyncio.to_thread(self._prune, now)

    def _prune(self, now: datetime) -> None:
        """Both horizons, batched. Runs on a worker thread: Session is sync.

        NO LEASE, unlike the ingest. A delete is idempotent and publishes
        nothing, so a second worker running the same prune finds the rows
        already gone and does no harm — which is not true of the ingest, where
        two leaders would put two frames per tick on the wall channel. Taking a
        lease here would add a failure mode (a stale lease stops the prune
        entirely) to buy nothing.
        """
        removed = 0
        try:
            with PrivilegedSessionLocal() as db:
                # RLS BYPASS, SET EXPLICITLY.
                #
                # station_events is ENABLE + FORCE ROW LEVEL SECURITY (0015),
                # and FORCE means even the table's owner is subject to the
                # policy. This works today only because the privileged role
                # happens to be the image's bootstrap SUPERUSER, and a superuser
                # is exempt even under FORCE (scripts/verify_rls.py says so in
                # as many words). De-superusing that role is an obvious future
                # hardening — and the failure it would cause here is the worst
                # kind: the DELETE matches nothing, commits cleanly, logs
                # success, and the table grows for ever.
                #
                # One statement, and the doubt is gone.
                db.execute(
                    text(
                        "SELECT set_config('app.current_org', :org, true), "
                        "set_config('app.bypass', 'on', true)"
                    ),
                    {"org": str(uuid.UUID(int=0))},
                )

                removed += self._delete_batched(
                    db,
                    "received_at < :before",
                    {"before": now - LONG_RETENTION},
                    what="everything past the long horizon",
                )
                removed += self._delete_batched(
                    db,
                    "received_at < :before AND severity = 'info' "
                    "AND type NOT LIKE 'platform.%'",
                    {"before": now - INFO_RETENTION},
                    what="info rows past the short horizon",
                )
        except Exception:
            log.exception("Station event prune failed.")
            return

        # Logged with a NUMBER, not "prune complete". Zero rows is the exact
        # symptom of the RLS failure above, and a message that cannot tell zero
        # from success is a message that would have hidden it.
        if removed:
            log.info("Event retention removed %d station_events rows.", removed)
        else:
            log.debug("Event retention: nothing past either horizon.")

    @staticmethod
    def _delete_batched(db, where: str, params: dict, *, what: str) -> int:
        """DELETE ... WHERE ctid IN (SELECT ... LIMIT n), until it stops matching."""
        table = StationEvent.__tablename__
        statement = text(
            f"DELETE FROM {table} WHERE ctid IN ("
            f"  SELECT ctid FROM {table} WHERE {where} LIMIT {BATCH}"
            f")"
        )
        removed = 0
        for attempt in range(MAX_BATCHES):
            result = db.execute(statement, params)
            db.commit()
            count = result.rowcount or 0
            removed += count
            if count < BATCH:
                return removed
            if attempt == MAX_BATCHES - 1:
                # Not silent. Stopping early is correct — the next cycle picks
                # up where this one left off — but a backlog that needs two
                # million deletions is worth somebody knowing about.
                log.warning(
                    "Event retention hit its batch ceiling pruning %s; "
                    "%d rows removed, more remain.",
                    what,
                    removed,
                )
        return removed


event_retention = EventRetention()
