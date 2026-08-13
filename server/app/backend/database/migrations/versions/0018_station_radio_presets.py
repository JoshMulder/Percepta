"""Radio presets, per station and shared across the organisation

Presets lived in each browser's localStorage, keyed by station — so every
operator built their own set, a preset saved at the console was invisible from a
phone, and clearing site data lost them. They describe the station's airspace,
not the person looking at it: the tower, ground and ATIS frequencies for a site
are the same for everyone in the organisation.

A JSONB column on ground_stations rather than a table of its own. Presets are a
short fixed-slot list read and written whole, they are per-station configuration
exactly like map_min_zoom beside them, and hanging them off the station row means
they inherit its row-level security — which is what makes them org-wide and no
wider, with no new policy to get right.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable rather than defaulted to an empty array: null means "nobody has
    # set any", which is what every existing station is, and lets the API tell
    # that from a set that was deliberately cleared.
    op.add_column(
        "ground_stations",
        sa.Column("radio_presets", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ground_stations", "radio_presets")
