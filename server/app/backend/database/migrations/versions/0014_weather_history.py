"""Persisted weather history for the trend charts

The console shows the weather now; the trend popout shows the last hours or days
of it. Like the power history, the samples have to outlive the tab, so they are
recorded server-side rather than buffered in the browser.

One row per station per minute, org-scoped and under RLS, exactly as
`power_samples`. Every reading is nullable — the fitted instrument decides what
it has, and null means "no such sensor" rather than zero.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREDICATE = (
    "organization_id = nullif(current_setting('app.current_org', true), '')::uuid "
    "OR coalesce(current_setting('app.bypass', true), 'off') = 'on'"
)


def upgrade() -> None:
    op.create_table(
        "weather_samples",
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
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("humidity_pct", sa.Float(), nullable=True),
        sa.Column("pressure_hpa", sa.Float(), nullable=True),
        sa.Column("wind_kt", sa.Float(), nullable=True),
        sa.UniqueConstraint(
            "ground_station_id", "at", name="uq_weather_sample_station_minute"
        ),
    )
    op.create_index(
        "ix_weather_samples_station_at",
        "weather_samples",
        ["ground_station_id", "at"],
    )
    op.create_index(
        "ix_weather_samples_organization_id", "weather_samples", ["organization_id"]
    )

    op.execute("ALTER TABLE weather_samples ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE weather_samples FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY org_isolation ON weather_samples "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON weather_samples;")
    op.drop_table("weather_samples")
