import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.database.models.common import TimestampMixin, UUIDMixin


class Release(UUIDMixin, TimestampMixin, Base):
    """One published station-image release: a signed image pinned by digest.

    The catalog that makes one-click "update to latest" possible. The operator
    never types a digest; a platform admin publishes a release here (the image,
    the immutable digest, and a human tag) once, and the station's own updater
    still cosign-verifies that digest against its pinned keys before running it.
    So this table only chooses WHICH signed image the fleet is offered — it is
    never the trust anchor for whether an image is signed.

    **Global, not org-scoped.** The station image is the same across every
    tenant, so a release belongs to the platform rather than an organisation, and
    like `password_reset_tokens` it carries no RLS policy — publishing is platform
    work and reading "the latest tag" grants nobody anything. "Latest" is simply
    the most recently published row (`created_at` desc).
    """

    __tablename__ = "releases"

    #: The repository, e.g. registry.percepta.nz/percepta-gsu.
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The immutable pin the station pulls and verifies — sha256:<64 hex>.
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    #: A human label carried through for the record, e.g. v0.3.0.
    tag: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Optional release notes, shown when choosing to update.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The platform admin who published it. SET NULL — removing an admin must not
    #: erase the record of a release they cut.
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
