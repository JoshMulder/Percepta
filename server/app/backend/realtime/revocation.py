"""Making access changes reach connections that are already open.

The problem this solves (docs/03-realtime-isolation.md section 6): DroneOps
revokes by invalidating a row in auth_sessions, so the *next* HTTP request
fails. That works because HTTP requests are short and frequent. A monitoring
WebSocket makes no further requests and can stay open for hours - so without
this, logging out would leave live video running.

Two overlapping mechanisms, and both are needed:

  push (here)   immediate, but fails silently if a worker misses the message
  poll (hub)    every STREAM_REVALIDATE_SECONDS, bounds the worst case

Push alone would be a guarantee that quietly degrades to nothing on a dropped
connection. Poll alone leaves a minute of unauthorised streaming. Together the
common case is instant and the worst case is bounded.

Publishing is sync so any request handler, repository or service can raise an
event without an event loop. Applying is async, on each worker's bus reader.
"""

import logging
import uuid
from typing import TYPE_CHECKING

from backend.realtime import media
from backend.realtime.bus import REVOKE_CHANNEL, publish_sync

if TYPE_CHECKING:
    from backend.realtime.hub import Hub

log = logging.getLogger(__name__)

# What changed. Each maps to how much has to be re-checked - deliberately
# coarse, because re-checking too much costs one database round trip and
# re-checking too little leaves access open.
KIND_SESSION = "session"          # one login ended: close its connections
KIND_USER = "user"                # roles/membership/certification changed
KIND_STATION = "station"          # deactivated, deleted, or re-scoped
KIND_ORGANIZATION = "organization"  # org-wide change
KIND_GRANT = "grant"              # a station_grant was written or removed


def _publish(kind: str, target_id: uuid.UUID, **extra) -> bool:
    return publish_sync(
        REVOKE_CHANNEL, {"kind": kind, "id": str(target_id), **extra}
    )


def revoke_session(session_id: uuid.UUID) -> bool:
    """Logout, sign-out-everywhere, password change."""
    return _publish(KIND_SESSION, session_id)


def revoke_user(user_id: uuid.UUID) -> bool:
    """Roles changed, membership removed, account disabled, certification
    lapsed. Does not close connections outright - revalidation decides, because
    the user may legitimately keep a subset of what they had."""
    return _publish(KIND_USER, user_id)


def grants_changed(user_id: uuid.UUID) -> bool:
    """A station grant was created, edited or removed for this user."""
    return _publish(KIND_GRANT, user_id)


def station_changed(ground_station_id: uuid.UUID) -> bool:
    """Deactivated or otherwise changed in a way that affects who may see it."""
    return _publish(KIND_STATION, ground_station_id)


def organization_changed(organization_id: uuid.UUID) -> bool:
    return _publish(KIND_ORGANIZATION, organization_id)


async def apply_revocation(hub: "Hub", event: dict) -> None:
    """Apply an event to this worker's connections.

    Everything except an ended session goes through the hub's ordinary
    revalidation, which already knows how to drop the subscriptions whose
    capability has gone while leaving the rest alone. Revocation should narrow
    access to whatever is still permitted, not disconnect people whose access
    merely changed.
    """
    kind = event.get("kind")
    raw_id = event.get("id")
    try:
        target = uuid.UUID(str(raw_id))
    except (TypeError, ValueError):
        log.warning("Ignoring revocation event with a bad id: %r", raw_id)
        return

    if kind == KIND_SESSION:
        for conn in hub.connections_for_session(target):
            hub.close_connection(conn, reason="session ended")
        # Media viewers are not hub connections - they are keyed by ticket, on
        # their own socket - so they need telling separately. Without this the
        # camera keeps running in a signed-out tab until the viewer's own poll
        # comes round, which is up to a minute on the most expensive stream
        # the platform carries.
        stopped = media.close_session_viewers(target)
        if stopped:
            log.info("Closed %d media viewer(s) for an ended session.", stopped)
        return

    if kind in (KIND_USER, KIND_GRANT):
        affected = hub.connections_for_user(target)
    elif kind == KIND_STATION:
        affected = hub.connections_for_station(target)
    elif kind == KIND_ORGANIZATION:
        affected = hub.connections_for_organization(target)
    else:
        log.warning("Ignoring unknown revocation kind: %r", kind)
        return

    for conn in affected:
        try:
            if not await hub.revalidate(conn):
                hub.close_connection(conn, reason="session ended")
        except Exception:
            log.exception("Revalidation failed while applying %s; closing.", kind)
            hub.close_connection(conn, reason="revalidation failed")
