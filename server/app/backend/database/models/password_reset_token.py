import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class PasswordResetToken(UUIDMixin, TimestampMixin, Base):
    """A single-use link that lets one person set one password.

    Stored only as `token_hash`, like a station enrolment token and for the same
    reason: the value exists in the email and nowhere else, so a copy of this
    table is not a set of live credentials. Unlike an enrolment token this one
    really is single use - there is no lost-signal-on-a-hilltop case here, and a
    reset link that keeps working is a spare key to the account left in a
    mailbox.

    **Not scoped to an organisation.** A password is a property of the person,
    not of a tenancy, and the same user can belong to several. That is also why
    this table has no RLS policy: it is read during redemption, before anyone is
    authenticated and before there is any organisation context to bind to. It
    carries nothing but a hash, an expiry and a user id.

    Who asked for it is recorded. An admin resetting somebody else's password
    and a user resetting their own are different events, and the difference
    matters when reading back what happened to an account.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # SHA-256 hex of the token. Unique, so a collision is a database error
    # rather than two people sharing a reset.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Null when the user asked for it themselves. Set to the admin who triggered
    # it otherwise - kept even after the admin's own account is gone, hence
    # SET NULL rather than CASCADE: deleting an administrator must not delete
    # the record that they reset somebody's password.
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
