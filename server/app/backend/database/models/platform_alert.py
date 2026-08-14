"""An alert that outlives the browser that first saw it.

Every alert in this product has, until now, been a React object in one tab:
capped at forty, gone on reload, and invisible to the person at the next desk
(components/Console.tsx). That is defensible for a console somebody watches for
an hour. It is not defensible for a command centre where the point is that
nothing is missed across a shift change, and where two operators must not both
walk out to the same site.

Two indexes carry the whole design, and they are not optimisations:

  UNIQUE (ground_station_id, dedupe_key) WHERE state <> 'closed'
    One open alert per fact. A re-raise bumps `occurrences` and cannot stack, so
    a station flapping its uplink every ninety seconds produces one row with a
    count rather than four hundred rows. It also makes the raise path idempotent
    under at-least-once redelivery, which the station's event queue explicitly
    is: the same fact arriving twice must not become two alerts.

  (state, severity, first_seen_at) WHERE state <> 'closed'
    The rail reads a hot set of hundreds. Without the partial predicate it would
    scan a table that accumulates every alert this platform has ever raised, to
    find the handful that are still open.

`organization_id` is the STATION's organisation, never the operator's. An Odin
operator is acting across tenants, and an alert about a customer's hardware
belongs to that customer's row-level security, not to the platform org — so the
same policy that protects everything else protects this without a special case.

Severity stays info | warning | critical. That vocabulary is the contract's, it
is shared with the station's own health conditions, and a fourth level invented
here would have no meaning at either end of the wire.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class PlatformAlert(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "platform_alerts"

    #: The STATION's organisation. See the module note: this is what makes the
    #: existing RLS policy correct for a cross-tenant surface.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Where the alert came from: "condition" (the station's own health), "event"
    #: (an occurrence-shaped station event), "dark" (the platform noticing
    #: silence), "link" (the broker seeing a connection come and go). Kept so a
    #: noisy source can be identified and turned off without guessing.
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    #: What makes two raises the same fact. For a condition it is the condition
    #: id; for an event, its type. Scoped per station by the unique index, so two
    #: stations with the same fault are two alerts — which is right, because they
    #: are two site visits.
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)

    type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: When it first happened, and when it last did. The rail sorts on the
    #: FIRST: a queue ages upward, and an alert that keeps re-occurring must not
    #: keep resetting its own position to the bottom of the list.
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: open | acked | closed.
    #:
    #: ACK IS NOT CLOSE, and conflating them is how a command centre loses a
    #: fault. Ack means "I have seen this and I am dealing with it" — it stops a
    #: second operator picking it up. Close means "it stopped being true". A
    #: station keeps its attention colour on the wall until CLOSED, so
    #: acknowledging something does not make it disappear from the screen while
    #: the site is still broken.
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")

    #: Ack IS assignment. There is no separate assignee concept and no "assigned"
    #: state: two concepts where one will do is how a queue stops being read.
    acked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ack_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: "resolved" when the underlying fact went away on its own, "manual" when an
    #: operator said so. Worth distinguishing: a fault that never self-resolves
    #: is a different maintenance question from one that clears overnight.
    closed_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Silenced until this moment. Snooze is per-alert; a whole station is
    #: silenced with a maintenance window instead.
    snooze_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Both indexes are PARTIAL (WHERE state <> 'closed') and live in the
    # migration, which is the only place a partial predicate is expressed once
    # rather than in two spellings that can drift. Declaring them here as well
    # would give SQLAlchemy a second opinion about the schema it does not own.


class StationMaintenance(UUIDMixin, TimestampMixin, Base):
    """A window in which a station is expected to misbehave.

    Ships WITH the alert engine rather than after it. A design whose stated
    biggest risk is alert fatigue must not schedule its own primary mitigation
    for a later phase: the week where a known-bad site shouts every ninety
    seconds is the week operators learn to stop reading the rail, and that habit
    does not come back when the feature does.
    """

    __tablename__ = "station_maintenance"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    until_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Required. A silenced station with no stated reason is indistinguishable
    #: from a forgotten one, and the next operator has no way to judge whether
    #: the silence is still deliberate.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
