"""The platform, reached over the same 443 the console is on.

**This is the transport.** Contract 2.0 defines one, and this is it: a single
authenticated WebSocket to `/broker` carrying frames of two keys.

WHY IT IS SHAPED LIKE THIS
--------------------------
443 is the one port open at every site, on every corporate network, behind
every reverse proxy, over Starlink. "Only 443 is open" is not a quirk to work
around once; it is the normal condition.

**It is a message relay, not a broker proxy, and that is the security
argument.** Tunnelling RESP would give a station the ability to subscribe to
any channel on the platform, including other stations' telemetry and other
organisations' commands. What goes over this socket is `{stream, payload}` —
a one-letter code and an object. The platform derives the station's identity
from the credential, and there is no field in which a station could name a
channel, a tenant or itself even if it wanted to. A stolen box can impersonate
nothing, structurally rather than by a check that might be forgotten.

WHAT IT DOES NOT DO
-------------------
**It does not queue.** Telemetry is current state, not a ledger. A frame that
cannot be sent now is worth less than the one along in a second, and replaying
stale readings into a live console is worse than a gap. Frames that cannot go
are counted and dropped, and the count is in health telemetry so a station
quietly dropping everything does not look like a quiet site.

The events stream is the exception to that and it is handled a layer up, in
`gsu/events.py`, because at-least-once delivery is built out of a durable store
and acknowledgements rather than out of anything a socket can offer.

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

from . import PUBLISHABLE, Handler, Transport
from .. import tls
from ..media.websocket import WebSocket, WebSocketError

log = logging.getLogger("gsu.transport")

#: Reconnect backoff. Jittered, because a platform restart otherwise brings a
#: whole fleet back in the same second — the herd is the problem, not the wait.
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 300.0

#: A connection must stay up at least this long to count as healthy — only then
#: does a reconnect reset the backoff. A platform that accepts the socket and
#: closes it again at once (its own Redis down, say) would otherwise reset the
#: backoff on every attempt, turning the fault into a hot ~1s reconnect loop
#: that hammers a platform already in trouble. A shorter-lived open is a soft
#: failure and lets the backoff keep climbing to the cap, exactly like an
#: outright failed connect.
HEALTHY_CONNECTION_S = 10.0

#: A frame bigger than this is not sent. The platform enforces the same cap by
#: closing the socket (1009), and discovering it that way costs a reconnect and
#: takes telemetry and commands down with it, so it is cheaper to notice here.
MAX_FRAME_BYTES = 512 * 1024

#: Socket liveness, from the timings table in `contract/transport.md`. Ping
#: after this long with nothing sent or received; treat no pong within
#: PONG_TIMEOUT_S as a dead socket and reconnect.
#:
#: This is not the heartbeat the contract's liveness rule rules out. That one
#: is about whether a *station* is alive and is still derived from publishing.
#: This is about whether the *socket* is, and nothing else can tell you: a
#: dropped NAT mapping on CGNAT looks exactly like a quiet minute, because
#: commands are unrequested and an hour without one is ordinary.
PING_IDLE_S = 20.0
PONG_TIMEOUT_S = 10.0


def _is_loopback(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
    host = host.rsplit(":", 1)[0].strip("[]")
    return host in ("127.0.0.1", "::1", "localhost")


class RelayTransport(Transport):
    """`{stream, payload}` over one WebSocket, both directions."""

    def __init__(self, url: str, secret: str | None = None, trust=None) -> None:
        self.url = url
        self.secret = secret
        self.trust = trust
        self._socket: WebSocket | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handler: Handler | None = None
        self._dropped = 0
        self._refusals: dict[str, str] = {}
        self._last_error: str | None = None
        self._last_activity = time.monotonic()
        self._ping_sent_at: float | None = None
        #: A certificate this station will not accept, as distinct from a link
        #: that is down — the agent raises a different health condition for
        #: each, because an operator does different things about weather and
        #: about a trust root.
        self.tls_failed = False
        #: Set once the platform has accepted the credential, so "connected"
        #: means "usable" rather than "a socket exists". A TCP connection to a
        #: proxy that then refuses the upgrade is not a working link.
        self._ready = threading.Event()
        #: Set when the platform closes with 4401. The agent renews once and
        #: reconnects rather than giving up, because 4401 covers both "revoked,
        #: and will never work again" and "expired while you were offline, and
        #: renewal will fix it" — and a station that stopped on the first would
        #: need a site visit after a transient.
        self.credential_refused = threading.Event()

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

    def set_credential(self, secret: str) -> None:
        with self._lock:
            self.secret = secret
        # Not an immediate reconnect: the old credential keeps working through
        # the 24 h overlap, so the next reconnect picks this up and nothing is
        # interrupted to achieve it.

    # --- sending ---------------------------------------------------------

    def publish(self, stream: str, payload: dict) -> bool:
        if stream not in PUBLISHABLE:
            # Caught here rather than earned as a `refused` frame. A station
            # publishing on `c` is a bug in this station, and the platform
            # telling us so a round trip later is a worse way to learn it.
            log.error("Refusing to publish on stream %r; a station may send "
                      "only %s.", stream, ", ".join(sorted(PUBLISHABLE)))
            self._dropped += 1
            return False
        socket = self._socket
        if socket is None or not socket.connected or not self._ready.is_set():
            self._dropped += 1
            return False
        try:
            frame = json.dumps({"stream": stream, "payload": payload},
                               separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            # `allow_nan=False` is what makes this fire on NaN and Infinity.
            # They are not JSON, and NaN in particular passes every numeric
            # bound in the schemas because comparisons against it are false —
            # so a station emitting one produces a frame that validates and
            # means nothing. Not a link fault, and not something a retry fixes.
            log.warning("Dropping an unserialisable %s payload: %s", stream, exc)
            self._dropped += 1
            return False
        if len(frame) > MAX_FRAME_BYTES:
            log.warning("Dropping a %d byte %s frame; the cap is %d.",
                        len(frame), stream, MAX_FRAME_BYTES)
            self._dropped += 1
            return False
        if not socket.send_text(frame):
            self._dropped += 1
            return False
        self._last_activity = time.monotonic()
        return True

    def on_command(self, handler: Handler) -> None:
        with self._lock:
            self._handler = handler
        # Nothing is sent to ask for it. The platform knows which station this
        # is from the credential and sends that station's commands from the
        # moment the socket opens; a subscribe request would be the station
        # naming a channel, which is what this design refuses to accept.

    # --- receiving -------------------------------------------------------

    def _on_message(self, _opcode: int, data: bytes) -> None:
        self._last_activity = time.monotonic()
        try:
            message = json.loads(data.decode("utf-8", "replace"))
        except (TypeError, ValueError):
            log.warning("Dropping a malformed frame from the platform.")
            return
        if not isinstance(message, dict):
            return

        # `type` before `stream`, and the order is load-bearing. Both downward
        # frames carry `stream` and only the command carries `payload`, so
        # dispatching on `stream` first raises KeyError on a refusal — and the
        # refusal a station is most likely to provoke is for publishing on `c`,
        # which arrives as `stream: "c"` and looks exactly like a command. The
        # frame written to explain a misconfiguration would crash the station
        # that made it.
        if message.get("type") == "refused":
            stream = str(message.get("stream", "?"))
            reason = str(message.get("reason", "refused"))
            if self._refusals.get(stream) != reason:
                log.warning("The platform refused stream %s: %s", stream, reason)
            self._refusals[stream] = reason
            return

        if message.get("type") == "unauthorized":
            # The credential is refused, told to us in-band. This is the same
            # signal as a 4401 close (see the connect handler below), sent as a
            # frame because a 4401 *close code* does not survive every proxy —
            # Cloudflare strips it, and without this the station never learns the
            # close was an auth refusal and hot-loops reconnecting instead of
            # renewing. Recovery is identical: set the flag and the agent renews
            # once and comes back. See broker.py `_refuse`.
            reason = str(message.get("reason", "unauthorized"))
            log.warning(
                "The platform refused this station's credential: %s", reason)
            self.credential_refused.set()
            return

        if message.get("stream") != "c":
            return
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        with self._lock:
            handler = self._handler
        if handler is None:
            log.info("A command arrived before anything was listening.")
            return
        try:
            handler(payload)
        except Exception:  # noqa: BLE001 - one bad command must not end the link
            log.exception("A command handler failed.")

    # --- the connection ---------------------------------------------------

    def _run(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._stop.is_set():
            if self._connect():
                opened_at = time.monotonic()
                self._hold()
                self._ready.clear()
                # Only a connection that STAYED up resets the backoff. One that
                # the platform accepts and drops again at once (see
                # HEALTHY_CONNECTION_S) is a soft failure: leave the backoff
                # climbing rather than resetting it into a hot reconnect loop.
                if time.monotonic() - opened_at >= HEALTHY_CONNECTION_S:
                    backoff = BACKOFF_MIN_S
                if not self._stop.is_set():
                    log.info("Relay closed; reconnecting.")
            wait = min(backoff, BACKOFF_MAX_S) * (0.5 + random.random())
            if self._stop.wait(wait):
                return
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _hold(self) -> None:
        """Stay here while the socket is up, pinging when it goes quiet."""
        self._ping_sent_at = None
        while not self._stop.is_set():
            socket = self._socket
            if socket is None or not socket.connected:
                return
            now = time.monotonic()
            if self._ping_sent_at is not None:
                if socket.last_pong >= self._ping_sent_at:
                    self._ping_sent_at = None            # answered; still there
                elif now - self._ping_sent_at > PONG_TIMEOUT_S:
                    # Nothing came back. On a healthy link this is impossible:
                    # the platform answers a ping, and the only thing that
                    # swallows one silently is a socket that no longer reaches
                    # anybody. Reconnecting is the only move that recovers a
                    # dropped NAT mapping, and without this the station would
                    # publish into the hole until the OS noticed, which is
                    # unbounded.
                    log.warning("No pong in %.0fs; the socket is half-open. "
                                "Reconnecting.", PONG_TIMEOUT_S)
                    self._last_error = "the platform stopped answering pings"
                    self._close()
                    return
            elif now - self._last_activity >= PING_IDLE_S:
                self._ping_sent_at = now
                socket.send_ping()
            self._stop.wait(1.0)

    def _connect(self) -> bool:
        with self._lock:
            secret = self.secret
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
                # A header rather than a query parameter because a URL is
                # logged by every proxy between here and there.
                headers={"Authorization": f"Bearer {secret}"},
                trust=self.trust,
                on_message=self._on_message,
                what="the platform relay",
            )
            socket.connect()
        except tls.Refusal as exc:
            # A refusal is a decision, not a link fault, and it is permanent
            # until someone changes something. Retrying forever would bury the
            # reason under reconnect noise — but dropping it is worse, and is
            # what used to happen: `Refusal` is a RuntimeError, so it fell
            # through the clause below, killed this thread, and left
            # `last_error` None. The console then showed a station with no link
            # and nothing at all to say about why.
            self._last_error = str(exc)
            self.tls_failed = True
            log.error("%s", exc)
            self._stop.set()
            return False
        except (WebSocketError, OSError) as exc:
            self._last_error = str(exc)
            self.tls_failed = tls.looks_like_tls_failure(str(exc))
            if "4401" in str(exc):
                # The platform completed the handshake and then closed 4401:
                # the credential was refused. Flagged rather than retried
                # blindly, because the agent's answer is to renew once and come
                # back — which is the whole recovery path for a box whose
                # credential expired while it was offline.
                self.credential_refused.set()
            log.warning("Relay could not connect: %s", exc)
            return False
        self._socket = socket
        self._ready.set()
        self._last_activity = time.monotonic()
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
