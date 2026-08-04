import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class WeatherSample(UUIDMixin, TimestampMixin, Base):
    """One minute of a station's weather, for the trend charts.

    The same shape and the same reasoning as `PowerSample`: written by a
    telemetry persister (services/weather_history.py) rather than by the station,
    `at` rounded to the minute and unique per station so the rounding *is* the
    downsample. Every reading is nullable because the fitted instrument decides
    what it has — the Airmar ships with and without a humidity module, a station
    may have no barometer — and null here means "no such sensor", never zero.
    """

    __tablename__ = "weather_samples"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    ground_station_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ground_stations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    pressure_hpa: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_kt: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "ground_station_id", "at", name="uq_weather_sample_station_minute"
        ),
    )
