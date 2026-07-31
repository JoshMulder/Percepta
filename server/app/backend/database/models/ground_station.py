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

    #: Where this is, in words — "Timaru", "Canterbury". Derived from the
    #: coordinates by the server (services/geocode.py), never sent by the
    #: station, and recomputed only when `locality_for` no longer matches the
    #: position. Null is a real state: no position, open water, or a provider
    #: that could not resolve it.
    locality: Mapped[str | None] = mapped_column(String(160), nullable=True)
    region: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: The rounded coordinate pair the two above were derived from, so a stale
    #: locality is detectable and the lookup neither repeats every frame nor
    #: never repeats.
    locality_for: Mapped[str | None] = mapped_column(String(48), nullable=True)

    #: Metres. Set at commissioning with the coordinates and frozen with them.
    #: Null is a real state: the ADS-B barometric correction is its only
    #: consumer and refuses without one rather than assuming sea level, which
    #: would put every corrected altitude out by the height of the site.
    elevation_m: Mapped[float | None] = mapped_column(Float, nullable=True)

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
    # Declared by whoever creates the station, in the console. The simulator
    # reads it to decide what to drive and never writes it: it used to own this
    # field, which meant it overwrote the operator's answer on every run and
    # adopted any station not on a deny-list, including real ones.
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
