"""Remote console: a platform admin reaches a station's own setup page.

Two routes, the same split the media path has (`api/media.py`):

    WS   /console/ingest                                    station → platform
    *    /api/platform/stations/{id}/console/{path}         admin → platform

**Why this exists.** A field station is behind Starlink CGNAT with no inbound
route, and the full on-box setup surface — enrolment, the per-slot device
inventory, radio config, location, transcription, factory reset, logs — is
served by the station itself on `127.0.0.1:8088` and reachable only from the box.
The platform console (`SettingsRadio`/`SettingsStation`/`SettingsEnrolment`) has
a deliberate subset; the point of this is the rest. So the station opens a socket
*outward* to `/console/ingest`, and the platform re-originates an admin's browser
requests down it — the station's own console, tunnelled home.

**The station side authenticates with the station credential**, exactly as
`/media/ingest` does: a bearer secret, the station id derived from it rather than
sent, close 4401 on refusal. **The admin side is `require_platform_admin` on
every request**, and the station is resolved *in code* — the guard takes no
station id and a platform-admin session bypasses RLS, so a privileged lookup by
id is how the station (and the tenant it belongs to) is found.

**Terminate and re-originate.** The relay (`realtime/console.py`) never forwards
a raw socket; it holds the station's ingest connection and multiplexes
request/response frames over it. This endpoint is where a browser HTTP request
becomes a request frame and a response frame becomes an HTTP reply — and where
the reply is adapted so the box's console, which was written to be served at a
site's `http://<box>:8088/`, works framed under this path instead (see
`_adapt_response`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.websockets import WebSocketDisconnect
from starlette.responses import Response

from backend.auth.identity import Identity
from backend.auth.platform import PLATFORM_ORGANIZATION_ID, require_platform_admin
from backend.database.models.ground_station import GroundStation
from backend.database.session import PrivilegedSessionLocal
from backend.realtime.bus import command_channel, publish_sync
from backend.realtime.console import (
    MAX_FRAME_BYTES,
    ConsoleError,
    ConsoleResponse,
    relay,
)
from backend.services import enrolment
from backend.services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(tags=["console"])

#: How long the station is asked to keep its console socket open per `open`, in
#: seconds. The station resets this window on every request it serves, so an
#: admin actively using the page keeps it alive; when the tab closes, polling
#: stops and the window lapses. Longer than a comfortable pause between clicks,
#: far shorter than "left open on a laptop overnight".
LEASE_SECONDS = 300

#: Request headers forwarded to the station's loopback console. Deliberately a
#: tiny allowlist. `Cookie` carries the console's own CSRF session so a rendered
#: form's token still matches on POST; `Content-Type` lets it parse a form body.
#: Everything else — and `Origin` and `Host` in particular — is dropped: the
#: station re-originates against `127.0.0.1`, and a forwarded browser `Origin`
#: would trip the console's same-origin guard (`console._same_origin`) and refuse
#: every POST, while a forwarded `Host` would trip its DNS-rebinding check.
#: `Cookie` is forwarded but filtered — see `_console_cookie`.
_FORWARD_REQUEST_HEADERS = frozenset({"content-type"})

#: The station console's own session cookie (`setup_access.COOKIE_NAME`). It is
#: the *only* cookie forwarded to the box, and that filtering is a real control,
#: not tidiness: the browser sends the platform session cookie on these requests
#: too, and a station is a field box that could be compromised — handing it the
#: admin's platform credential would let it replay that session and become a
#: platform admin. So the box sees its own CSRF cookie and nothing else.
_CONSOLE_COOKIE = "gsu_setup"

#: Response headers dropped before re-originating. `Content-Length` and
#: `Transfer-Encoding` describe the station's framing of a body we are re-sending
#: with our own; `X-Frame-Options: DENY` would stop the admin console framing the
#: page at all, and the CSP's `frame-ancestors` is relaxed for the same reason in
#: `_adapt_response`.
_DROP_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "x-frame-options"}
)


# --- the station side ----------------------------------------------------


@router.websocket("/console/ingest")
async def console_ingest(websocket: WebSocket) -> None:
    """A station opening its setup console outward, on demand.

    Authenticated with the station credential — the same bearer secret the
    broker and `/media/ingest` take, the station id derived from it and never
    sent, so a box holding a valid secret still cannot claim to be another
    station. The socket carries request frames down and response frames up, both
    JSON, multiplexed by request id (`realtime/console.py`).
    """
    auth = websocket.headers.get("authorization", "")
    secret = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not secret:
        await websocket.close(code=4401)
        return

    with PrivilegedSessionLocal() as db:
        found = enrolment.authenticate(db, secret=secret)
        if found is None:
            await websocket.close(code=4401)
            return
        station, credential = found
        station_id = station.id
        organization_id = station.organization_id
        credential_id = credential.id
        db.commit()

    await websocket.accept()
    link = relay.station_connected(
        station_id, organization_id, send=websocket.send_text
    )
    log.info("Console ingest open for station %s.", station_id)
    # The same once-at-connect revocation gap the broker and media path close:
    # a decommissioned box must not keep an admin's reach into it open just
    # because the socket happens to stay up.
    revoked = asyncio.create_task(
        enrolment.close_when_revoked(
            credential_id, station_id, lambda: websocket.close(code=4401)
        )
    )
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                continue
            if len(text) > MAX_FRAME_BYTES:
                log.warning(
                    "Console frame from station %s is %d bytes; the cap is %d. "
                    "Closing.", station_id, len(text), MAX_FRAME_BYTES,
                )
                await websocket.close(code=1009)
                return
            _handle_response_frame(link, station_id, text)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Console ingest failed for station %s.", station_id)
    finally:
        revoked.cancel()
        relay.station_gone(station_id, link)
        log.info("Console ingest closed for station %s.", station_id)


def _handle_response_frame(link, station_id: uuid.UUID, text: str) -> None:
    """One response (or error) frame from the station, resolved to its waiter."""
    try:
        frame = json.loads(text)
    except ValueError:
        log.warning("Dropping a malformed console frame from station %s.", station_id)
        return
    if not isinstance(frame, dict):
        return
    request_id = frame.get("id")
    if not isinstance(request_id, int):
        return
    if frame.get("t") == "err":
        link.fail(request_id, str(frame.get("error") or "the station errored"))
        return
    if frame.get("t") != "resp":
        return
    try:
        body = base64.b64decode(frame.get("body_b64") or "")
    except (ValueError, TypeError):
        link.fail(request_id, "the station sent an undecodable body")
        return
    headers = frame.get("headers")
    link.resolve(
        request_id,
        ConsoleResponse(
            status=int(frame.get("status") or 502),
            headers=headers if isinstance(headers, dict) else {},
            body=body,
        ),
    )


# --- the admin side ------------------------------------------------------


def _resolve_station(station_id: uuid.UUID) -> GroundStation:
    """Find the station for a platform admin, across tenants.

    `require_platform_admin` takes no station id, and a platform-admin session
    bypasses RLS — so this reads privileged, by id, and is the single place the
    station (and the organisation whose box is about to be reached) is resolved.
    A missing or decommissioned station is a 404, not a puzzle.
    """
    with PrivilegedSessionLocal() as db:
        station = db.get(GroundStation, station_id)
        if station is None or not station.is_active:
            raise HTTPException(status_code=404, detail="No such station")
        # Detached copies of the two fields the caller needs; the session closes
        # with this block.
        return SimpleStation(id=station.id, organization_id=station.organization_id)


class SimpleStation:
    """The two fields the proxy needs, lifted out of the ORM session."""

    __slots__ = ("id", "organization_id")

    def __init__(self, id: uuid.UUID, organization_id: uuid.UUID) -> None:
        self.id = id
        self.organization_id = organization_id


@router.api_route(
    "/api/platform/stations/{station_id}/console/{path:path}",
    methods=["GET", "POST", "HEAD"],
)
async def console_proxy(
    station_id: uuid.UUID,
    path: str,
    request: Request,
    identity: Identity = Depends(require_platform_admin),
) -> Response:
    """Re-originate one browser request onto the station's loopback console.

    Opens the station's console socket on demand if it is not already up:
    `console.open` on the command channel is the station's cue to dial back
    (`video.start` is the same shape), and the request then waits for the socket.
    A station that has not opted in, or is offline, never dials back and this
    answers 504 — an honest "the box did not open its console" rather than a hang.
    """
    station = _resolve_station(station_id)

    link = relay.get(station_id)
    if link is None:
        # Ask the station to open its console, audit that we did, and wait. The
        # audit is written here — on the transition from no-socket to socket —
        # so "who reached into this box, and when" has exactly one row per
        # session rather than one per asset the page then loads.
        _audit_open(request, identity, station)
        if not publish_sync(
            command_channel(station_id),
            {"kind": "console.open", "lease_seconds": LEASE_SECONDS},
        ):
            raise HTTPException(
                status_code=503, detail="Could not reach the station right now"
            )
        link = await relay.wait_for(station_id)
        if link is None:
            raise HTTPException(
                status_code=504,
                detail=(
                    "The station did not open its console. It may be offline, or "
                    "remote console access may not be enabled on it."
                ),
            )

    base = f"/api/platform/stations/{station_id}/console"
    station_path = "/" + path
    if request.url.query:
        station_path += "?" + request.url.query

    forwarded: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower == "cookie":
            console_cookie = _console_cookie(value)
            if console_cookie:
                forwarded["cookie"] = console_cookie
        elif lower in _FORWARD_REQUEST_HEADERS:
            forwarded[name] = value
    body = await request.body()

    try:
        response = await link.request(
            request.method, station_path, forwarded, body
        )
    except ConsoleError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _adapt_response(response, base)


def _console_cookie(cookie_header: str) -> str:
    """Keep only the station console's own cookie from the browser's `Cookie`.

    See `_CONSOLE_COOKIE`: the platform session cookie rides the same header and
    must never reach the box.
    """
    kept = []
    for morsel in cookie_header.split(";"):
        name, _, value = morsel.strip().partition("=")
        if name == _CONSOLE_COOKIE:
            kept.append(f"{name}={value}")
    return "; ".join(kept)


def _audit_open(
    request: Request, identity: Identity, station: SimpleStation
) -> None:
    """Record a platform admin opening a station's on-box console.

    Written against the *station's* organisation, not the platform org, so the
    row sits with the tenant whose box was reached — which is where an incident
    review looks — while the actor is the platform admin who reached it. Like
    every audit write, it can never block the action (`services/audit.py`).
    """
    record(
        action="console_open",
        organization_id=station.organization_id,
        actor_user_id=identity.user_id,
        target_type="ground_station",
        target_id=str(station.id),
        ground_station_id=station.id,
        ip_address=request.client.host if request.client else None,
        detail={"via": "platform", "platform_org": str(PLATFORM_ORGANIZATION_ID)},
    )


# --- making the box's console work under this path ------------------------

#: Root-relative URLs in link/form/asset attributes. The station's console was
#: written to be served at a site's `http://<box>:8088/`, so it links to
#: `/connection`, posts to `/enrol`, and points `<img src="/frame.jpg">` — all of
#: which resolve to the *platform* root when the page is framed under
#: `/api/platform/…/console/`. Rewriting the attribute value to carry the console
#: base is what keeps navigation and forms inside the frame.
#:
#: Deliberately an attribute allowlist (href/src/action/formaction/poster), not
#: every quoted `/…` on the page: the Devices tab renders `/dev/ttyUSB0` device
#: paths as text and in `value="…"` inputs, and a blanket rewrite would corrupt
#: them. `(?![/])` leaves protocol-relative `//host` URLs alone.
_ATTR_URL = re.compile(rb"""\b(href|src|action|formaction|poster)=(["'])/(?![/])""")

#: The endpoint URLs the console's inline scripts fetch as string literals
#: (`fetch("/status.json")`, `still.src = "/frame.jpg?t=" + …`). Matched by name
#: so a `?query` after them is preserved and nothing else that starts with `/`
#: is touched.
_SCRIPT_URL = re.compile(
    rb"""(["'])/(status\.json|registry\.json|stream\.mp4|audio\.wav|frame\.jpg)"""
)


def _adapt_response(response: ConsoleResponse, base: str) -> Response:
    """Turn the station's console reply into one an admin's browser can use.

    Three adaptations, all forced by the console having been written for a box's
    own origin rather than for this subpath:

      * `text/html` bodies have their root-relative URLs rewritten to the base,
        so links, forms and the script's own fetches stay inside the frame.
      * `X-Frame-Options` is dropped and the CSP's `frame-ancestors 'none'` is
        relaxed to `'self'`, so the platform console (same origin) may frame it.
        Nothing else in the console's CSP is loosened — its per-response script
        nonce is preserved untouched.
      * `Set-Cookie` and any redirect `Location` are re-pathed onto the base, so
        the console's CSRF cookie is scoped to the frame and a POST's redirect
        lands back inside it.
    """
    body = response.body
    content_type = ""
    headers: dict[str, str] = {}
    for name, value in response.headers.items():
        lower = name.lower()
        if lower in _DROP_RESPONSE_HEADERS:
            continue
        if lower == "content-type":
            content_type = value
        if lower == "content-security-policy":
            value = value.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
        elif lower == "set-cookie":
            value = re.sub(r"(?i)\bPath=/(?=;|$)", f"Path={base}", value)
        elif lower == "location":
            value = _rewrite_location(value, base)
        headers[name] = value

    if "html" in content_type.lower():
        body = _ATTR_URL.sub(rb"\1=\2" + base.encode() + b"/", body)
        body = _SCRIPT_URL.sub(rb"\1" + base.encode() + rb"/\2", body)

    return Response(content=body, status_code=response.status, headers=headers)


def _rewrite_location(location: str, base: str) -> str:
    """Prefix a root-relative redirect with the console base; leave absolute
    URLs and protocol-relative ones (`//host`) alone."""
    if location.startswith("/") and not location.startswith("//"):
        return base + location
    return location
