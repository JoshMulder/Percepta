"""Tenant isolation at the database layer.

Ported near-verbatim from DroneOps (app/backend/database/session.py). The
approach is deliberately unchanged: enforcement lives *below* the application,
in Postgres row-level security, so a query that forgets its org filter returns
nothing rather than everything. Every comment explaining a non-obvious choice
below is inherited because the reasoning still holds.

Note what this does NOT cover. RLS protects database reads. Live telemetry,
video and radio audio never pass through Postgres, so none of this constrains
them - see docs/03-realtime-isolation.md for how the same fail-closed property
is reconstructed for streams.
"""

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from backend.core.config import settings

# Two connections, two privilege levels:
#
#   privileged_engine - the schema owner. Migrations, seeding, device enrolment
#     and background workers use this. It bypasses row-level security, which
#     those paths legitimately need (a worker ingests any org's telemetry).
#
#   app_engine - the least-privilege role (NOSUPERUSER, NOBYPASSRLS) the
#     web/API tier uses. RLS actually constrains it. When no app-role password
#     is configured this is the same URL as the privileged engine, so the app
#     keeps working with RLS bypassed (start-up warns) until the role is
#     provisioned.
privileged_engine = create_engine(settings.database_url, pool_pre_ping=True)

# Behind PgBouncer in transaction-pooling mode, server connections rotate per
# transaction, so psycopg3's server-side prepared statements (auto-prepared
# after a few executions) would be looked up on a connection that no longer has
# them. Disable auto-prepare on the app engine when pooled. Our RLS context uses
# transaction-local SET LOCAL (re-applied each transaction), which IS compatible
# with transaction pooling.
_app_connect_args = {"prepare_threshold": None} if settings.pgbouncer_enabled else {}
app_engine = create_engine(
    settings.app_database_url, pool_pre_ping=True, connect_args=_app_connect_args
)

engine = privileged_engine


# The active org rides on the DBAPI connection's `.info`, not a ContextVar.
# Under FastAPI a sync dependency (get_current_user) and the endpoint run in
# separate threadpool executions, each with its own copy of the context, so a
# ContextVar set in the dependency never reaches the endpoint. The connection is
# the one thing they genuinely share (Depends(get_db) is cached per request), so
# it's where the org has to live.
_ORG_KEY = "rls_current_org"
_BYPASS_KEY = "rls_bypass"


@event.listens_for(app_engine, "begin")
def _apply_org_context(conn):
    """Stamp the connection's org onto every transaction it opens.

    Re-applies on each new transaction (so context survives an intra-request
    commit, e.g. the ORM's post-commit refresh), reading from connection.info
    which persists for the connection's whole request. Transaction-local
    (is_local true) so nothing leaks past the transaction; the checkin reset
    below clears info when the connection returns to the pool.

    Default - no org set - is an empty org and bypass off, under which the
    policies match nothing: an unscoped query fails closed, not open.
    """
    org = conn.info.get(_ORG_KEY, "")
    bypass = conn.info.get(_BYPASS_KEY, False)
    # ::text casts are required or Postgres can't infer the placeholder types.
    conn.exec_driver_sql(
        "SELECT set_config('app.current_org', %s::text, true), "
        "set_config('app.bypass', %s::text, true)",
        (org or "", "on" if bypass else "off"),
    )


@event.listens_for(app_engine, "checkin")
def _clear_org_context(dbapi_connection, connection_record):
    """Wipe the org when the connection returns to the pool, so it can never
    carry one request's tenant into the next."""
    connection_record.info.pop(_ORG_KEY, None)
    connection_record.info.pop(_BYPASS_KEY, None)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=app_engine)

PrivilegedSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=privileged_engine
)


def set_request_org_context(db, *, organization_id, bypass: bool) -> None:
    """Bind the current request's org to its DB connection.

    Called from get_current_user once the active org is known. Stashes the org
    on the connection (so transactions opened later in the request re-apply it
    via the begin event) and force-applies to the already-open transaction,
    which began earlier - during auth's own reads - before the org was known.
    """
    connection = db.connection()
    connection.info[_ORG_KEY] = str(organization_id)
    connection.info[_BYPASS_KEY] = bypass
    db.execute(
        text(
            "SELECT set_config('app.current_org', :org ::text, true), "
            "set_config('app.bypass', :bypass ::text, true)"
        ),
        {"org": str(organization_id), "bypass": "on" if bypass else "off"},
    )


def check_database_connection() -> bool:
    try:
        with privileged_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
