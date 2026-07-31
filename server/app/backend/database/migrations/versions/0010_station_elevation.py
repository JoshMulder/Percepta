"""Elevation, alongside the position it belongs with

Set at commissioning with the coordinates and frozen with them: a station that
needs a different elevation has physically moved, and a box that has moved is
recommissioned rather than edited.

It lived only on the station before, typed into the setup page. That was the
one part of "where this box is" the platform did not know — so the platform
could not show it, could not carry it into a replacement box, and an installer
correcting a position had to remember to correct a number on a different screen
in a different place.

Nullable, and null is a real state. The ADS-B barometric correction is the only
consumer and it refuses to run without one rather than assuming sea level,
which would put every corrected altitude out by the height of the site.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ground_stations",
        sa.Column("elevation_m", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ground_stations", "elevation_m")
