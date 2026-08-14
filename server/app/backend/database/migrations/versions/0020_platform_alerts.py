"""Alerts that survive a reload, and a way to silence a known-bad site

Until now every alert in this product was a React object in one browser tab —
capped at forty and gone on refresh. Nothing was queryable, nothing survived a
shift change, and two operators at two desks had no way to know they were both
looking at the same fault.

The two partial indexes here are the design rather than tuning:

  ux_platform_alerts_open_key — one open alert per (station, fact). A re-raise
  bumps `occurrences` instead of inserting, so a station flapping its uplink
  every ninety seconds is one row with a count rather than four hundred rows,
  and the raise path becomes idempotent under the at-least-once redelivery the
  station's event queue already promises.

  ix_platform_alerts_open — the rail's read. Partial, so it stays a hot set of
  hundreds rather than a scan of every alert ever raised.

`station_maintenance` ships in the SAME migration as the alerts it silences, and
that is deliberate. The stated biggest risk of this whole feature is alert
fatigue; shipping the engine first and its mitigation later means one week where
a known-bad site shouts every ninety seconds, which is exactly long enough for
operators to learn to stop reading the rail. That habit does not come back when
the suppression does.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-14

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# The same GUC-keyed predicate every org-scoped table uses. An absent org
# setting matches nothing, so a query with no context fails closed.
_PREDICATE = (
    "organization_id = nullif(current_setting('app.current_org', true), '')::uuid "
    "OR coalesce(current_setting('app.bypass', true), 'off') = 'on'"
)


def upgrade() -> None:
    op.create_table(
        "platform_alerts",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "organization_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ground_station_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("ground_stations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column(
            "acked_by_user_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_note", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(32), nullable=True),
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_alerts_org", "platform_alerts", ["organization_id"]
    )
    op.create_index(
        "ix_platform_alerts_station", "platform_alerts", ["ground_station_id"]
    )
    # One open alert per fact per station.
    op.execute(
        """
        CREATE UNIQUE INDEX ux_platform_alerts_open_key
        ON platform_alerts (ground_station_id, dedupe_key)
        WHERE state <> 'closed'
        """
    )
    # The rail's read: still-open alerts, worst first, oldest first.
    op.execute(
        """
        CREATE INDEX ix_platform_alerts_open
        ON platform_alerts (state, severity, first_seen_at)
        WHERE state <> 'closed'
        """
    )

    op.create_table(
        "station_maintenance",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "organization_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ground_station_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("ground_stations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_by_user_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_station_maintenance_org", "station_maintenance", ["organization_id"]
    )
    op.create_index(
        "ix_station_maintenance_window",
        "station_maintenance",
        ["ground_station_id", "until_at"],
    )

    for table in ("platform_alerts", "station_maintenance"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY org_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
        )


def downgrade() -> None:
    for table in ("station_maintenance", "platform_alerts"):
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
    op.drop_table("station_maintenance")
    op.execute("DROP INDEX IF EXISTS ix_platform_alerts_open")
    op.execute("DROP INDEX IF EXISTS ux_platform_alerts_open_key")
    op.drop_table("platform_alerts")
