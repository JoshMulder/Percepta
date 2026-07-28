"""Sync the least-privilege app DB role's login password from the environment.

Ported from DroneOps. The RLS migration creates the percepta_app role NOLOGIN
with no password, so no secret lives in a migration file. This runs at startup on
the privileged connection and reconciles the role's login state with
APP_DB_PASSWORD:

  set   -> role gets LOGIN and that password. Rotating the secret is a restart.
  unset -> role is left/made NOLOGIN, and the app falls back to the owner role
           (RLS bypassed, warned separately). Nothing can log in as the app role
           with a stale password.

Idempotent and safe to run every boot.
"""

import logging

from sqlalchemy import text

from backend.core.config import settings
from backend.database.session import privileged_engine

logger = logging.getLogger(__name__)


def ensure_app_role() -> None:
    role = settings.app_db_user
    with privileged_engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
        if not exists:
            # The migration creates it; if it's missing, RLS hasn't been applied
            # yet. Nothing to sync - the app will use the owner role meanwhile.
            logger.info(
                "App DB role %s does not exist yet (RLS migration not applied?); "
                "skipping password sync.",
                role,
            )
            return

        if settings.rls_enabled:
            # ALTER ROLE is DDL and can't take a bind parameter for the
            # password, so quote it into the statement. The role name is our own
            # constant; the password is escaped by Postgres' own quote_literal
            # rather than by hand, so it's injection-safe whatever it contains.
            quoted_pw = conn.execute(
                text("SELECT quote_literal(:pw)"), {"pw": settings.app_db_password}
            ).scalar()
            conn.exec_driver_sql(f"ALTER ROLE {role} WITH LOGIN PASSWORD {quoted_pw}")
            logger.info("Synced login password for app DB role %s.", role)
        else:
            conn.exec_driver_sql(f"ALTER ROLE {role} WITH NOLOGIN")
            logger.warning(
                "APP_DB_PASSWORD unset - app DB role %s left NOLOGIN; the app will "
                "use the owner role and RLS is BYPASSED. Set APP_DB_PASSWORD to "
                "enable database-enforced tenant isolation.",
                role,
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_app_role()
