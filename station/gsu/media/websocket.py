"""A WebSocket client, client side of RFC 6455, and nothing more than needed.

Written out rather than depended on, for the reason the whole station is:
`requirements.txt` is one line on purpose, and an unattended box in the field
should boot with what is in its image. It is about two hundred lines because the
station only ever does one thing here — open one outbound connection, send
frames, answer pings, close cleanly — and the parts it does not need (server
side, extensions, permessage-deflate, continuation of outgoing frames) are
absent rather than approximated.

Two properties matter more than completeness, and both are about what happens
when the link cannot carry the stream:

**A frame is written whole or not at all.** Before sending, the socket is asked
whether it is writable; when it is not, the caller is told and the frame is
dropped without a byte going out. A partial frame would leave the peer reading a
length that never arrives — a stream that hangs rather than one that drops, and
the hang is much harder to see.

**Backpressure is visible, not absorbed.** There is no queue in here. On a
metered satellite link a buffered second of 1080p is several megabytes of a
picture that is already out of date, and the right answer is to drop it and say
so — `gsu/stream.py` counts what that costs and reports it.

TLS is the station's own pinned trust (`gsu/tls.py`). There is no unverified
mode and no plaintext fallback, exactly as with the broker.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import select
import socket
import ssl
import struct
import threading

log = logging.getLogger("gsu.media")

#: RFC 6455's magic value, used to prove the server understood the handshake.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: How long to wait for the socket to become writable before calling it
#: congested and dropping the frame. A tenth of a frame interval at 30 fps: long
#: enough to ride out a scheduling hiccup, short enough that it cannot become a
#: queue.
WRITABLE_TIMEOUT_S = 0.25

#: How long a whole frame may take once we have started writing it. Reaching
#: this means the link stalled mid-frame, which is unrecoverable for the stream:
#: the connection is closed rather than left carrying half a message.
SEND_TIMEOUT_S = 5.0

CONNECT_TIMEOUT_S = 10.0
HANDSHAKE_TIMEOUT_S = 10.0

#: Refuse a server frame larger than this. The station is a sender; anything
#: this size arriving is a fault or an attack, and either way it is not a
#: command channel.
MAX_INCOMING_BYTES = 64 * 1024


class WebSocketError(RuntimeError):
    """The connection failed, with a sentence for a log rather than a trace."""


def _split_url(url: str) -> tuple[str, str, int, str]:
    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    if scheme not in ("ws", "wss"):
        raise WebSocketError(f"{url!r} is not a WebSocket URL")
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    if authority.startswith("["):                       # IPv6 literal
        host, _, tail = authority[1:].partition("]")
        port_text = tail.lstrip(":")
    else:
        host, _, port_text = authority.partition(":")
    port = int(port_text) if port_text.isdigit() else (443 if scheme == "wss" else 80)
    return scheme, host, port, ("/" + path if slash else "/")


def _mask(payload: bytes, key: bytes) -> bytes:
    """XOR the payload with the four-byte key, in one operation.

    A Python loop over a 100 kB frame is milliseconds of a 900 MHz core, thirty
    times a second. Doing it as one big integer keeps it in C.
    """
    if not payload:
        return payload
    length = len(payload)
    repeated = (key * (length // 4 + 1))[:length]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(
        length, "big"
    )


class WebSocket:
    """One outbound WebSocket connection, opened only while it is needed."""

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 trust=None, origin: str | None = None,
                 on_message=None, what: str = "the media uplink") -> None:
        #: What to call this connection in logs and errors. Two things use this
        #: client now — the media uplink and the broker relay — and a relay
        #: socket reporting itself as "the media uplink" sends whoever is
        #: reading the log to the wrong file.
        self.what = what
        #: Called with (opcode, payload) for each text or binary frame that
        #: arrives. None means "there is nothing to receive here", which is
        #: true of the media uplink and not of the broker relay — see the
        #: note where frames are dispatched.
        self.on_message = on_message
        self.url = url
        self.headers = dict(headers or {})
        self.trust = trust
        self.origin = origin
        self.scheme, self.host, self.port, self.path = _split_url(url)
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()
        self.close_reason = ""
        self.sent_frames = 0
        self.sent_bytes = 0

    # --- connecting ------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._socket is not None and not self._closed.is_set()

    def connect(self) -> None:
        """Open the connection, or raise with the reason."""
        if self.trust is not None:
            # Refuses a plaintext URL, or a TLS one with nothing to verify
            # against, in the same words as the broker path. There is no route
            # from here to an unverified connection.
            self.trust.check(self.url, self.what)
        self._closed.clear()
        raw = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
        try:
            if self.scheme == "wss":
                context = self.trust.context() if self.trust is not None else \
                    ssl.create_default_context()
                raw = context.wrap_socket(raw, server_hostname=self.host)
            raw.settimeout(HANDSHAKE_TIMEOUT_S)
            key = self._handshake(raw)
            del key
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            raise
        raw.settimeout(SEND_TIMEOUT_S)
        self._socket = raw
        self._reader = threading.Thread(target=self._read_forever, name="gsu-ws",
                                        daemon=True)
        self._reader.start()
        log.info("%s open to %s", self.what.capitalize(), self.url)

    def _handshake(self, raw: socket.socket) -> str:
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.origin:
            lines.append(f"Origin: {self.origin}")
        lines += [f"{name}: {value}" for name, value in self.headers.items()]
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = raw.recv(1024)
            if not chunk:
                raise WebSocketError("the server closed the connection during the handshake")
            response += chunk
            if len(response) > 16384:
                raise WebSocketError("the server sent an unreasonable handshake response")
        head = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        status = head.split("\r\n")[0]
        if "101" not in status:
            # The status line is the whole diagnosis: 401 is a rejected
            # credential, 404 a path that does not exist, 502 a proxy that does
            # not know about WebSockets.
            raise WebSocketError(f"the server refused the upgrade: {status.strip()}")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        got = ""
        for line in head.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = value.strip()
        if got != expected:
            # Not pedantry: this is what proves the peer is a WebSocket server
            # and not something that happened to return 101.
            raise WebSocketError("the server's handshake did not verify")
        return key

    # --- sending ---------------------------------------------------------

    def send_text(self, text: str) -> bool:
        return self._send(OP_TEXT, text.encode())

    def send_json(self, payload: dict) -> bool:
        return self.send_text(json.dumps(payload, separators=(",", ":")))

    def send_binary(self, data: bytes) -> bool:
        return self._send(OP_BINARY, data)

    def _send(self, opcode: int, payload: bytes) -> bool:
        """One whole frame, or nothing at all. False means congested."""
        with self._lock:
            sock = self._socket
            if sock is None or self._closed.is_set():
                return False
            frame = self._frame(opcode, payload)
            try:
                ready = select.select([], [sock], [], WRITABLE_TIMEOUT_S)[1]
            except (OSError, ValueError) as exc:
                self._fail(f"the media socket failed: {exc}")
                return False
            if not ready:
                # Congested. Nothing has been written, so the stream is intact
                # and the caller can simply drop this frame.
                return False
            try:
                sock.sendall(frame)
            except (TimeoutError, socket.timeout):
                # Part of a frame may already be out, so the peer's parser is
                # now waiting on bytes that will never come. The connection
                # cannot be reused.
                self._fail("the media uplink stalled mid-frame")
                return False
            except (OSError, ssl.SSLError) as exc:
                self._fail(f"the media uplink failed: {exc}")
                return False
            self.sent_frames += 1
            self.sent_bytes += len(frame)
            return True

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        header = bytearray()
        header.append(0x80 | opcode)                    # FIN, one frame
        length = len(payload)
        # The mask is mandatory for a client and must be unpredictable; a fixed
        # one is a well-known way to make a proxy cache something it should not.
        key = os.urandom(4)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += key
        return bytes(header) + _mask(payload, key)

    # --- receiving, which is mostly answering pings ----------------------

    def _read_forever(self) -> None:
        sock = self._socket
        buffer = bytearray()
        while not self._closed.is_set() and sock is not None:
            try:
                ready = select.select([sock], [], [], 1.0)[0]
                if not ready:
                    continue
                chunk = sock.recv(8192)
            except (TimeoutError, socket.timeout):
                continue
            except (OSError, ssl.SSLError) as exc:
                if not self._closed.is_set():
                    self._fail(f"{self.what} dropped: {exc}")
                return
            if not chunk:
                self._fail(f"the platform closed {self.what}")
                return
            buffer += chunk
            while True:
                frame = self._take_frame(buffer)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == OP_PING:
                    self._send(OP_PONG, payload)
                elif opcode == OP_CLOSE:
                    reason = payload[2:].decode("utf-8", "replace") if len(payload) > 2 else ""
                    self._fail(f"the platform closed {self.what}: {reason or 'no reason given'}")
                    return
                elif opcode in (OP_TEXT, OP_BINARY):
                    if self.on_message is not None:
                        # The broker relay receives here: commands come down
                        # the same socket the telemetry goes up.
                        try:
                            self.on_message(opcode, payload)
                        except Exception:  # noqa: BLE001 - a handler must not
                            # take the reader thread down with it.
                            log.exception("A websocket message handler failed.")
                        continue
                    # No handler: the media uplink, which does not take
                    # instructions. Commands arrive on the command channel,
                    # authenticated and ACL-pinned, and a second control path
                    # would be a second thing to secure.
                    log.info("Ignoring a message from the media endpoint (%d bytes).",
                             len(payload))

    def _take_frame(self, buffer: bytearray) -> tuple[int, bytes] | None:
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        masked = second & 0x80
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < 4:
                return None
            length = struct.unpack_from(">H", buffer, 2)[0]
            offset = 4
        elif length == 127:
            if len(buffer) < 10:
                return None
            length = struct.unpack_from(">Q", buffer, 2)[0]
            offset = 10
        if length > MAX_INCOMING_BYTES:
            self._fail("the platform sent an unreasonably large frame")
            return None
        key = b""
        if masked:
            if len(buffer) < offset + 4:
                return None
            key = bytes(buffer[offset:offset + 4])
            offset += 4
        if len(buffer) < offset + length:
            return None
        payload = bytes(buffer[offset:offset + length])
        del buffer[:offset + length]
        if masked:
            payload = _mask(payload, key)
        return first & 0x0F, payload

    # --- closing ---------------------------------------------------------

    def _fail(self, reason: str) -> None:
        if not self.close_reason:
            self.close_reason = reason
            log.warning("%s", reason)
        self._shutdown()

    def close(self, reason: str = "") -> None:
        """Close politely: a close frame, then the socket."""
        with self._lock:
            sock = self._socket
            if sock is not None and not self._closed.is_set():
                try:
                    sock.sendall(self._frame(OP_CLOSE, struct.pack(">H", 1000)))
                except (OSError, ssl.SSLError):
                    pass
        if reason and not self.close_reason:
            self.close_reason = reason
        self._shutdown()

    def _shutdown(self) -> None:
        self._closed.set()
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
