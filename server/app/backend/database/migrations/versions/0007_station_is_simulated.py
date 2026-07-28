"""Per-station simulated flag

Demo mode was a deployment-wide setting, which had to be wrong the moment a
deployment carried both a real station and simulated ones - and that is the
normal case during development, not an edge case.

Existing rows are seeded from the deployment's current DEMO_MODE so the console
looks the same immediately after this migration as it did before it.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-28

"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ground_stations",
        sa.Column(
            "is_simulated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # Carry the old global setting onto the rows that existed under it, so
    # nothing appears to change the moment this runs.
    if os.environ.get("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}:
        op.execute("UPDATE ground_stations SET is_simulated = true")


def downgrade() -> None:
    op.drop_column("ground_stations", "is_simulated")
