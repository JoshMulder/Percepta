import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from backend.api.aircraft import router as aircraft_router
from backend.api.broker import router as broker_router
from backend.api.account import router as account_router
from backend.api.auth import router as auth_router
from backend.api.commands import router as commands_router
from backend.api.enrolment import router as enrolment_router
from backend.api.media import renew_leases, router as media_router
from backend.services import audio_demand
from backend.api.organization import router as organization_router
from backend.api.platform import router as platform_router
from backend.api.station_config import router as station_config_router
from backend.api.station_enrolment import router as station_enrolment_router
from backend.api.stations import router as stations_router
from backend.api.tiles import router as tiles_router
from backend.core.config import settings, verify_secure_config, verify_signing_key
from backend.core.crypto import warn_if_unencrypted
from backend.database.session import check_database_connection
from backend.realtime.endpoint import websocket_endpoint
from backend.realtime.hub import hub
from backend.services.power_history import power_history
from backend.services.weather_history import weather_history
from backend.services.station_ingest import station_ingest

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
    verify_signing_key()
    # Refuse to boot fail-open (RLS bypassed / plaintext secrets) unless
    # ALLOW_INSECURE is set. When it is, the warnings below still fire, so a
    # deliberately-insecure local stack is loud rather than silent.
    verify_secure_config()
    warn_if_unencrypted()
    if not settings.rls_enabled:
        logger.warning(
            "Running as the schema owner - ROW-LEVEL SECURITY IS BYPASSED. Tenant "
            "isolation is only as good as the application's own query scoping. "
            "Set APP_DB_PASSWORD before this touches real data."
        )
    await hub.start()
    # Ingest before history: history reads what the ingest republishes, so
    # starting it first only means it sits idle for a moment.
    await station_ingest.start()
    await power_history.start()
    await weather_history.start()
    # Keeps watched stations streaming. Silence is the stop signal, so this
    # task existing is what makes on-demand video actually stop.
    leases = asyncio.create_task(renew_leases())
    # The same shape for airband audio, and for the same reason: a listener
    # who closes their laptop never says so, and a station transmitting to
    # nobody is the most expensive thing on the link.
    audio = asyncio.create_task(audio_demand.renew())
    try:
        yield
    finally:
        leases.cancel()
        audio.cancel()
        await power_history.stop()
        await weather_history.stop()
        await station_ingest.stop()
        await hub.stop()


app = FastAPI(title="Percepta", lifespan=lifespan)

app.include_router(account_router)
app.include_router(aircraft_router)
app.include_router(auth_router)
app.include_router(broker_router)
app.include_router(commands_router)
app.include_router(enrolment_router)
app.include_router(media_router)
app.include_router(organization_router)
app.include_router(platform_router)
app.include_router(station_config_router)
app.include_router(station_enrolment_router)
app.include_router(stations_router)
app.include_router(tiles_router)


# The CA a station has to pin, served to anyone who asks.
#
# A certificate authority's *certificate* is public by construction — it is
# handed to every station and every browser that will ever talk to this
# platform, and it authenticates nothing on its own. What must never leave the
# host is `ca.key`, which is not mounted into this container at all.
#
# This exists because provisioning a station otherwise needs somebody to scp a
# file from the platform host, and a manual step in a trust decision is a step
# that gets skipped — the failure mode being an operator who turns verification
# off instead. Fetching it here is trust-on-first-use, so `bootstrap.sh` prints
# the fingerprint and takes `--ca-fingerprint` to check it against one carried
# out of band, which is the part that makes it not TOFU.
#
# Deliberately NOT committed to the repository. This file is generated per
# platform instance; a copy in source is correct for exactly one deployment and
# confidently wrong for every other, including production.
@app.get("/ca.crt", include_in_schema=False)
async def certificate_authority() -> Response:
    ca = Path("/certs/ca.crt")
    if not ca.is_file():
        raise HTTPException(status_code=404, detail="This platform pins no CA.")
    return Response(
        content=ca.read_bytes(),
        media_type="application/x-pem-file",
        headers={
            "Content-Disposition": 'attachment; filename="platform-api-ca.pem"',
            # It changes only when the CA is rotated, which is a thing an
            # operator does deliberately and then re-provisions for.
            "Cache-Control": "public, max-age=300",
        },
    )


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "database": "up" if check_database_connection() else "down",
        # Surfaced deliberately: an operator should be able to see at a glance
        # whether the deployment they are looking at is actually enforcing
        # tenant isolation, rather than having to infer it from config.
        "rls_enabled": settings.rls_enabled,
        "demo_mode": settings.demo_mode,
        "connections": hub.connection_count(),
    }


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket_endpoint(websocket)


class ConsoleFiles(StaticFiles):
    """Static files with cache headers that suit a hashed-asset build.

    Vite fingerprints everything under /assets, so those can be cached forever.
    `index.html` must NOT be, because it is the file that names which fingerprint
    to load - a cached copy pins the browser to the previous build's assets, and
    the deploy silently does nothing. Starlette sends only ETag and
    Last-Modified by default, which lets a browser apply its own heuristic
    freshness and keep serving a stale page for as long as it likes.

    Unhashed files served from the root (the logo, the audio worklet) get the
    same treatment for the same reason.
    """

    def file_response(self, full_path, stat_result, scope: Scope, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        path = scope.get("path", "")
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # Revalidate every time. Cheap - a 304 is a few hundred bytes - and
            # it means a deploy takes effect on the next load rather than
            # whenever the browser next feels like asking.
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# The built console, if it is present. Mounted last so it never shadows /api or
# /ws, and served same-origin so the HttpOnly session cookie just works and
# there is no CORS surface at all.
#
# html=True makes unknown paths fall back to index.html, which a single-page
# app needs for deep links. Absent in a backend-only checkout (no `npm run
# build` yet), in which case the API still serves normally.
_STATIC_DIR = Path("/app/static")
if _STATIC_DIR.is_dir():
    app.mount("/", ConsoleFiles(directory=_STATIC_DIR, html=True), name="console")
else:
    logger.info("No built console at %s; serving the API only.", _STATIC_DIR)
