"""A partial index for the notable-events feed

Odin's wall asks one question every poll, for every operator on shift: what are
the most recent warnings and criticals across the whole fleet? Today that walks
`ix_station_events_received_at` backwards through a table whose overwhelming
majority is `info` — measured on the live deployment, 291 `radio.transmission`
rows in 24 hours against 71 warnings — discarding almost everything it reads to
find twenty rows.

The cost is invisible at three stations and grows with TRAFFIC rather than with
fleet size, which is the bad direction: a busy airband circuit degrades a query
about station health. A partial index over just the notable severities is a few
hundred rows where the table is hundreds of thousands, and it stays that way.

`severity` is in the predicate rather than the key because it is the filter, and
`received_at DESC` matches the order the feed reads in, so the planner can walk
the index and stop at twenty.

Concurrently, and hence the autocommit block: this table is written by ingest on
every telemetry event, and a plain CREATE INDEX takes a lock that would stall
that write path on a deployment with real history.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-14

"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_station_events_notable"


def upgrade() -> None:
    # CONCURRENTLY cannot run inside a transaction, and alembic wraps migrations
    # in one by default.
    with op.get_context().autocommit_block():
        op.execute(
            f"""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
            ON station_events (received_at DESC)
            WHERE severity IN ('warning', 'critical')
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
