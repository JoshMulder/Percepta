"""Raise the default basemap max zoom from 14 to 17

14 was chosen when every tile had to be prefetched in bulk, where each extra
level costs 4x the download. With cache-through fetching (api/tiles.py) tiles
arrive only for what someone actually looks at, so a deeper zoom costs nothing
until it is used - and 14 is too shallow to see a compound, a gate or a vehicle,
which is most of what a security site is watching for.

17 rather than 19: it is the deepest level all three basemaps have, so the
zoom limit does not change under the operator when they switch to terrain.

Existing rows still sitting on the old default are moved up; anything an
operator has deliberately set to something else is left alone.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-28

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("ground_stations", "map_max_zoom", server_default="17")
    op.execute("UPDATE ground_stations SET map_max_zoom = 17 WHERE map_max_zoom = 14")


def downgrade() -> None:
    op.alter_column("ground_stations", "map_max_zoom", server_default="14")
    op.execute("UPDATE ground_stations SET map_max_zoom = 14 WHERE map_max_zoom = 17")
