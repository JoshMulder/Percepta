"""Devices go with their station, like everything else that hangs off one

Every child table of `ground_stations` was created ON DELETE CASCADE —
credentials, enrolment tokens, events, grants, power samples — except
`devices`, which was created with a plain reference.

So deleting a station raised a foreign-key violation the moment it had a single
device row, which is a 500 on the one operation whose entire purpose is tidying
away records that never became stations. The endpoint's own guard is about
whether anything can still *authenticate* as the station; nothing about a
device row bears on that, and a device without its station is not a record of
anything.

`audit_log` is deliberately absent from that list and stays absent: it holds
`ground_station_id` as a plain column with no foreign key, so that "who let
that box onto our platform, and when" outlives the row it refers to. That is
the distinction this migration preserves — the rows that describe a station go
with it, and the rows that describe what people did do not.

The constraint is unnamed in 0001, so it is found by reflection rather than by
a name this migration would have to guess.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-03

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _constraint_name(bind, referred_table: str) -> str | None:
    """The foreign key on `devices` pointing at `referred_table`.

    Reflected because 0001 created it without a name, so the database chose
    one. Postgres' default is predictable but it is not a promise, and a
    migration that guesses wrong fails in front of somebody's data.
    """
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys("devices"):
        if fk.get("referred_table") == referred_table:
            return fk.get("name")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    name = _constraint_name(bind, "ground_stations")
    if name:
        op.drop_constraint(name, "devices", type_="foreignkey")
    op.create_foreign_key(
        "fk_devices_ground_station_id",
        "devices", "ground_stations",
        ["ground_station_id"], ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_devices_ground_station_id", "devices",
                       type_="foreignkey")
    op.create_foreign_key(
        None, "devices", "ground_stations", ["ground_station_id"], ["id"],
    )
