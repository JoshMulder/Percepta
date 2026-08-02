"""The events ledger, which is the one station channel that is not a stream

Everything else a station publishes is current state: a newer telemetry frame
replaces the last one within a second, so a dropped frame costs nothing and the
transport is explicitly allowed to drop them. An event has no newer version.
Losing one loses the fact, which is why the station buffers events across an
outage while discarding telemetry, and why this is the only channel the
platform acknowledges.

Two identifiers, deliberately. `event_id` is the station's own and is the
deduplication key — delivery is at-least-once, an acknowledgement can be lost
after this table has committed, and the station then re-sends. The unique
constraint is what makes that normal rather than a fault. `seq` is the ack
cursor: monotonic per station, so one number says "everything up to here is
dealt with", which an id cannot.

`received_at` is the platform's own clock and is what staleness is judged on.
Never `at`: that is set by the station, on a clock the station itself may flag
as `unsynced`, and alerting decisions hang off it — so a station that is wrong,
or lying, must not be able to backdate a real event into silence.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "station_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("ground_station_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ground_stations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clock", sa.String(length=16), nullable=False,
                  server_default="synced"),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_station_events_organization_id", "station_events",
                    ["organization_id"])
    op.create_index("ix_station_events_ground_station_id", "station_events",
                    ["ground_station_id"])
    op.create_index("ix_station_events_received_at", "station_events",
                    ["received_at"])
    op.create_index("ix_station_events_type", "station_events", ["type"])
    op.create_index("ix_station_event_seq", "station_events",
                    ["ground_station_id", "seq"])
    # Scoped to the station rather than global. The contract calls `id`
    # globally unique and says consumers deduplicate on it, which reads as `id`
    # alone — and `id` alone is a cross-tenant key the moment any station emits
    # a non-UUID one, which nothing on the way in prevents.
    op.create_unique_constraint("uq_station_event_id", "station_events",
                                ["ground_station_id", "event_id"])


def downgrade() -> None:
    op.drop_table("station_events")
