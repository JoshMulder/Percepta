"""Email change tokens

Self-service email change, confirmed by a link sent to the new address. Same
shape and rationale as password_reset_tokens (0008): a bearer token stored only
as a SHA-256 hash, and no row-level security because the table is read during
redemption — before the caller is authenticated and before any organisation
context exists to bind a policy to. The one extra column is `new_email`, the
address the link was sent to and the one the account moves to on redemption.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "percepta_app"


def upgrade() -> None:
    op.create_table(
        "email_change_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("new_email", sa.String(320), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_email_change_token_hash"),
    )
    op.create_index(
        "ix_email_change_tokens_user", "email_change_tokens", ["user_id"]
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON email_change_tokens TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.drop_index("ix_email_change_tokens_user", table_name="email_change_tokens")
    op.drop_table("email_change_tokens")
