import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class StationEnrolmentToken(UUIDMixin, TimestampMixin, Base):
    """A short-lived code that lets one box claim one station record.

    An admin issues it; a technician types it into the box on site. It is both a
    secret and a lookup key, so it is stored only as `lookup_hash` of the value
    and is unrecoverable afterwards - the same treatment DroneOps gives its
    calendar feed token, for the same reason. Losing one means issuing another,
    which is cheap and leaves an audit trail.

    Bound to exactly one station. A token cannot be redirected at a different
    record, so the worst a leaked token achieves is enrolling the wrong hardware
    into a station the admin had already decided to create.

    Not single-use in the strictest sense, deliberately. See
    `services/enrolment.py` for why a claim can be repeated inside the token's
    lifetime: a technician who loses signal mid-enrolment must be able to retry,
    and the alternative is a site visit.
    """

    __tablename__ = "station_enrolment_tokens"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256 hex of the normalised token. Unique so a collision is a database
    # error rather than two stations sharing a code.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # First successful claim. Retries after this are still allowed while the
    # token is live; this records when the box first appeared.
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_count: Mapped[int] = mapped_column(nullable=False, default=0)

    # Revoked by an admin who decided the code should stop working before it
    # expired - a token read aloud on a call that turned out to be the wrong
    # person, for instance.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Nullable so the token outlives the admin's account.
    issued_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
