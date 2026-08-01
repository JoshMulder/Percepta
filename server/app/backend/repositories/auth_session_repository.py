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

    def revoke_all_for_user(self, *, user_id: uuid.UUID) -> list[uuid.UUID]:
        """Sign out everywhere. Returns the sessions that were ended.

        **The caller must push each id to `realtime.revocation.revoke_session`.**
        Writing the rows alone does not stop a live WebSocket: an open socket
        makes no further HTTP requests, so it notices only at the next
        revalidation sweep (`STREAM_REVALIDATE_SECONDS`, default 60s) - and the
        flows that call this are the ones where somebody is believed to be in
        the account right now, which is the worst possible time to leave their
        video and telemetry running for another minute.

        The ids are returned rather than pushed here because the push is not
        transactional and this is: sending it before the commit would announce
        a revocation that a rollback then undoes. `api/account.py` is the
        worked example.

        (This docstring used to say the Redis push "is not built yet". It has
        been built - `realtime/revocation.py` - and the stale note is most of
        why the reset path went without it.)
        """
        rows = self.db.execute(
            select(AuthSession.id).where(
                AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
            )
        ).scalars().all()
        self.db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        return list(rows)
