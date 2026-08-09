"""Row-level security for station_events, the table 0011 left uncovered

Every other org-scoped table carries an RLS policy — ground_stations, devices
and station_grants from 0002, power_samples from 0005, weather_samples from
0014 — but station_events (0011) was created with an organization_id and its
indexes and never got `ENABLE/FORCE ROW LEVEL SECURITY` or a policy. So its
tenant isolation rested entirely on the application remembering to scope every
query, with nothing underneath: one forgotten `.where(organization_id == ...)`
and a station's airband transcripts and proximity events read across tenants.
This closes that, with the same predicate and the same FORCE as the rest.

FORCE, so the policy binds the table owner too — otherwise anything running as
the schema owner sees every org's events and the isolation is only as strong as
which role happened to connect. The app tier connects as percepta_app
(NOBYPASSRLS), so in production the policy bites; FORCE is what makes it bite in
any tooling that runs as the owner as well.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-10

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The same GUC-keyed predicate every org-scoped table uses (session.py sets the
# two settings per transaction): the active org matches, or platform god-mode is
# on. An empty/absent org setting matches nothing, so a query with no context
# fails closed rather than open.
_PREDICATE = (
    "organization_id = nullif(current_setting('app.current_org', true), '')::uuid "
    "OR coalesce(current_setting('app.bypass', true), 'off') = 'on'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE station_events ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE station_events FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY org_isolation ON station_events "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE});"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON station_events;")
    op.execute("ALTER TABLE station_events NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE station_events DISABLE ROW LEVEL SECURITY;")
