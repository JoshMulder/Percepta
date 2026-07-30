"""Where a station is, in words

A latitude and a longitude are exact and unreadable. "Timaru, Canterbury" is
what somebody says on the phone and what makes a station recognisable in a list
without opening it.

Stored rather than looked up per request. A fixed site does not move, so the
answer changes only when its coordinates do — which keeps the platform inside a
geocoding provider's usage policy without needing a rate limiter, because the
steady-state request rate is zero.

Nullable, and nullable is a real state: a station with no position has no
locality, and neither does one over open water or anywhere the provider could
not resolve. A station without one shows its coordinates, exactly as it did
before this column existed.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ground_stations",
        sa.Column("locality", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "ground_stations",
        sa.Column("region", sa.String(length=160), nullable=True),
    )
    # The coordinates the two above were derived from. Without it there is no
    # way to tell a locality that is current from one left behind by a position
    # that has since changed, and the lookup would either repeat on every frame
    # or never repeat at all.
    op.add_column(
        "ground_stations",
        sa.Column("locality_for", sa.String(length=48), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ground_stations", "locality_for")
    op.drop_column("ground_stations", "region")
    op.drop_column("ground_stations", "locality")
