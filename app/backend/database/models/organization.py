from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class Organization(UUIDMixin, TimestampMixin, Base):
    """A tenant. Owns ground stations, users are members of it.

    Much leaner than DroneOps' Organization, which carries a large amount of
    agricultural-operations configuration. Only the settings that apply to a
    monitoring platform are carried across.
    """

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    logo_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # When true, every member must complete TOTP two-factor authentication at
    # login (enrolling on first sign-in). Carried across from DroneOps as-is.
    mfa_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
