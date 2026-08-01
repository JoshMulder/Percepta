"""The broker, reached over the same 443 the console is on.

**This is the deployment transport.** `redis_transport.py` remains for a bench
where the broker is directly reachable, and is selected by URL scheme, but a
station at a real site talks to the platform here.

WHY THIS EXISTS
---------------
A station's broker is Redis on 6380. That works on a LAN and nowhere else: put
the platform behind a reverse proxy — which is what a public deployment is —
and 6380 is shut, while 443 is the one port that is open everywhere, at every
customer site, behind every corporate firewall, on Starlink. "Only 443 is open"
is not a quirk to work around once; it is the normal condition.

**It is a message relay, not a Redis proxy, and that is the security argument.**
Tunnelling RESP would give a station the ability to `SUBSCRIBE` to any channel
on the platform's broker, including other stations' telemetry and other
organisations' commands. What goes over this socket is `{topic, payload}`, the
platform derives the station's identity from the credential rather than from
the frame, and it refuses any topic that is not this station's own. A stolen
box can impersonate nothing.

The frames are the same JSON the Redis transport publishes, so nothing above
`Transport` changes and nothing on the platform's side of the relay changes
either: the endpoint republishes into the same Redis channels, and
`station_ingest` cannot tell the difference.

WHAT IT DOES NOT DO
-------------------
**It does not queue.** The rule is the same as everywhere else on this path and
it is in the ABC: telemetry is current state, not a ledger. A frame that cannot
be sent now is worth less than the one along in a second, and replaying stale
readings into a live console is worse than a gap. Frames that cannot go are
counted and dropped, and the count is in health telemetry so a station quietly
dropping everything does not look like a quiet site.

**It does not block the sensing loop.** `start` returns immediately and a
background thread connects; `publish` from the sensing thread either writes to
a live socket or returns False.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time

from . import Handler, Transport
from .. import tls
from ..media.websocket import WebSocket, WebSocketError

log = logging.getLogger("gsu.transport")

#: Reconnect backoff. Jittered, because a platform restart otherwise brings a
#: whole fleet back in the same second — the herd is the problem, not the wait.
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0

#: A frame bigger than this is not sent. Audio at 125 ms and a spectrum are the
#: largest things that legitimately go up here; a megabyte is a bug upstream,
#: and discovering it as a stalled socket rather than as a log line is worse.
MAX_FRAME_BYTES = 512 * 1024


def _is_loopback(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
    host = host.rsplit(":", 1)[0].strip("[]")
    return host in ("127.0.0.1", "::1", "localhost")


class RelayTransport(Transport):
    """`{topic, payload}` over one WebSocket, both directions."""

    def __init__(self, url: str, username: str | None = None,
                 password: str | None = None, trust=None) -> None:
        self.url = url
        self.username = username
        self.password = password
        self.trust = trust
        self._socket: WebSocket | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handlers: dict[str, Handler] = {}
        self._dropped = 0
        self._refusals: dict[str, str] = {}
        self._last_error: str | None = None
        #: A certificate this station will not accept, as distinct from a link
        #: that is down — `agent._update_link_state` raises a different health
        #: condition for each, because an operator does different things about
        #: weather and about a trust root. The Redis transport has always
        #: reported this; the relay did not, so the deployment transport was
        #: the one that could fail on TLS and say nothing.
        self.tls_failed = False
        #: Set once the platform has accepted the credential, so "connected"
        #: means "usable" rather than "a socket exists". A TCP connection to a
        #: proxy that then refuses the upgrade is not a working link.
        self._ready = threading.Event()

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    def set_credentials(self, username: str, password: str) -> None:
        with self._lock:
            self.username, self.password = username, password
        # Not an immediate reconnect: the old credential keeps working through
        # the overlap window, so the next reconnect picks this up and nothing
        # is interrupted to achieve it.

    # --- sending ---------------------------------------------------------

    def publish(self, topic: str, payload: dict) -> bool:
        socket = self._socket
        if socket is None or not socket.connected or not self._ready.is_set():
            self._dropped += 1
            return False
        try:
            frame = json.dumps({"topic": topic, "payload": payload})
        except (TypeError, ValueError) as exc:
            # Not a link fault, and not something a retry fixes.
            log.warning("Dropping unserialisable payload for %s: %s", topic, exc)
            self._dropped += 1
            return False
        if len(frame) > MAX_FRAME_BYTES:
            log.warning("Dropping %d byte frame for %s; the cap is %d.",
                        len(frame), topic, MAX_FRAME_BYTES)
            self._dropped += 1
            return False
        if not socket.send_text(frame):
            self._dropped += 1
            return False
        return True

    def subscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            self._handlers[topic] = handler
        # Nothing is sent to ask for it. The platform knows which station this
        # is from the credential and sends that station's commands; a
        # subscribe request would be the station naming a channel, which is
        # exactly what this design refuses to accept from a station.

    # --- receiving -------------------------------------------------------

    def _on_message(self, _opcode: int, data: bytes) -> None:
        try:
            message = json.loads(data.decode("utf-8", "replace"))
        except (TypeError, ValueError):
            log.warning("Dropping a malformed frame from the relay.")
            return
        if not isinstance(message, dict):
            return
        # A refusal is the platform telling this station a topic is not its
        # own. Reported rather than retried: an ACL fault and an unreachable
        # broker are completely different problems and used to look identical.
        if message.get("type") == "refused":
            topic = str(message.get("topic", ""))
            reason = str(message.get("reason", "refused"))
            self._refusals[topic] = reason
            log.warning("The platform refused %s: %s", topic, reason)
            return
        topic = message.get("topic")
        payload = message.get("payload")
        if not isinstance(topic, str) or not isinstance(payload, dict):
            return
        with self._lock:
            handler = self._handlers.get(topic)
        if handler is None:
            log.info("No handler for %s; ignoring.", topic)
            return
        try:
            handler(topic, payload)
        except Exception:  # noqa: BLE001 - one bad command must not end the link
            log.exception("A command handler failed for %s.", topic)

    # --- the connection ---------------------------------------------------

    def _run(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._stop.is_set():
            if self._connect():
                backoff = BACKOFF_MIN_S
                # Connected. Wait here until the reader thread notices the
                # socket has gone; there is nothing to poll while it is up.
                while not self._stop.is_set():
                    socket = self._socket
                    if socket is None or not socket.connected:
                        break
                    self._stop.wait(1.0)
                self._ready.clear()
                if not self._stop.is_set():
                    log.info("Relay closed; reconnecting.")
            wait = min(backoff, BACKOFF_MAX_S) * (0.5 + random.random())
            if self._stop.wait(wait):
                return
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _connect(self) -> bool:
        with self._lock:
            secret = self.password
        if not secret:
            self._last_error = "no credential yet"
            return False
        # Plaintext goes no further than this box. Everywhere else in the
        # station refuses to fall back to an unverified connection, and a relay
        # carrying a bearer credential is the last place to make an exception —
        # `ws://` to anything but loopback puts the credential on the wire.
        #
        # Loopback is allowed because that is a test harness talking to itself,
        # and there is no network for anyone to be on.
        if self.url.lower().startswith("ws://") and not _is_loopback(self.url):
            self._last_error = (
                f"{self.url} is plaintext. The relay carries the station's "
                "credential and will not send it unencrypted; use wss://."
            )
            log.error("%s", self._last_error)
            self._stop.set()
            return False
        try:
            socket = WebSocket(
                self.url,
                # The same bearer the media uplink presents, and the same rule
                # behind it: the station proves who it is and the platform
                # decides what that means. The credential is a header rather
                # than a query parameter because a URL is logged by every proxy
                # between here and there.
                headers={"Authorization": f"Bearer {secret}"},
                trust=self.trust,
                on_message=self._on_message,
                what="the broker relay",
            )
            socket.connect()
        except tls.Refusal as exc:
            # A refusal is a decision, not a link fault, and it is permanent
            # until someone changes something. Retrying it forever would bury
            # the reason under reconnect noise — but *dropping* it is worse and
            # is what used to happen: `Refusal` is a RuntimeError, so it fell
            # straight through the clause below, killed this thread, and left
            # `last_error` None. The console then showed a station with no
            # broker and nothing at all to say about why.
            self._last_error = str(exc)
            self.tls_failed = True
            log.error("%s", exc)
            self._stop.set()
            return False
        except (WebSocketError, OSError) as exc:
            self._last_error = str(exc)
            self.tls_failed = tls.looks_like_tls_failure(str(exc))
            log.warning("Relay could not connect: %s", exc)
            return False
        self._socket = socket
        self._ready.set()
        self._last_error = None
        log.info("Relay open to %s.", self.url)
        return True

    def _close(self) -> None:
        socket, self._socket = self._socket, None
        self._ready.clear()
        if socket is not None:
            socket.close("station shutting down")

    # --- what health telemetry reports ------------------------------------

    @property
    def connected(self) -> bool:
        socket = self._socket
        return bool(socket is not None and socket.connected
                    and self._ready.is_set())

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def refusals(self) -> dict[str, str]:
        return dict(self._refusals)

    @property
    def secure(self) -> bool:
        return self.url.lower().startswith("wss://")

    @property
    def last_error(self) -> str | None:
        return self._last_error
