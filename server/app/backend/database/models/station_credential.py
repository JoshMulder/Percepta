import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class StationCredential(UUIDMixin, TimestampMixin, Base):
    """What a station authenticates with, once enrolled.

    A row per credential rather than a column on `ground_stations`, because a
    station legitimately has more than one at a time. Renewal issues a new
    credential while the old one keeps working for an overlap window
    (`contract/enrolment.md` §6) - a station that renews and then loses power
    mid-swap must not be locked out of a site that is hours away.

    Stored as `lookup_hash` of the secret, never the secret. The input is CSPRNG
    output rather than a chosen password, so SHA-256 is the right primitive
    here: brute force is not on the table and lookups have to be exact-match on
    an index. The plaintext exists once, in the enrolment response, and is not
    recoverable afterwards even by us - a platform that can hand out a station's
    credential is one whose operator can impersonate a customer's station.

    `kind` exists so mTLS can arrive without a migration. The lifecycle, the
    endpoints and the technician's steps are all deliberately independent of
    which credential type is in use.
    """

    __tablename__ = "station_credentials"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="bearer")

    secret_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Set when a renewal replaces this credential. It stays valid until this
    # moment, not until the renewal happens, which is the overlap that makes
    # renewing safe over an unreliable link.
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set by an admin revoking access, or by a re-enrolment replacing hardware.
    # Immediate and unconditional - unlike superseding, there is no overlap.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Observability for the failure that costs a site visit: a station that has
    # stopped renewing is visible here well before its credential expires.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Whether a broker principal was successfully provisioned for this
    # credential. Provisioning is fail-soft (the enrolment still succeeds and is
    # auditable), so this is how an operator finds the ones that need retrying.
    broker_provisioned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
