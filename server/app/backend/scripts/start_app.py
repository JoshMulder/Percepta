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

from backend.api.broker import MAX_FRAME_BYTES as BROKER_MAX_FRAME_BYTES
from backend.api.media import MAX_FRAME_BYTES as MEDIA_MAX_FRAME_BYTES
from backend.core.config import settings
from backend.database.session import privileged_engine

logging.basicConfig(
    level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s"
)
logger = logging.getLogger("startup")

APP_DIR = "/app"

#: What each endpoint enforces for itself, imported so this file cannot claim a
#: number the code does not. The relay's is the contract's 512 KiB; the media
#: path's is sized for H.264. Both are authoritative for their own socket.
RELAY_MAX_FRAME_BYTES = BROKER_MAX_FRAME_BYTES

#: Slack between those caps and uvicorn's, so that a frame refused anywhere is
#: refused by the endpoint rather than by the server. A frame in this band gets
#: `broker.py` or `media.py` logging which station sent how many bytes and
#: closing 1009; above it, uvicorn closes a socket it cannot describe.
WS_HEADROOM_BYTES = 256 * 1024

#: Uvicorn's websocket frame cap.
#:
#: **This cannot be the relay's 512 KiB, and setting it there broke video
#: entirely.** Uvicorn applies one limit to every WebSocket on the app, and two
#: endpoints have completely different needs: the station relay carries JSON
#: telemetry where 512 KiB is a generous ceiling, and `/media/ingest` carries
#: H.264 where a single fMP4 segment of three megabytes is ordinary. Sharing
#: the relay's number closed the media socket on the first real frame — the
#: station logged `frame with 3149876 bytes exceeds limit of 524288` every two
#: seconds and no video ever flowed.
#:
#: So this is derived from the largest endpoint cap and deliberately sits a
#: little above it. **Every socket now enforces its own limit in its own
#: module** — the relay always did, and `/media/ingest` does since the flag
#: became its only bound. This is the backstop for the fact that those checks
#: run *after* `receive()` has allocated, and nothing more: any frame either
#: endpoint would refuse is refused there first, where the log can name the
#: station and the close code means something.
#:
#: The lesson is worth keeping: a per-app knob cannot express a per-endpoint
#: rule, and taking the number "from the relay's own so the two cannot drift"
#: made them agree about the wrong thing. Deriving it from the widest of the
#: per-endpoint rules is the version of "cannot drift" that holds.
WS_MAX_SIZE = max(RELAY_MAX_FRAME_BYTES,
                  MEDIA_MAX_FRAME_BYTES) + WS_HEADROOM_BYTES


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

    # After the app role, before serving: it writes with the privileged engine
    # and the API must not accept a request before a first admin can exist.
    from backend.scripts.ensure_platform_admin import ensure_platform_admin

    ensure_platform_admin()

    # Redis keeps ACL users in memory only, so a broker restart would silently
    # lock out every station until each happened to re-enrol. Rebuilt here from
    # the credential hashes in Postgres, which needs no plaintext secret.
    try:
        from backend.database.session import PrivilegedSessionLocal
        from backend.services import broker_acl

        with PrivilegedSessionLocal() as db:
            count = broker_acl.sync_all(db)
        logger.info("Broker principals synchronised for %d station(s).", count)
    except Exception:
        logger.exception(
            "Could not synchronise broker principals. Stations may be unable to "
            "authenticate until this is retried."
        )

    if not settings.rls_enabled:
        logger.warning(
            "APP_DB_PASSWORD is unset - starting with ROW-LEVEL SECURITY BYPASSED."
        )

    port = os.environ.get("APP_PORT", "8000")
    workers = os.environ.get("WEB_CONCURRENCY", "1")

    # Two subsystems are in-process by design and say so where they live:
    # `realtime/media.py` (the video relay) and `api/media.py` (`_tickets`).
    # Both are correct — the socket a ticket authorises has to land on the
    # worker holding the stream anyway — but neither survives being forked.
    #
    # Above one worker, a ticket issued on worker A cannot be redeemed on
    # worker B, and a viewer routed away from the worker holding the station's
    # ingest socket sees nothing. Neither fails cleanly: they fail for a
    # fraction of viewers, non-deterministically, which reads as "video is
    # flaky" and sends you looking at the camera.
    #
    # Warned rather than refused: raising the worker count is a legitimate
    # thing to want, and somebody who has read this may be doing it knowing
    # video is not in use.
    try:
        if int(workers) > 1:
            logger.warning(
                "WEB_CONCURRENCY=%s - VIDEO WILL BE UNRELIABLE. Stream tickets "
                "and the media relay are per-process, so a viewer that lands "
                "on the wrong worker gets no picture and no error. Everything "
                "else fans out through Redis and is unaffected.", workers,
            )
    except ValueError:
        logger.warning("WEB_CONCURRENCY=%r is not a number; uvicorn will "
                       "decide what to do with it.", workers)

    argv = [
        "uvicorn",
        "backend.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        port,
        "--workers",
        workers,
        # A backstop above every endpoint's own cap — see WS_MAX_SIZE. The
        # relay enforces 512 KiB in broker.py and the media path 8 MiB in
        # media.py; neither is affected by this number.
        "--ws-max-size",
        str(WS_MAX_SIZE),
        # Socket liveness, and the numbers are the contract's — the timings
        # table in `contract/transport.md` states 20 s idle and a 10 s pong
        # deadline, and both ends have to agree on them.
        #
        # It has to be set here rather than in the Dockerfile, which is what it
        # was: compose overrides the image's CMD with this script, so a `CMD`
        # carrying these flags is inert and silently so. Uvicorn's own defaults
        # are 20/20 — the interval already matched and the timeout did not, so
        # a station whose NAT mapping was dropped went unnoticed for twice as
        # long as the contract allows.
        #
        # This is also the only place it *can* be set: Starlette exposes no
        # access to the ping machinery, so the relay endpoint cannot do it.
        "--ws-ping-interval",
        "20",
        "--ws-ping-timeout",
        "10",
    ]

    # Behind a reverse proxy by default: the proxy terminates TLS and this
    # serves plain HTTP on the internal network only.
    #
    # --proxy-headers is not cosmetic. Without it every request appears to come
    # from the proxy, so the audit log records the proxy's address for every
    # login and every command issued to hardware - which is exactly the field
    # you need when something has gone wrong. forwarded-allow-ips must name the
    # proxy rather than "*", because a client can set X-Forwarded-For itself and
    # trusting it from anywhere lets anyone write whatever address they like
    # into the audit trail.
    argv += ["--proxy-headers", "--forwarded-allow-ips",
             os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")]

    # Direct TLS remains available for a deployment with no proxy in front.
    cert = os.environ.get("APP_TLS_CERT", "")
    key = os.environ.get("APP_TLS_KEY", "")
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        argv += ["--ssl-certfile", cert, "--ssl-keyfile", key]
        logger.info("Starting uvicorn on :%s over TLS (%s worker(s)).", port, workers)
    else:
        logger.info(
            "Starting uvicorn on :%s in plain HTTP for a TLS-terminating proxy "
            "(%s worker(s)). Nothing but the proxy should be able to reach this "
            "port: enrolment tokens and station credentials cross it in clear.",
            port, workers,
        )
    os.execvp("uvicorn", argv)


if __name__ == "__main__":
    main()
