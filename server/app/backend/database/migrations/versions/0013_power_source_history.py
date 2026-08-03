"""Record AC-mains and generator watts alongside the rest of the power sample

The battery-history popout grew a second chart — load, solar, AC and generator
over the same window — but only load and solar were ever persisted. Solar
(`pv_w`) and load (`load_w`) were columns from 0005; the two input sources were
not, so a 7-day view of them had nothing to draw.

Both nullable, and null means "no such source" rather than 0 W — the same
distinction the live telemetry draws, and the reason the chart can leave a grid
or a generator off the legend at a site that has never had one instead of
plotting a flat line at zero that reads as a dead input.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("power_samples", sa.Column("mains_w", sa.Float(), nullable=True))
    op.add_column(
        "power_samples", sa.Column("generator_w", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("power_samples", "generator_w")
    op.drop_column("power_samples", "mains_w")
