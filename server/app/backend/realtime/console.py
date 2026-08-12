"""The console relay: a station's own setup page, re-originated to an admin.

The shape is the media relay's (`realtime/media.py`), and deliberately so — a
field station is behind Starlink CGNAT with no inbound path, so the *only* way
to reach the box's own `127.0.0.1:8088` setup console is back down the socket the
station itself opened outward:

    admin browser ──► platform ──(the station's outbound WS)──► station ──► :8088

**It is not the broker.** The broker (`api/broker.py`) is fire-and-forget JSON
with a single downward `c` stream and a 512 KiB cap — the wrong shape for an
interactive HTTP surface where a request has an answer and a `POST /device`
carries a form. So this is request/response multiplexed over one persistent
WebSocket, exactly the trade the media path makes for bulk: terminate at the
platform, re-originate, and never let the browser learn the station's address or
the station learn the browser's.

**Request/response, not a byte pipe.** Unlike media — which forwards fMP4
fragments without reading them — a console request has a reply that has to come
back to *this* waiter and no other, so every request carries an id and the
station echoes it on the response. A `dict[id -> Future]` per link is the whole
of the multiplexing. Bodies are small (an HTML page, a `status.json`, a form, a
cached JPEG), so they ride inside the JSON frame base64-encoded rather than as
separate binary frames the way media fragments do — one stream, one framing, no
interleaving to get wrong.

**In-process, per worker, for the same reason media is.** The station's ingest
socket and the admin's HTTP request must meet in one process to share a Future,
and there is no Redis hop that helps: a request pinned to the worker holding the
admin's connection has to reach the worker holding the station's, and the honest
way to guarantee that is to run one worker (the `WEB_CONCURRENCY` warning in
`scripts/start_app.py` already says video needs this; the console rides the same
constraint). A second worker does not corrupt anything — it fails to find the
link and answers 504, which reads as "the station did not open its console".
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

#: How long a browser request waits for its response frame from the station
#: before giving up. A setup page renders local state off the box's own disk, so
#: it is fast — but a cold camera preview or a factory reset is not instant, and
#: this has to outlast the slowest honest reply without letting a wedged station
#: hold an admin's browser connection open indefinitely.
REQUEST_TIMEOUT_S = 30.0

#: How long a browser request waits for the station to *open* its console socket
#: after the platform has asked it to (`console.open` on the command channel).
#: The station has to receive the command, decide it has opted in, and dial back
#: — a couple of round trips over Starlink. Short enough that an opted-out or
#: offline station answers "no console" promptly rather than hanging the tab.
CONNECT_WAIT_S = 12.0

#: A frame larger than this is refused. The console proxy carries pages, JSON and
#: small cached images — not the live video or audio streams, which have their
#: own path (`/media`) and would never fit a request/response tunnel anyway. So
#: this is generous for the traffic that belongs here and a firm ceiling on the
#: traffic that does not: a station asked for `/stream.mp4` caps its own read and
#: returns an error frame rather than trying to pour an endless body through this.
MAX_FRAME_BYTES = 8 * 1024 * 1024


@dataclass
class ConsoleResponse:
    """One HTTP reply, re-originated from the station's loopback console."""

    status: int
    headers: dict[str, str]
    body: bytes


class ConsoleError(RuntimeError):
    """The station could not answer this request — not that it answered 4xx.

    A 404 from the box's own console is a `ConsoleResponse`, not this: this is
    the tunnel failing (the socket went, the station errored, or nothing came
    back in time), which is a 502/504 to the browser rather than a page.
    """


@dataclass
class ConsoleLink:
    """One station's open console socket, and the requests in flight on it.

    Requests are multiplexed by an integer id the station echoes back. The
    counter is per link and monotonic — a link is a single socket to a single
    station, so ids never need to be unique across stations, and starting fresh
    each connection means a late response frame from a dropped socket cannot
    resolve a live request on its replacement.
    """

    station_id: uuid.UUID
    organization_id: uuid.UUID
    #: Sends one text frame to the station. Guarded by `_send_lock`, because the
    #: ingest endpoint reads on its own task while browser requests send on
    #: theirs, and two concurrent sends on one WebSocket interleave frames.
    send: Callable[[str], Awaitable[None]]
    _ids: "itertools.count[int]" = field(default_factory=lambda: itertools.count(1))
    _pending: dict[int, "asyncio.Future[ConsoleResponse]"] = field(default_factory=dict)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        *,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> ConsoleResponse:
        """Send one request down the socket and wait for its reply frame."""
        request_id = next(self._ids)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ConsoleResponse] = loop.create_future()
        self._pending[request_id] = future
        frame = _encode(
            {
                "t": "req",
                "id": request_id,
                "method": method,
                "path": path,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode("ascii"),
            }
        )
        try:
            async with self._send_lock:
                await self.send(frame)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ConsoleError("the station did not answer in time") from exc
        except (RuntimeError, OSError) as exc:
            # The send itself failed: the socket is gone underneath us.
            raise ConsoleError(f"the station console socket failed: {exc}") from exc
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: int, response: ConsoleResponse) -> None:
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(response)

    def fail(self, request_id: int, error: str) -> None:
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_exception(ConsoleError(error))

    def fail_all(self, error: str) -> None:
        """Break every request still waiting when the socket goes away, so an
        admin gets a 502 now rather than the 30-second timeout on each."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConsoleError(error))
        self._pending.clear()


class ConsoleRelay:
    """Per-process registry of open station console sockets.

    In-process and not shared through Redis, exactly like the media relay and
    for the same reason: a request and the socket that answers it must be in one
    process to hand a Future between them.
    """

    def __init__(self) -> None:
        self._links: dict[uuid.UUID, ConsoleLink] = {}
        #: Browser requests waiting for a station to open its socket, so the
        #: request that triggered `console.open` proceeds the instant the station
        #: dials back rather than polling for it.
        self._waiters: dict[uuid.UUID, list["asyncio.Future[ConsoleLink]"]] = {}

    def get(self, station_id: uuid.UUID) -> ConsoleLink | None:
        return self._links.get(station_id)

    def station_connected(
        self,
        station_id: uuid.UUID,
        organization_id: uuid.UUID,
        send: Callable[[str], Awaitable[None]],
    ) -> ConsoleLink:
        """A station has opened its console socket. Supersede any older one.

        A station reconnecting (its idle window lapsed, then an admin came back)
        must not leave a dead link in the map that the next request finds and
        sends into. So a new socket replaces the old, and the old one's waiters
        are failed — the same supersede posture the broker takes.
        """
        existing = self._links.pop(station_id, None)
        if existing is not None:
            existing.fail_all("the station opened a newer console socket")
        link = ConsoleLink(
            station_id=station_id, organization_id=organization_id, send=send
        )
        self._links[station_id] = link
        for future in self._waiters.pop(station_id, []):
            if not future.done():
                future.set_result(link)
        return link

    def station_gone(self, station_id: uuid.UUID, link: ConsoleLink) -> None:
        """The station's socket closed. Only forget it if it is still the live
        one — a superseded link calling this must not evict its replacement."""
        link.fail_all("the station console socket closed")
        if self._links.get(station_id) is link:
            self._links.pop(station_id, None)

    async def wait_for(
        self, station_id: uuid.UUID, *, timeout: float = CONNECT_WAIT_S
    ) -> ConsoleLink | None:
        """Wait for the station to open its socket, or return None on timeout.

        Called after the platform has published `console.open`: the station is
        being asked to dial back, and this is what the browser request awaits.
        """
        link = self._links.get(station_id)
        if link is not None:
            return link
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ConsoleLink] = loop.create_future()
        self._waiters.setdefault(station_id, []).append(future)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            holders = self._waiters.get(station_id)
            if holders is not None:
                if future in holders:
                    holders.remove(future)
                if not holders:
                    self._waiters.pop(station_id, None)


def _encode(message: dict) -> str:
    import json

    return json.dumps(message, separators=(",", ":"))


relay = ConsoleRelay()
