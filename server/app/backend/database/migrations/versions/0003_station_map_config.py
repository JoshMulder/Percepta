"""Per-station map configuration for the cached basemap

A ground station is fixed, so the map it needs is a finite set of tiles around
one point. These columns define that set: the radius to cache and the zoom range
to cache it at. They are per station rather than global because a coastal site
watching 60 km of approach and an urban site watching a compound want very
different extents.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defaults are chosen so the cache stays small. Tile count grows by 4x per
    # zoom level, so 8-14 over a 50 km radius is a few thousand tiles (tens of
    # MB); pushing max_zoom to 17 over the same radius is roughly 64x that.
    op.add_column(
        "ground_stations",
        sa.Column("map_min_zoom", sa.Integer(), nullable=False, server_default="8"),
    )
    op.add_column(
        "ground_stations",
        sa.Column("map_max_zoom", sa.Integer(), nullable=False, server_default="14"),
    )
    op.add_column(
        "ground_stations",
        sa.Column(
            "map_radius_km", sa.Float(), nullable=False, server_default="50"
        ),
    )
    op.add_column(
        "ground_stations",
        sa.Column("map_cached_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_station_zoom_range",
        "ground_stations",
        "map_min_zoom >= 0 AND map_max_zoom <= 19 AND map_min_zoom <= map_max_zoom",
    )


def downgrade() -> None:
    op.drop_constraint("ck_station_zoom_range", "ground_stations", type_="check")
    for column in (
        "map_cached_at",
        "map_radius_km",
        "map_max_zoom",
        "map_min_zoom",
    ):
        op.drop_column("ground_stations", column)
