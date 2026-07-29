"""Password reset tokens

An admin can reset another user's password by sending them a link rather than
choosing a password on their behalf. The link is a bearer token, so only its
SHA-256 hash is stored - the value exists in the recipient's mailbox and nowhere
else.

**No row-level security on this table, deliberately.** Every other table here is
bound to an organisation, but a password belongs to the person rather than to a
tenancy, and the same user can be a member of several. More decisively, this
table is read during redemption - before the caller is authenticated and before
there is any organisation context to bind a policy to. A policy would evaluate
against an unset `app.current_org` and match nothing, which would present as
"every reset link is invalid".

What that costs is bounded: the table holds a hash, an expiry and a user id.
Reading all of it grants nothing, because a hash cannot be redeemed - and the
app role reaches it only through this application's own queries.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "percepta_app"


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        # SET NULL, not CASCADE: removing an administrator must not erase the
        # record that they reset somebody else's password.
        sa.Column(
            "requested_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_token_hash"),
    )
    op.create_index(
        "ix_password_reset_tokens_user", "password_reset_tokens", ["user_id"]
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON password_reset_tokens TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.drop_index("ix_password_reset_tokens_user", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
