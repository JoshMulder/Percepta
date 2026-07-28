import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class AuditLog(UUIDMixin, TimestampMixin, Base):
    """Append-only security and operations trail. Rows are never edited or
    deleted by the app.

    Same shape as DroneOps' audit_logs, with two differences that matter here.

    *It is read by org admins, not only platform admins.* DroneOps scopes this
    to auth and administrative events, visible only to the platform. Here it
    also records every command issued to physical hardware at a remote,
    unattended site, and the org that owns that hardware has a legitimate need
    to see who did what to it.

    *Ground station and device are first-class columns*, not buried in `detail`,
    because "what happened at station 7 last night" is the question this table
    exists to answer and it should not require a JSONB scan.
    """

    __tablename__ = "audit_logs"

    # Nullable: some events (a failed login for an unknown email) have no
    # resolved user/org. actor_email is captured as free text so the record
    # survives even if the user is later deleted.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Not FKs: an audit row must outlive the station or device it refers to.
    ground_station_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Extra structured context. For a hardware command this is where the station
    # state at the time of the command belongs - the record that matters if an
    # incident is ever reviewed. Never store secrets here.
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
