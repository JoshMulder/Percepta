import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class StationGrant(UUIDMixin, TimestampMixin, Base):
    """What one user may do on one ground station.

    Deliberately explicit: every station a user can reach is a row naming that
    station. There is no org-wide wildcard for ordinary users. It costs a little
    administrative convenience and buys the property that matters here - "who
    can see station 7?" is one query with no inference, which is what an access
    review or an incident investigation actually needs.

    The single implicit path is that org admins hold every capability on every
    station in their own org, which is what DroneOps' require_admin already
    means. So this sits alongside organization_memberships.roles rather than
    replacing it, and the inherited role checks keep working untouched.
    """

    __tablename__ = "station_grants"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Values from auth.capabilities.Capability. Stored as text[] to match
    # DroneOps' organization_memberships.roles, so both are queried the same way.
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )

    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Optional expiry. Revoking a grant - by expiry or by hand - must reach any
    # WebSocket already streaming under it, which is why revocation is pushed
    # rather than only checked on the next request. See
    # docs/03-realtime-isolation.md section 6.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "ground_station_id", name="uq_grant_user_station"),
    )
