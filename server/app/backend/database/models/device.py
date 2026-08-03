import uuid
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class DeviceKind(StrEnum):
    CAMERA = "camera"
    RADIO = "radio"
    ADSB = "adsb"
    WEATHER = "weather"
    LIGHT = "light"
    POWER = "power"
    LINK = "link"


class Device(UUIDMixin, TimestampMixin, Base):
    """One subsystem instance on a ground station.

    Modelled as a generic typed device with an adapter behind it rather than a
    column per sensor, so adding a subsystem later - including the DJI dock,
    which is a separate workstream for now - is a new kind and an adapter, not a
    schema change.
    """

    __tablename__ = "devices"

    # Denormalised from the station for RLS: policies key off organization_id
    # directly rather than joining through ground_stations on every row. Must
    # always match the parent station's org - enforced in the repository layer
    # and by the enrolment path, which is the only writer.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    # CASCADE, like every other table that hangs off a station — credentials,
    # enrolment tokens, events, grants, power samples all have it and this was
    # the one that did not. A device row is meaningless without its station,
    # and the constraint made deleting a station raise a foreign-key violation
    # instead: a 500 on the one operation whose whole purpose is tidying up
    # records that never became stations.
    #
    # The audit trail is deliberately not in this list. `audit_log` holds
    # `ground_station_id` as a plain column with no foreign key at all, so that
    # "who let that box onto our platform" outlives the row it refers to.
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Stable identifier within its station, e.g. "cam-north". Part of the
    # telemetry topic, so it must not change once the unit is publishing.
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Adapter-specific configuration - ONVIF endpoint, Modbus unit id, serial
    # port, tuner defaults. Shape is the adapter's business, not the platform's.
    config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("ground_station_id", "slug", name="uq_device_station_slug"),
    )
