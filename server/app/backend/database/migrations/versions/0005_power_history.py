"""Persisted power history for the battery chart

The console buffered state of charge in the browser, which reset on every reload
and could never reach past the moment the page was opened. A 7-day view needs
the samples to outlive the tab.

One row per station per minute: fine enough to show a discharge overnight,
coarse enough that a week is ten thousand rows rather than six hundred thousand.
Org-scoped and under RLS like everything else operational.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREDICATE = (
    "organization_id = nullif(current_setting('app.current_org', true), '')::uuid "
    "OR coalesce(current_setting('app.bypass', true), 'off') = 'on'"
)


def upgrade() -> None:
    op.create_table(
        "power_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "ground_station_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ground_stations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("soc_pct", sa.Float(), nullable=False),
        sa.Column("battery_v", sa.Float(), nullable=True),
        sa.Column("pv_w", sa.Float(), nullable=True),
        sa.Column("load_w", sa.Float(), nullable=True),
        # One sample per station per minute. The unique constraint is what
        # enforces the downsample - the writer rounds `at` to the minute and
        # lets the insert be discarded if that minute is already recorded.
        sa.UniqueConstraint(
            "ground_station_id", "at", name="uq_power_sample_station_minute"
        ),
    )
    # Every read is "this station, this window", newest last.
    op.create_index(
        "ix_power_samples_station_at", "power_samples", ["ground_station_id", "at"]
    )
    op.create_index(
        "ix_power_samples_organization_id", "power_samples", ["organization_id"]
    )

    op.execute("ALTER TABLE power_samples ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE power_samples FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY org_isolation ON power_samples "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON power_samples;")
    op.drop_table("power_samples")
