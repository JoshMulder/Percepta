"""The host-shell relay: a station's host PTY, bridged to a browser terminal.

This is the second, larger reach of the "platform admin reaches a station"
feature, and the honest name for it is **host RCE via the platform**: a shell on
the box's host, not inside the sandboxed agent. It is off by default and gated
twice (a compose profile the box must opt into, and a station flag the agent
checks), time-boxed, and audited — because the blast radius demands it.

Same topology as media and the console proxy: nothing meets directly. A
privileged helper container on the station opens a PTY on the host
(`--pid=host` + `nsenter`) and connects a socket *outward* to `/host/ingest`; a
platform admin's browser terminal connects to `/host/view`; and this relay pairs
the two per station. The station never learns the admin's address, the admin
never learns the station's.

**Unlike the console proxy, this is not request/response — it is a raw byte
bridge.** A PTY is a bidirectional stream: keystrokes and resize control down,
terminal output up, both continuously and neither with a reply. So the relay
does not interpret frames at all; it forwards them verbatim between the two
sockets (binary for PTY bytes, text for the small JSON control frames like
resize). One session per station: a second browser redeeming a ticket supersedes
the first, because two terminals sharing one PTY is chaos, not collaboration.

**In-process, per worker**, for the media relay's reason: the two sockets must
meet in one process to be paired, and there is no Redis hop that pairs them (the
`WEB_CONCURRENCY` warning in `scripts/start_app.py` covers this too).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: How long a browser terminal waits for the station's helper to open its host
#: socket after the platform has asked it to (`host.open` on the command
#: channel). Longer than the console proxy's wait: this trip is broker command →
#: agent → handoff file → the privileged helper noticing and dialling back, so
#: there is one more hop than the agent-served console has.
CONNECT_WAIT_S = 20.0


@dataclass
class HostLink:
    """One station's open host socket, and the browser terminal on it, if any.

    `ingest` is the helper's socket — the relay sends keystrokes to it and
    receives PTY output from it. `viewer` is the current browser terminal, set
    when one redeems a ticket and cleared when it leaves; a PTY with nobody
    watching still runs (its output is dropped until a viewer attaches).
    """

    station_id: uuid.UUID
    organization_id: uuid.UUID
    #: The station helper's WebSocket (a Starlette WebSocket).
    ingest: object
    #: The browser terminal's WebSocket, or None between sessions.
    viewer: object | None = None


class HostRelay:
    """Per-process registry of open host sockets, pairing helper and browser."""

    def __init__(self) -> None:
        self._links: dict[uuid.UUID, HostLink] = {}
        self._waiters: dict[uuid.UUID, list["asyncio.Future[HostLink]"]] = {}

    def get(self, station_id: uuid.UUID) -> HostLink | None:
        return self._links.get(station_id)

    def register_ingest(
        self, station_id: uuid.UUID, organization_id: uuid.UUID, ingest: object
    ) -> HostLink:
        """The helper has opened its host socket. Supersede any older one."""
        existing = self._links.pop(station_id, None)
        if existing is not None:
            _close_soon(existing.ingest, "superseded by a newer host socket")
            _close_soon(existing.viewer, "superseded by a newer host session")
        link = HostLink(station_id=station_id, organization_id=organization_id,
                        ingest=ingest)
        self._links[station_id] = link
        for future in self._waiters.pop(station_id, []):
            if not future.done():
                future.set_result(link)
        return link

    def ingest_gone(self, station_id: uuid.UUID, link: HostLink) -> None:
        """The helper's socket closed. Take the browser terminal down with it —
        a terminal wired to a PTY that no longer exists is a frozen prompt that
        reads as a working shell."""
        if self._links.get(station_id) is link:
            self._links.pop(station_id, None)
        _close_soon(link.viewer, "the station host session ended")

    def attach_viewer(self, station_id: uuid.UUID, viewer: object) -> HostLink | None:
        """Bind a browser terminal to a station's host socket, superseding any
        terminal already on it. Returns None if the helper is not connected."""
        link = self._links.get(station_id)
        if link is None:
            return None
        if link.viewer is not None and link.viewer is not viewer:
            _close_soon(link.viewer, "another terminal took over this session")
        link.viewer = viewer
        return link

    def detach_viewer(self, station_id: uuid.UUID, viewer: object) -> None:
        link = self._links.get(station_id)
        if link is not None and link.viewer is viewer:
            link.viewer = None

    async def wait_for(
        self, station_id: uuid.UUID, *, timeout: float = CONNECT_WAIT_S
    ) -> HostLink | None:
        """Wait for the helper to open its socket, or None on timeout."""
        link = self._links.get(station_id)
        if link is not None:
            return link
        loop = asyncio.get_running_loop()
        future: asyncio.Future[HostLink] = loop.create_future()
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


def _close_soon(websocket: object | None, reason: str) -> None:
    """Best-effort close of a socket we are displacing. Scheduled rather than
    awaited: the caller is usually mid-registration and must not block on a
    teardown, and a socket already gone raises, which is fine to ignore."""
    if websocket is None:
        return

    async def _close() -> None:
        try:
            await websocket.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    try:
        asyncio.ensure_future(_close())
    except RuntimeError:  # pragma: no cover - no running loop
        pass
    log.debug("Host relay closing a displaced socket: %s", reason)


relay = HostRelay()
