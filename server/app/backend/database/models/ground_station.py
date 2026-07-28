import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
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

    # Whether this station's data is synthetic. Per-station rather than a
    # deployment-wide flag because a deployment is routinely both at once: a
    # real station reporting real sensors alongside simulated ones used for
    # development. A global switch had to be wrong about one of them.
    #
    # It drives two things in the console - the DEMO badge, and the suppression
    # of sensor-fault indication, since on a simulated station a fault would only
    # ever mean the simulator stopped. Getting it wrong in either direction is
    # bad: badging real data as synthetic invites an operator to ignore it, and
    # not badging synthetic data invites them to believe it.
    #
    # Maintained by whatever is producing the data - the simulator sets it on
    # the stations it drives and clears it on the ones it does not - so it stays
    # true without anyone remembering to change it.
    is_simulated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

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

    # What the box said about itself at its last enrolment: model, serial, OS,
    # agent version. Inventory only - nothing here is ever used to decide what
    # the station is allowed to do, which is why it is safe to accept unverified
    # from the station itself.
    hardware: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # The configuration generation the platform intends this station to run.
    # The station reports the version it has actually applied on its telemetry;
    # a mismatch is what triggers the platform to send config.set. Delivering
    # configuration is not built yet - see contract/enrolment.md section 7 - but
    # the version is issued at enrolment because the response carries it.
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Basemap cache extent. The station does not move, so its map is a finite
    # set of tiles fetched once and served locally from then on - the console
    # never reaches a tile provider, which matters on a metered link and keeps
    # the "nothing leaves our infrastructure at view time" property.
    #
    # Tile count grows 4x per zoom level, so max_zoom is the expensive knob.
    map_min_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    map_max_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=17)
    map_radius_km: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    map_cached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
