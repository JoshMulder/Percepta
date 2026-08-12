"""Releases catalog

The station-image releases a platform admin has published, so the console can
offer one-click "update to latest" without an operator pasting a digest. Global
and un-RLS'd like password_reset_tokens (0008): the image is the same across
every tenant, so a release belongs to the platform, and reading "the latest tag"
grants nobody anything. The station's updater still cosign-verifies the digest
before running it — this table only records which signed image to offer.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "percepta_app"


def upgrade() -> None:
    op.create_table(
        "releases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("image", sa.String(512), nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("tag", sa.String(128), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        # SET NULL, not CASCADE: removing an administrator must not erase the
        # record of a release they published.
        sa.Column(
            "published_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # "Latest" is the most recently published row, so the resolver orders by this.
    op.create_index("ix_releases_created_at", "releases", ["created_at"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON releases TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_index("ix_releases_created_at", table_name="releases")
    op.drop_table("releases")
