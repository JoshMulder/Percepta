"""Station enrolment: tokens, credentials, and reported hardware

Until now a station record existed but nothing could authenticate as one. This
adds the two tables the enrolment lifecycle needs (contract/enrolment.md) and
two columns on ground_stations.

Both new tables carry organization_id and go under RLS with the same predicate
as everything else operational. That is worth stating plainly: a credential is
the thing that decides which tenant's data a box may publish, so a query against
it that escaped its org scope would be the most damaging kind of leak in this
schema. It fails closed like the rest.

Secrets are stored hashed, never encrypted-and-recoverable. A Fernet column
would let the platform hand out a customer's station credential; a hash cannot.
The inputs are CSPRNG output, so SHA-256 is sound here in a way it would not be
for a password.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "percepta_app"

_PREDICATE = (
    "organization_id = nullif(current_setting('app.current_org', true), '')::uuid "
    "OR coalesce(current_setting('app.bypass', true), 'off') = 'on'"
)

_TABLES = ["station_enrolment_tokens", "station_credentials"]


def upgrade() -> None:
    op.add_column(
        "ground_stations",
        sa.Column("hardware", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "ground_stations",
        sa.Column(
            "config_version", sa.Integer(), nullable=False, server_default="1"
        ),
    )

    op.create_table(
        "station_enrolment_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "ground_station_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ground_stations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "issued_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.UniqueConstraint("token_hash", name="uq_enrolment_token_hash"),
    )
    op.create_index(
        "ix_enrolment_tokens_station",
        "station_enrolment_tokens",
        ["ground_station_id"],
    )
    op.create_index(
        "ix_enrolment_tokens_org", "station_enrolment_tokens", ["organization_id"]
    )

    op.create_table(
        "station_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "ground_station_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ground_stations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind", sa.String(length=16), nullable=False, server_default="bearer"
        ),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "broker_provisioned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.UniqueConstraint("secret_hash", name="uq_station_credential_hash"),
    )
    op.create_index(
        "ix_station_credentials_station",
        "station_credentials",
        ["ground_station_id"],
    )
    op.create_index(
        "ix_station_credentials_org", "station_credentials", ["organization_id"]
    )

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_org_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
    op.drop_table("station_credentials")
    op.drop_table("station_enrolment_tokens")
    op.drop_column("ground_stations", "config_version")
    op.drop_column("ground_stations", "hardware")
