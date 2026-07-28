from sqlalchemy import Boolean, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.crypto import EncryptedString
from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class User(UUIDMixin, TimestampMixin, Base):
    """A person. Global, not org-scoped - the same account can be a member of
    several organisations, and its capabilities differ per station in each.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Canonical rendered name (operator lists, audit log). Derived from
    # first/last name when those are set, same as DroneOps.
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # TOTP two-factor auth. mfa_secret is the base32 shared secret (set at
    # enrollment); mfa_enabled flips true once the user verifies their first
    # code. Encrypted at rest - a readable secret lets anyone holding a database
    # dump mint valid codes, which would make MFA no barrier at all.
    mfa_secret: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)
