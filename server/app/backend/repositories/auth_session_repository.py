import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.database.models.auth_session import AuthSession


class AuthSessionRepository:
    """auth_sessions is deliberately outside RLS - it is read during
    authentication, before an org context exists. See migration 0002.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_active(self, *, session_id: uuid.UUID) -> AuthSession | None:
        now = datetime.now(UTC)
        return self.db.execute(
            select(AuthSession).where(
                AuthSession.id == session_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        ).scalar_one_or_none()

    def create(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        expires_at: datetime,
    ) -> AuthSession:
        session = AuthSession(
            id=uuid.uuid4(),
            user_id=user_id,
            organization_id=organization_id,
            expires_at=expires_at,
        )
        self.db.add(session)
        self.db.flush()
        return session

    def revoke(self, *, session_id: uuid.UUID) -> None:
        self.db.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    def revoke_all_for_user(self, *, user_id: uuid.UUID) -> None:
        """Sign out everywhere.

        NOT YET COMPLETE. This alone does not promptly stop a live WebSocket - an
        open socket makes no further HTTP requests, so it only notices at the
        next revalidation sweep (STREAM_REVALIDATE_SECONDS, default 60s). The
        Redis push that makes revocation immediate is not built yet; until it is,
        60 seconds is the real worst case, not the backstop it is meant to be.
        See docs/03-realtime-isolation.md section 6.
        """
        self.db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
