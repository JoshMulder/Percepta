import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database.models.ground_station import GroundStation
from backend.database.models.station_grant import StationGrant


class StationGrantRepository:
    """Reads of this table are scoped by RLS to the request's org, so a lookup
    that somehow escaped its org context returns nothing rather than another
    tenant's grants - the authorisation check fails closed. Do not add a
    privileged-engine path here without a very good reason.
    """

    def __init__(self, db: Session):
        self.db = db

    def get(
        self, *, user_id: uuid.UUID, ground_station_id: uuid.UUID
    ) -> StationGrant | None:
        return self.db.execute(
            select(StationGrant).where(
                StationGrant.user_id == user_id,
                StationGrant.ground_station_id == ground_station_id,
            )
        ).scalar_one_or_none()

    def get_live(
        self, *, user_id: uuid.UUID, ground_station_id: uuid.UUID
    ) -> StationGrant | None:
        """As get(), but returns None for a grant whose expiry has passed.

        Expiry is evaluated on read rather than swept by a job, so a lapsed
        grant stops working the moment it lapses even if nothing has run.
        """
        grant = self.get(user_id=user_id, ground_station_id=ground_station_id)
        if grant is None:
            return None
        if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
            return None
        return grant

    def list_for_user(self, *, user_id: uuid.UUID) -> list[StationGrant]:
        """Every live grant this user holds in the current org context. Backs
        the station switcher and the org status channel's subscription set."""
        now = datetime.now(UTC)
        rows = self.db.execute(
            select(StationGrant).where(
                StationGrant.user_id == user_id,
                (StationGrant.expires_at.is_(None)) | (StationGrant.expires_at > now),
            )
        ).scalars()
        return list(rows)

    def active_station(self, *, ground_station_id: uuid.UUID) -> GroundStation | None:
        """The station, if it exists, is active, and is visible in the current
        org context. All three matter: a deactivated station grants nothing, and
        RLS handles the third."""
        return self.db.execute(
            select(GroundStation).where(
                GroundStation.id == ground_station_id,
                GroundStation.is_active.is_(True),
            )
        ).scalar_one_or_none()
