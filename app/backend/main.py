import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from backend.core.config import settings
from backend.core.crypto import warn_if_unencrypted
from backend.database.session import check_database_connection
from backend.realtime.endpoint import websocket_endpoint
from backend.realtime.hub import hub

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Make our own loggers visible under uvicorn.

    start_app.py calls basicConfig, but it then execs uvicorn, which replaces
    the process and installs a log config covering only the `uvicorn.*` loggers.
    The root logger is left at WARNING with no handler, so everything this
    application logs below WARNING vanishes - including "Realtime bus connected"
    and, worse, the exception handler that reports the bus failing to start.

    Those warnings exist precisely to stop a degraded deployment looking
    healthy, so they have to actually reach the log.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)-5.5s [%(name)s] %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    warn_if_unencrypted()
    if not settings.rls_enabled:
        logger.warning(
            "Running as the schema owner - ROW-LEVEL SECURITY IS BYPASSED. Tenant "
            "isolation is only as good as the application's own query scoping. "
            "Set APP_DB_PASSWORD before this touches real data."
        )
    await hub.start()
    try:
        yield
    finally:
        await hub.stop()


app = FastAPI(title="Percepta", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "database": "up" if check_database_connection() else "down",
        # Surfaced deliberately: an operator should be able to see at a glance
        # whether the deployment they are looking at is actually enforcing
        # tenant isolation, rather than having to infer it from config.
        "rls_enabled": settings.rls_enabled,
        "connections": hub.connection_count(),
    }


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket_endpoint(websocket)
