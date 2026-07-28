import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class GroundStation(UUIDMixin, TimestampMixin, Base):
    """A deployed ground station unit: sensors plus an onboard computer.

    Belongs to exactly one organisation (docs/00-topology.md rule 2). That
    binding is authoritative and lives here - it is never read from anything the
    station itself sends, so a compromised unit cannot claim to belong to a
    different org.
    """

    __tablename__ = "ground_stations"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # IANA zone, e.g. "Pacific/Auckland". Stations are remote and an operator
    # may be in a different zone, so local time is a property of the station.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Set when the unit is enrolled and issued its client credential. Until then
    # the station exists as a record but nothing may publish as it.
    enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last authenticated contact. Drives the online/offline state carried on the
    # org status channel, which is how an operator learns a station has dropped
    # while they are looking at a different one.
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
