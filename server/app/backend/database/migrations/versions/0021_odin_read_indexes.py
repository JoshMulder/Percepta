"""Indexes for the two ODIN read surfaces added in phase 5

Both new browsers page with a COMPOUND cursor — `(timestamp, id)` — because
neither timestamp is unique. Station events are stamped once per arriving batch
of up to a hundred, and audit rows are written several to a request. A bare
timestamp cursor silently drops rows on `<` and repeats them for ever on `<=`,
and neither shows up on a bench box sending one thing at a time: it needs a real
site reconnecting with a backlog. The index has to match the cursor or Postgres
sorts the whole filtered set to answer a page of a hundred.

`audit_logs` has never had an index on `created_at` at all (0001). That was
tolerable while nothing read the table — and nothing did, for nineteen
migrations. `audit.record` is on the login path, so this stays cheap: two
b-trees on an append-only table whose writes are already doing more work than
one index entry.

The station_events index is the smaller win and is included because the event
browser's commonest question by far is "this one station, this window", which
today walks a fleet-wide `received_at` index discarding everybody else's rows.

CONCURRENTLY, hence the autocommit block — the same reason 0019 gives. Both
tables are on live write paths (ingest for one, every login for the other) and a
plain CREATE INDEX takes a lock that would stall them on a deployment with real
history.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-14

"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


AUDIT_ORG_INDEX = "ix_audit_logs_org_created"
AUDIT_CREATED_INDEX = "ix_audit_logs_created"
EVENTS_STATION_INDEX = "ix_station_events_station_received"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # The scoped read: one organisation's history, newest first. Leading on
        # organization_id because that is the predicate the whole API is built
        # around — audit_logs has NO row-level security, so the org filter is
        # written by hand on every query and is therefore always present.
        op.execute(
            f"""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS {AUDIT_ORG_INDEX}
            ON audit_logs (organization_id, created_at DESC, id DESC)
            """
        )
        # The unscoped read: a platform administrator looking across every
        # tenant, which is the default when no organisation is named. Separate
        # rather than relying on the composite above — a leading equality column
        # the query does not constrain cannot be skipped.
        op.execute(
            f"""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS {AUDIT_CREATED_INDEX}
            ON audit_logs (created_at DESC, id DESC)
            """
        )
        # "Everything this station said in the last 48 hours" — the question the
        # event browser exists to answer.
        op.execute(
            f"""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS {EVENTS_STATION_INDEX}
            ON station_events (ground_station_id, received_at DESC, id DESC)
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {EVENTS_STATION_INDEX}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {AUDIT_CREATED_INDEX}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {AUDIT_ORG_INDEX}")
