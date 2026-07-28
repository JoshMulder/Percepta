import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class AuthSession(UUIDMixin, TimestampMixin, Base):
    """A server-side login session, keyed by the JWT's `jti` claim (this row's
    id). Every issued access token references a session; revoking the session
    (logout, "sign out everywhere", password change) immediately invalidates
    the token regardless of its unexpired `exp`. This is what makes tokens
    revocable without waiting for them to time out.

    Ported from DroneOps unchanged, with one addition below. The inherited model
    works because HTTP requests are short and frequent, so the *next* request
    fails. A monitoring WebSocket makes no further requests and can stay open for
    hours, so revoking a row here is not by itself enough to stop a live stream -
    revocation is also pushed over Redis to the processes holding sockets, and
    every socket independently revalidates on an interval. See
    docs/03-realtime-isolation.md section 6.
    """

    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Note there is deliberately no active_ground_station_id here. The
    # one-station-at-a-time rule binds a *connection*, not a session: a user may
    # hold several tabs open on different stations, and each socket serves one.
    # A session spans tabs, so pinning the station here would make switching in
    # one tab yank another. The pin lives on the realtime connection instead -
    # see realtime/, and docs/03-realtime-isolation.md section 4.
