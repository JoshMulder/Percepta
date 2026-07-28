"""Row-level security: least-privilege app role + per-org policies

Ported from DroneOps, same reasoning: the app scopes every query by
organization_id in code, and this makes a *forgotten* scope fail safe at the
database instead of leaking another tenant's rows.

Creates the percepta_app role (NOSUPERUSER, NOBYPASSRLS) the API tier connects
as, grants it plain DML (no DDL, no TRUNCATE), and puts an org-isolation policy
on every org-scoped table. The role is created NOLOGIN with no password here on
purpose - the password is a secret and is set at startup from APP_DB_PASSWORD
(see scripts/ensure_app_role.py), so rotating it is a restart, not a migration,
and no secret is ever written into this file.

Excluded tables and why:

  auth_sessions, organization_memberships
    read by the auth flow *before* an org context exists. Protecting them would
    deadlock login for no gain - the same exclusion DroneOps makes.

  users
    global, not org-scoped: one account may belong to several orgs. Visibility
    is enforced by joining through memberships in the repository layer.

  audit_logs
    written during authentication, before an org context exists (a failed login
    for an unknown email has no resolved org at all), so an INSERT policy would
    reject exactly the rows most worth keeping. Reads are filtered in the
    repository instead. Note this is a weaker guarantee than the rest of the
    schema has, and it is the one table where a missing filter would leak
    cross-org - repository access to it deserves the extra scrutiny.

Everything else, including the new real-time tables, is covered:

  ground_stations, devices, station_grants

station_grants under RLS is worth calling out. It is the table consulted to
decide what a user may do at a station, and it is read *after* the org context
is set (the org comes from the token). So it gets the policy too, and an
authorisation lookup that somehow escaped its org scope returns nothing rather
than another tenant's grants - the check fails closed.

The policy keys on two GUCs the app sets per transaction (session.py):
  app.current_org  the active org; empty => matches nothing (fail closed)
  app.bypass       'on' only for platform god-mode, which reads across orgs

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-28

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


APP_ROLE = "percepta_app"

RLS_TABLES = [
    "ground_stations",
    "devices",
    "station_grants",
]

_PREDICATE = (
    "organization_id = nullif(current_setting('app.current_org', true), '')::uuid "
    "OR coalesce(current_setting('app.bypass', true), 'off') = 'on'"
)


def upgrade() -> None:
    # Role: idempotent, NOLOGIN until startup sets a password from the env.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
            END IF;
        END
        $$;
        """
    )

    # Plain DML only - no DDL, no TRUNCATE, no ownership.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE};")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};")
    # Future tables/sequences created by the owner become usable without another
    # grant migration.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE};"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE};"
    )

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        # FORCE so the policy applies to the table owner too, not just to other
        # roles - otherwise anything running as the owner silently sees
        # everything and the isolation is only as good as which role connected.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY org_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
        )


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE};"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE};"
    )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE};")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE};")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE};")
    # The role itself is deliberately left in place: it may own grants in other
    # databases on the same cluster, and dropping it is not this migration's
    # call to make.
