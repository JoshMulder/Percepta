import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class StationEvent(UUIDMixin, TimestampMixin, Base):
    """One thing that happened at a site, and the only station data that is a
    ledger rather than a stream.

    Every other channel is current state and may be dropped: a newer telemetry
    frame replaces the last one within a second, so losing one costs nothing.
    An event has no newer version. A transmission recorded at 03:12, a
    proximity alert, a credential renewal that failed, a floodlight that drew
    no current — losing it loses the fact. That asymmetry is why this table
    exists, why the station buffers events across an outage while discarding
    telemetry, and why this is the one channel the platform acknowledges.

    **Delivery is at-least-once and never exactly-once.** An acknowledgement
    can be lost after the row is committed, in which case the station sends the
    batch again — so a duplicate arriving is normal operation rather than a
    fault, and the unique constraint below is the thing that makes it harmless.
    """

    __tablename__ = "station_events"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: The station's own id for this event, and the deduplication key.
    #:
    #: A string rather than a UUID column because the contract does not assert
    #: `format: uuid` on it and the schemas are explicitly not an accept/reject
    #: gate — a station emitting something else must deduplicate correctly
    #: rather than raise, and a UUID column would make a malformed id an error
    #: at the worst possible moment.
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: The station's ack cursor, which is deliberately not `event_id`.
    #:
    #: Two identifiers because they answer different questions: `event_id`
    #: survives a store rebuild and says "this is the same fact", `seq` is
    #: monotonic per station and says "everything up to here is dealt with".
    #: Collapsing them would make the acknowledgement unable to advance past a
    #: gap.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: When the station says it happened.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: When this platform received it, which is what staleness is judged on.
    #:
    #: Never `at`. That one is set by the station, on a clock the station
    #: itself may flag as `unsynced`, and liveness decisions hang off it — so a
    #: station that is wrong, or lying, must not be able to backdate a real
    #: event into silence. This column is the platform's own and cannot be
    #: influenced from the field.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    #: `synced` or `unsynced`, as the station declared it. Carried rather than
    #: resolved: an event from a box with no battery-backed clock is still a
    #: fact, it just cannot be placed on a timeline, and the console needs to
    #: be able to say so rather than draw it confidently in the wrong hour.
    clock: Mapped[str] = mapped_column(String(16), nullable=False,
                                       server_default="synced")

    type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # The whole of the at-least-once guarantee, in one line. A re-sent
        # batch collides here and is discarded instead of producing a second
        # copy of a fact.
        UniqueConstraint("ground_station_id", "event_id",
                         name="uq_station_event_id"),
        # Scoped to the station, not global. The contract calls `id` globally
        # unique and consumers deduplicate on it, which reads as `id` alone —
        # and `id` alone is a cross-tenant key the moment any station emits a
        # non-UUID one, because nothing validates the format on the way in.
        # Two organisations cannot collide here.
        Index("ix_station_event_seq", "ground_station_id", "seq"),
    )
