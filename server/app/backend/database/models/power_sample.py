import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class PowerSample(UUIDMixin, TimestampMixin, Base):
    """One minute of a station's power state.

    Written by the telemetry persister (services/power_history.py) rather than
    by the station: the station reports continuously, and what is kept is a
    downsample of that. `at` is rounded to the minute and unique per station, so
    the rounding *is* the downsample - a second sample for the same minute is
    simply discarded.
    """

    __tablename__ = "power_samples"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    soc_pct: Mapped[float] = mapped_column(Float, nullable=False)
    battery_v: Mapped[float | None] = mapped_column(Float, nullable=True)
    pv_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    load_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Nullable and separate from "0 W" on purpose: a site with no grid and no
    # generator omits these, the same way the live payload does, so the history
    # chart can leave the source out rather than draw a flat line at zero that
    # reads as "fitted, delivering nothing" (see the contract note on mains_w).
    mains_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    generator_w: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "ground_station_id", "at", name="uq_power_sample_station_minute"
        ),
    )
