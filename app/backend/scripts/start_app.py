"""Container entrypoint: wait for Postgres, migrate, sync the app role, serve.

Migrations run here rather than in a separate one-shot service so a fresh
`docker compose up` produces a working system with no manual step. They run as
the schema owner; the API tier then connects as the least-privilege role.

Order matters. ensure_app_role must run *after* the migration that creates the
role, and before uvicorn starts, or the first request connects with a stale (or
absent) app-role password and silently falls back to the owner - which is the
one failure mode that looks fine and isn't.
"""

import logging
import os
import subprocess
import sys
import time

from sqlalchemy import text

from backend.core.config import settings
from backend.database.session import privileged_engine

logging.basicConfig(
    level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s"
)
logger = logging.getLogger("startup")

APP_DIR = "/app"


def wait_for_postgres(timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with privileged_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Postgres is up.")
            return
        except Exception as exc:
            if time.monotonic() >= deadline:
                logger.error("Postgres not reachable after %ss: %s", timeout, exc)
                sys.exit(1)
            time.sleep(1)


def run_migrations() -> None:
    logger.info("Applying migrations.")
    result = subprocess.run(
        ["alembic", "upgrade", "head"], cwd=APP_DIR, capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("Migrations failed:\n%s\n%s", result.stdout, result.stderr)
        sys.exit(1)
    logger.info("Migrations applied.")


def main() -> None:
    wait_for_postgres()
    run_migrations()

    from backend.scripts.ensure_app_role import ensure_app_role

    ensure_app_role()

    if not settings.rls_enabled:
        logger.warning(
            "APP_DB_PASSWORD is unset - starting with ROW-LEVEL SECURITY BYPASSED."
        )

    port = os.environ.get("APP_PORT", "8000")
    workers = os.environ.get("WEB_CONCURRENCY", "1")
    logger.info("Starting uvicorn on :%s (%s worker(s)).", port, workers)
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "backend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
            "--workers",
            workers,
        ],
    )


if __name__ == "__main__":
    main()
