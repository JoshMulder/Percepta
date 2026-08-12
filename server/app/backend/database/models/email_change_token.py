import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class EmailChangeToken(UUIDMixin, TimestampMixin, Base):
    """A single-use link that confirms a user controls a new email address.

    Modelled on `PasswordResetToken`, and not RLS-scoped for the same reasons:
    an email is a property of the person rather than a tenancy, and the token is
    redeemed before any organisation context exists. Only the hash is stored —
    the value lives in the recipient's mailbox and nowhere else.

    The one addition is **`new_email`**: the address the link was sent to and the
    one the account moves to when it is redeemed. Storing it on the token means
    the redeemer needs nothing but the link, and a second request for a different
    address supersedes the first (the outstanding ones are marked used), so only
    the most recently confirmed address can ever land.

    Always self-service — the endpoint requires the account's current password —
    so unlike a reset there is no `requested_by`: an admin cannot start one.
    """

    __tablename__ = "email_change_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The address the verification link was sent to, and the one the account
    #: adopts on redemption. Normalised (stripped, lower-cased) before storage.
    new_email: Mapped[str] = mapped_column(String(320), nullable=False)

    #: SHA-256 hex of the token. Unique, so a collision is a database error
    #: rather than two people sharing a verification.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
