#!/usr/bin/env python3
"""Check a running station against the contract.

    python contract/conformance/check_station.py
    python contract/conformance/check_station.py --port 8099

**This stands in for the platform.** It listens on the relay endpoint, the
station connects to it, and everything the station sends is validated against
the schemas in `../schemas/`. Then it issues each command and checks the
station reports the effect back — because a command that is accepted and
quietly ignored is the failure this platform is least able to notice.

Point a station at it and run it:

    GSU_PLATFORM_URL=... GSU_BROKER_URL=ws://127.0.0.1:8099/broker python -m gsu run
    python contract/conformance/check_station.py

Neutral about implementation: it speaks the wire format in `transport.md` and
knows nothing about anyone's code. It needs no broker, no database and no
station id — under contract 2.0 a station never puts its id on the wire, so
there is nothing to tell this harness and nothing for it to get wrong.

Dependencies: `jsonschema`. The WebSocket server is written out below rather
than imported, for the same reason the station writes its client out — this
file should run anywhere Python does.

**Plaintext by default, and that is a test.** A station configured to require
TLS *should* refuse to connect here, and refusing is correct behaviour rather
than a failure. Use --tls to check the other path.
"""

import argparse
import base64
import hashlib
import json
import math
import os
import socket
import ssl
import struct
import sys
import time
import traceback
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("pip install jsonschema")

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"

#: Every telemetry kind a station is expected to produce, and the default
#: seconds between frames from transport.md's cadence table. Only the defaults:
#: a station reporting `health.cadence` is believed instead, because the
#: contract says a site may legitimately slow a stream down to save bandwidth
#: and must not be failed for it.
DEFAULT_CADENCE = {"adsb": 1.0, "power": 1.0, "radio": 1.0, "light": 1.0,
                   "weather": 5.0}

#: How many periods to wait before calling a stream absent, and the floor for a
#: fast one. Generous: a station on a slow tick is not a broken station.
PERIODS = 4
MIN_WAIT = 8.0

#: Longest this will listen for the opening survey, whatever the cadences say.
#: Raise it with --listen-for for a station on a deliberately slow cadence.
MAX_WAIT = 45.0

#: transport.md's default health cadence. The opening window stretches to at
#: least this, because health carries the cadences everything else is judged
#: against and a shorter window discovers them only by luck.
HEALTH_PERIOD = 30.0

#: Kinds the contract defines but does not require in a short window. `health`
#: because a station is not less conformant for staying quiet about itself;
#: `events` because a station with nothing to report has nothing to send.
OPTIONAL_KINDS = {"health", "events"}

#: How long a command has to be reflected in telemetry.
COMMAND_TIMEOUT = 8.0

#: What this harness checks against.
CONTRACT_VERSION = "2.0"

#: Relay stream codes, per transport.md. A station may publish the first three
#: and only receive on the fourth.
STREAM_KIND = {"t": "telemetry", "a": "audio", "e": "events"}
COMMAND_STREAM = "c"

#: Each command, and the telemetry field that must reflect it. This pairing is
#: the contract's core promise: nothing is confirmed by the platform, so every
#: command has to be observable in what the station reports.
#:
#: Each pair that changes state is followed by one that puts it back, because
#: this runs against real commissioned hardware. `radio.gain` and `radio.ppm`
#: are deliberately absent: they are calibration settings trimmed once for a
#: site, and a harness that left one changed would desense a receiver in a way
#: nobody would connect to having run a test.
COMMANDS = [
    ("radio.tune", {"freq_hz": 119_500_000}, "radio", "freq_hz", 119_500_000),
    ("radio.auto_squelch", {"on": False}, "radio", "auto_squelch", False),
    ("radio.squelch", {"db": -55.0}, "radio", "threshold_db", -55.0),
    ("radio.monitor", {"on": True}, "radio", "monitor", True),
    ("radio.monitor", {"on": False}, "radio", "monitor", False),
    ("radio.auto_squelch", {"on": True}, "radio", "auto_squelch", True),
    ("radio.spectrum", {"on": True, "lease_seconds": 15}, "radio", "span_hz", None),
    ("light.set", {"on": True}, "light", "on", True),
    ("light.set", {"on": False}, "light", "on", False),
]

failures: list[str] = []
notes: list[str] = []


#: Checks that could not be run, as distinct from checks that failed. Kept
#: apart because "this station is wrong" and "this run could not tell" are
#: different answers, and reporting the second as the first is how correct
#: hardware gets rejected on a hillside.
skipped: list[str] = []

#: Required telemetry streams this run never observed because the station
#: declared a cadence longer than the listening window. Tracked separately
#: from other skips because they decide the verdict: a stream nobody watched
#: is not a stream that passed, and `health.cadence` is a number the station
#: supplies — believing it without bound would let a station opt out of its
#: own telemetry checks and still collect a certificate.
skipped_streams: list[str] = []

#: Why the socket ended, if it ended before the run did. This decides the
#: verdict for the same reason `skipped_streams` does: once the station is
#: gone, every remaining check passes by default because a dead socket
#: delivers nothing to fail on. A station that answered all nine commands and
#: then vanished was being told "audio stops when the lease lapses — PASS,
#: this station satisfies the contract", on the evidence of a closed socket,
#: in the same report that said it had been left mistuned.
disconnected: list[str] = []

#: Rules this run could not put to the test, as opposed to rules it watched
#: pass. Today that is the two audio rules, and they are the ones that matter:
#: the squelch gate and the lease are the most expensive things in the
#: contract, and the lease is what the reference implementation once shipped
#: wrong.
#:
#: They cannot simply be failed. Airband is silent most of the time and a quiet
#: channel is the normal case, so a station that sent no audio has done nothing
#: wrong — which is exactly why this needs its own verdict rather than a pass
#: or a failure. A run that never heard a transmission proves nothing about
#: gating, and saying "this station satisfies the contract" on that evidence is
#: a certificate nobody earned. Found by running the real agent against this
#: harness: three runs in a row reported no audio and exited 0, against a
#: station that was publishing it perfectly well on a longer window.
unexercised: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'' if ok else f'  ({detail})'}")
    if not ok:
        failures.append(label)


def skip(label: str, why: str) -> None:
    print(f"  SKIP  {label}  ({why})")
    skipped.append(f"{label}: {why}")


def load(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    # JSON Schema ignores keywords it does not recognise, by design. So a
    # misspelt `maxLength` is not an error — it is a bound that silently stops
    # existing, in the one tool that decides whether a station is conformant.
    # This is the cheapest possible guard against the schemas rotting under us.
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# --- the relay, written out ------------------------------------------------
#
# Enough of RFC 6455 to be the far end of one station's socket: the handshake,
# text frames, close and ping. No extensions are negotiated, so nothing here
# has to deal with compression — which also keeps the bandwidth figures in
# transport.md honest, since permessage-deflate would change them.

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Refused(Exception):
    """The station did not connect in a way this harness can accept."""


class NonFinite(ValueError):
    """A frame carried NaN or Infinity, which are not JSON (RFC 8259)."""


def _reject_constant(token: str):
    raise NonFinite(
        f"frame carries the non-finite token {token}; NaN and Infinity are "
        "not JSON, and NaN passes every numeric bound in the schemas"
    )


class Relay:
    """One station's socket, from the platform's side."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.credential: str | None = None
        self.path = ""
        self.closed = False
        #: Anything the station sent that the wire format does not allow.
        self.envelope_faults: list[str] = []
        #: Fragments of a message still being assembled. On the instance, not
        #: in `read_frame`, so that a control frame or a window ending
        #: mid-message does not throw away what has arrived so far.
        self._parts: list[bytes] = []
        #: Pongs seen. transport.md requires both ends to answer a ping, which
        #: is the only way either of them detects a half-open socket — a
        #: station on CGNAT whose NAT mapping is dropped otherwise publishes
        #: into a hole indefinitely, because nothing arrives downward on a
        #: healthy link either.
        self.pongs = 0

    # -- framing ----------------------------------------------------------

    def _fill(self, deadline: float) -> None:
        """One read into the buffer. Never consumes.

        Every way this socket can die leaves as `ConnectionError`. It used to
        leave as a bare `OSError` — `settimeout` on a socket this harness had
        already closed itself raises WinError 10038, which is neither
        `ConnectionError` nor `socket.timeout` — and the read loops catch only
        those two. One oversized frame therefore closed the socket at 1009 and
        then tracebacked out of the run on the next read, skipping the block
        that puts the receiver back and leaving a commissioned site mistuned
        with no warning printed. The warning lives in the block that got
        skipped, so the one path where it could not run is the one where it
        mattered.
        """
        if self.closed:
            raise ConnectionError("socket already closed")
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError
        try:
            self.sock.settimeout(min(remaining, 1.0))
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return
        except OSError as exc:
            self.closed = True
            raise ConnectionError(f"socket failed: {exc}") from exc
        if not chunk:
            self.closed = True
            raise ConnectionError("station closed the socket")
        self.buf += chunk

    def _recv(self, want: int, deadline: float) -> bytes:
        while len(self.buf) < want:
            self._fill(deadline)
        out, self.buf = self.buf[:want], self.buf[want:]
        return out

    def handshake(self, deadline: float) -> None:
        while b"\r\n\r\n" not in self.buf:
            self._fill(deadline)
            if len(self.buf) > 65536:
                raise Refused("request head too large")
        head, _, rest = self.buf.partition(b"\r\n\r\n")
        self.buf = rest
        lines = head.decode("latin-1").split("\r\n")
        self.path = lines[0].split(" ")[1] if " " in lines[0] else ""
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            self.credential = auth[7:].strip()

        key = headers.get("sec-websocket-key")
        if not key:
            self.sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            raise Refused("not a WebSocket upgrade")
        accept = base64.b64encode(
            hashlib.sha1((key + _GUID).encode()).digest()
        ).decode()
        self.sock.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )

    def read_frame(self, deadline: float) -> str | None:
        """One text payload, or None on a control frame or a timeout.

        Reassembles fragments. RFC 6455 permits any message to be split, and
        most client libraries split large ones — which is precisely a station
        batching events or audio. Treating each fragment as a whole message
        rejected such a station on every single check.

        The half-assembled message lives on the instance because two ordinary
        things interrupt one. §5.4 lets a control frame arrive *between*
        fragments, and a collection window can end in the middle of one — and
        when the fragments were a local, both threw them away. The next
        continuation frame then had nothing to continue, which desyncs the
        stream permanently: a conformant station that merely pinged mid-message
        failed all sixteen checks, with the same symptom that reassembly was
        added to fix.
        """
        while True:
            piece = self._read_one(deadline)
            if piece is None:
                return None                      # control frame; parts survive
            data, opcode, fin = piece
            if self._parts and opcode != 0x0:
                self.envelope_faults.append(
                    "a new message began before the previous one finished")
                self._parts = []
            if not self._parts and opcode == 0x0:
                self.envelope_faults.append("continuation frame with nothing to continue")
                return None
            self._parts.append(data)
            if sum(len(p) for p in self._parts) > 512 * 1024:
                self.envelope_faults.append(
                    "reassembled message exceeds 512 KiB")
                self._parts = []
                self.close(1009)
                raise ConnectionError("message too large")
            if fin:
                break
        out = b"".join(self._parts)
        self._parts = []
        return out.decode("utf-8", "replace")

    def _read_one(self, deadline: float) -> tuple[bytes, int, bool] | None:
        """One physical frame: `(payload, opcode, fin)`, or None if control."""
        first, second = self._recv(2, deadline)
        fin, opcode = first & 0x80, first & 0x0F
        masked, length = second & 0x80, second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv(8, deadline))[0]
        if length > 512 * 1024:
            # transport.md: frames are capped at 512 KiB, enforced by closing.
            self.envelope_faults.append(f"frame of {length} bytes exceeds 512 KiB")
            self.close(1009)
            raise ConnectionError("frame too large")
        mask = self._recv(4, deadline) if masked else b""
        data = self._recv(length, deadline) if length else b""
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

        if opcode in (0x8, 0x9, 0xA) and len(data) > 125:
            self.envelope_faults.append(
                f"control frame carries {len(data)} bytes; RFC 6455 allows 125")
            data = data[:125]
        if opcode == 0x8:                       # close
            self.closed = True
            raise ConnectionError("station closed the socket")
        if opcode == 0x9:                       # ping
            self.write(data, opcode=0xA)
            return None
        if opcode == 0xA:                       # pong
            self.pongs += 1
            return None
        if opcode == 0x2:
            self.envelope_faults.append(
                "binary frame on the relay; transport.md carries JSON text")
            return None
        if not masked:
            self.envelope_faults.append("unmasked frame from a client (RFC 6455)")
        return data, opcode, bool(fin)

    def write(self, data: bytes, opcode: int = 0x1) -> None:
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        self.sock.sendall(bytes(header) + data)

    def ping(self, payload: bytes = b"percepta") -> bool:
        """Send a WebSocket ping. False if the socket has already gone."""
        if self.closed:
            return False
        try:
            self.write(payload, opcode=0x9)
            return True
        except OSError:
            self.closed = True
            return False

    def close(self, code: int = 1000) -> None:
        try:
            self.write(struct.pack("!H", code), opcode=0x8)
            self.sock.close()
        except Exception:
            pass
        self.closed = True

    # -- the contract's envelope -----------------------------------------

    def send_command(self, payload: dict) -> bool:
        """Send, or record that the socket has gone. Never raises.

        The contract requires stations to reconnect on backoff, and this
        harness itself closes the socket with 1009 on an oversized frame — so a
        disconnect mid-run is ordinary, not exceptional. Raising here aborted
        the run before the block that puts the receiver back, leaving a
        commissioned site tuned to whatever frequency the test chose.
        """
        if self.closed:
            return False
        try:
            self.write(json.dumps({"stream": COMMAND_STREAM,
                                   "payload": payload}).encode())
            return True
        except OSError:
            self.closed = True
            return False

    def read_payload(self, deadline: float) -> tuple[str, dict] | None:
        """One `(kind, payload)`, checking the envelope on the way through."""
        raw = self.read_frame(deadline)
        if raw is None:
            return None
        try:
            # `parse_constant` is what enforces transport.md's rule against
            # NaN and Infinity. Python's parser accepts both by default, and
            # NaN defeats every numeric bound in the schemas — it satisfies
            # `minimum` and `maximum` at once, because comparisons against it
            # are false. Validating without this would pass the sharpest
            # violation the contract describes.
            frame = json.loads(raw, parse_constant=_reject_constant)
        except NonFinite as exc:
            self.envelope_faults.append(str(exc))
            return None
        except ValueError:
            self.envelope_faults.append("frame is not JSON")
            return None
        if not isinstance(frame, dict):
            self.envelope_faults.append("frame is not an object")
            return None
        stream, payload = frame.get("stream"), frame.get("payload")
        extra = set(frame) - {"stream", "payload"}
        if extra:
            self.envelope_faults.append(
                f"envelope carries {sorted(extra)}; transport.md defines two keys")
        if not isinstance(stream, str):
            # Guarded before the membership test below: a non-hashable stream
            # (an object, a list) would otherwise raise TypeError and take the
            # whole run down on a 26-byte frame — from the check whose job is
            # to catch exactly that frame.
            self.envelope_faults.append(
                f"stream is {type(stream).__name__}, expected a one-letter code")
            return None
        if stream == COMMAND_STREAM:
            self.envelope_faults.append(
                "station published on stream 'c', which is downward only")
            self.write(json.dumps({"type": "refused", "stream": "c",
                                   "reason": "c is platform to station"}).encode())
            return None
        if stream not in STREAM_KIND:
            self.envelope_faults.append(f"unknown stream code {stream!r}")
            self.write(json.dumps({"type": "refused", "stream": str(stream),
                                   "reason": "not a stream code"}).encode())
            return None
        if not isinstance(payload, dict):
            self.envelope_faults.append(f"payload on {stream!r} is not an object")
            return None
        return STREAM_KIND[stream], payload


def await_station(port: int, host: str, tls: tuple[str, str] | None,
                  seconds: float) -> Relay:
    """Wait for a station, ignoring whatever else finds the port first.

    Accepts in a loop rather than once. A port scanner, a browser tab or a
    monitoring probe would otherwise take the single slot and leave the real
    station refused — on a bench network that is not a hypothetical, and the
    failure looks like the station never connected.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows: SO_REUSEADDR lets another local process bind a live
        # listening port, which is the opposite of what it means elsewhere.
        server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(8)
    scheme = "wss" if tls else "ws"
    print(f"Listening on {scheme}://{host}:{port}/broker — start the station.\n")

    deadline = time.time() + seconds
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                sys.exit(
                    f"No station connected within {seconds:.0f}s.\n"
                    f"Point one at {scheme}://{host}:{port}/broker and run it.\n"
                    "If the station is on another machine, this listens on "
                    f"{host} — pass --host 0.0.0.0 to accept from the network.\n"
                    "If the station requires TLS it is right to refuse a ws:// "
                    "URL — pass --tls to test that path instead."
                )
            server.settimeout(remaining)
            try:
                sock, peer = server.accept()
            except socket.timeout:
                continue
            # Every peer gets a deadline of its own, before anything that can
            # block. CPython returns an accepted socket in blocking mode even
            # when the listener had a timeout, so without this a peer that
            # opens TCP and then says nothing hangs the run for ever — on the
            # TLS path, inside the handshake.
            sock.settimeout(10)
            try:
                if tls:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(tls[0], tls[1])
                    sock = context.wrap_socket(sock, server_side=True)
                relay = Relay(sock)
                relay.handshake(time.time() + 10)
                return relay
            except (Refused, ssl.SSLError, OSError, ConnectionError,
                    TimeoutError, socket.timeout, UnicodeDecodeError) as exc:
                print(f"  (ignoring {peer[0]}:{peer[1]} — {exc})")
                try:
                    sock.close()
                except OSError:
                    pass
    finally:
        server.close()


# --- checks ----------------------------------------------------------------


def collect(relay: Relay, seconds: float) -> list[tuple[str, dict]]:
    """Everything the station published in the window, in order.

    Order matters for the audio-gating check: it pairs each audio frame with
    the squelch state either side of it, so frames must arrive as sent.
    """
    out: list[tuple[str, dict]] = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            got = relay.read_payload(deadline)
        except (TimeoutError, socket.timeout):
            break
        except ConnectionError as exc:
            if not disconnected:
                disconnected.append(str(exc))
                notes.append(f"socket ended during collection: {exc}")
            break
        if got is not None:
            out.append(got)
    return out


def cadence_from(by_kind: dict[str, list[dict]]) -> dict[str, float]:
    """What this station says its cadences are, falling back to the defaults.

    **Only the streams already expected are timed by it.** A station reporting
    its `audio` or `health` cadence must not thereby be *demanded* to produce
    them: audio is gated on the squelch and a lease, so a quiet band
    legitimately produces none, and health is optional by design.
    """
    cadence = dict(DEFAULT_CADENCE)
    for frame in by_kind.get("health", []):
        reported = frame.get("cadence")
        if not isinstance(reported, dict):
            continue
        for kind, period in reported.items():
            if str(kind) not in cadence:
                continue
            if isinstance(period, (int, float)) and not isinstance(period, bool):
                if period > 0:
                    cadence[str(kind)] = float(period)
    return cadence


def unavailable_now(by_kind: dict[str, list[dict]]) -> set[str]:
    """Streams this station says it has no source for — for the whole run.

    **Every** frame has to say so, not merely one. Latching on any single frame
    let one transient declaration — a tuner reporting "warming up" for a second
    before working perfectly — route that stream's command checks, and both
    audio gates with them, into SKIP for the rest of the run, while the exit
    code stayed 0. That is the expensive rule certified on no evidence at all,
    and a tuner that warms up is the ordinary case rather than an attack.

    A station that goes unavailable partway and stays there now fails its
    command checks instead, which is the loud outcome and the right one:
    something really did stop working.
    """
    return {
        k for k, payloads in by_kind.items()
        if payloads and all(p.get("available") is False for p in payloads)
    }


def matches(got, expect) -> bool:
    if expect is None:                       # presence is the assertion
        return got is not None
    if got == expect:
        return True
    # Frequencies and thresholds are snapped and rounded station-side, so an
    # exact float match would fail a station doing the right thing.
    return (
        isinstance(expect, float)
        and isinstance(got, (int, float))
        and not isinstance(got, bool)
        and abs(got - expect) < 0.6
    )


def await_report(relay: Relay, kind: str, field: str, expect, seconds: float):
    """Wait until the station reports `field` as `expect`, or time out.

    Polling for the answer rather than sampling a window is what makes this
    deterministic: the old version could miss a correct report because the
    window ended a moment early.
    """
    got = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            frame = relay.read_payload(deadline)
        except (TimeoutError, socket.timeout):
            break
        except ConnectionError as exc:
            if not disconnected:
                disconnected.append(str(exc))
                notes.append(f"socket ended while awaiting a report: {exc}")
            break
        if frame is None:
            continue
        stream, payload = frame
        if stream != "telemetry" or payload.get("kind") != kind:
            continue
        if field not in payload:
            continue
        got = payload[field]
        if matches(got, expect):
            return got, True
    return got, False


def _num(value):
    """`value` if it is a usable number, else None.

    Everything `restore` reads came off the wire from the station under test,
    so a string where a frequency belonged raised `TypeError` inside an
    f-string — after the socket had been closed, which lost the verdict and
    both restore notes together. `1e400` is the other one: `parse_constant`
    catches the literal tokens `NaN` and `Infinity`, but a large enough
    exponent parses to `inf` without going near it, and prints as `inf MHz`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def restore(relay: Relay, by_kind: dict[str, list[dict]],
            unavailable_kinds: set[str]) -> None:
    """Put the site back the way it was found.

    This runs against commissioned hardware on a real site, and a station left
    listening to whatever frequency a test chose is a station that stopped
    doing its job quietly. Restore from what the site was observed doing, not
    from an assumption: the command list ends with `auto_squelch on` because
    that pair tests both directions, but a site deliberately running a fixed
    threshold against a known interferer would then be left in AUTO with its
    hand-set threshold discarded, and nobody would connect that to having run
    a test.

    Called from a `finally`, so it still runs when a check raises.
    """
    radio_live = "radio" not in unavailable_kinds
    first_radio = next((p for p in by_kind.get("radio", [])
                        if _num(p.get("freq_hz")) is not None), {})
    was = _num(first_radio.get("freq_hz"))
    was_auto = first_radio.get("auto_squelch")
    was_threshold = _num(first_radio.get("threshold_db"))

    if radio_live and was_auto is False:
        if was_threshold is None:
            # Reporting "returned to a fixed None dB" was worse than silence:
            # it says the site is safe when the threshold was never recovered.
            notes.append("COULD NOT return the squelch: this site was on a "
                         "fixed threshold but never reported a usable one, "
                         "and it is now on AUTO — check it")
        else:
            relay.send_command({"kind": "radio.squelch", "db": was_threshold})
            _, back = await_report(relay, "radio", "threshold_db",
                                   was_threshold, COMMAND_TIMEOUT)
            notes.append(
                f"squelch returned to a fixed {was_threshold} dB" if back else
                f"COULD NOT return the squelch to {was_threshold} dB — this "
                "site was on a fixed threshold and is now on AUTO; check it")

    if radio_live and any(k == "radio.tune" for k, *_ in COMMANDS):
        if was is None:
            notes.append("COULD NOT return the receiver: this run never saw a "
                         "frequency to put it back to, and it was tuned to "
                         "119.500 MHz — check the site")
        elif not relay.send_command({"kind": "radio.tune", "freq_hz": was}):
            notes.append(f"COULD NOT return the receiver to {was / 1e6:.3f} MHz: "
                         "the station disconnected — check the site")
        else:
            _, back = await_report(relay, "radio", "freq_hz", was, COMMAND_TIMEOUT)
            notes.append(f"receiver returned to {was / 1e6:.3f} MHz" if back else
                         f"COULD NOT return the receiver to {was / 1e6:.3f} MHz "
                         "— check the site")

    # The floodlight is switched twice by section 5 and was never put back —
    # so a daytime run could leave a site lit all night, which costs a battery
    # that has to last until morning.
    if "light" not in unavailable_kinds and any(
            k == "light.set" for k, *_ in COMMANDS):
        was_lit = next((p.get("on") for p in by_kind.get("light", [])
                        if isinstance(p.get("on"), bool)), None)
        lit = "on" if was_lit else "off"
        if was_lit is None:
            notes.append("COULD NOT return the floodlight: this run never saw "
                         "its state and has been switching it — check the site")
        elif not relay.send_command({"kind": "light.set", "on": was_lit}):
            notes.append(f"COULD NOT return the floodlight to {lit}: the "
                         "station disconnected — check the site")
        else:
            _, back = await_report(relay, "light", "on", was_lit, COMMAND_TIMEOUT)
            notes.append(f"floodlight returned to {lit}" if back else
                         f"COULD NOT return the floodlight to {lit} — check "
                         "the site")

    relay.send_command({"kind": "radio.spectrum", "on": False})
    relay.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PERCEPTA_RELAY_PORT", 8099)))
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to listen on; 0.0.0.0 for another machine")
    ap.add_argument("--wait", type=float, default=120.0,
                    help="seconds to wait for the station to connect")
    ap.add_argument("--listen-for", type=float, default=MAX_WAIT, dest="listen_for",
                    help="longest to listen for a slow stream, in seconds "
                         f"(default {MAX_WAIT:g}); raise it for a station on a "
                         "very slow cadence")
    ap.add_argument("--tls", nargs=2, metavar=("CERT", "KEY"),
                    help="serve wss:// with this certificate and key")
    args = ap.parse_args()

    telemetry_schema = load("telemetry.schema.json")
    audio_schema = load("audio.schema.json")
    events_schema = load("events.schema.json")

    max_listen = max(MIN_WAIT, args.listen_for)
    relay = await_station(args.port, args.host, args.tls, args.wait)

    by_kind: dict[str, list[dict]] = {}
    unavailable_kinds: set[str] = set()
    try:
        print("0. Connection")
        check("authenticates with a bearer credential", bool(relay.credential),
              "no Authorization header")
        if relay.credential:
            shown = relay.credential[:4] + "…" if len(relay.credential) > 4 else "…"
            notes.append(f"credential presented ({shown}), not verified by this harness")
        check("connects to /broker", relay.path.startswith("/broker"),
              f"path was {relay.path!r}")

        # Ask for audio before listening. A station that obeys the contract sends
        # none at all unless somebody has asked — so without this, the gating check
        # below would have nothing to judge and would silently pass every
        # lease-respecting station while only ever testing the ones that ignore it.
        audio_request = {"kind": "radio.audio", "on": True, "lease_seconds": 30}
        relay.send_command(audio_request)

        # Ping now, judge in section 2. A station has the whole opening window
        # to answer, which is far longer than the ten seconds transport.md
        # allows, so this fails only a station that never pongs at all.
        relay.ping()

        print("\n1. Telemetry")
        seen = collect(relay, MIN_WAIT)
        by_kind: dict[str, list[dict]] = {}
        for stream, payload in seen:
            key = "audio" if stream == "audio" else (
                "events" if stream == "events" else str(payload.get("kind")))
            by_kind.setdefault(key, []).append(payload)

        spoken = next(
            (f.get("contract_version") for f in by_kind.get("health", [])
             if f.get("contract_version")), None)
        if spoken is None:
            notes.append(f"station declares no contract_version; this checks "
                         f"{CONTRACT_VERSION}")
        elif spoken != CONTRACT_VERSION:
            notes.append(f"station speaks contract {spoken}, this checks "
                         f"{CONTRACT_VERSION}")

        def absorb(window: float) -> None:
            relay.send_command(audio_request)
            for stream, payload in collect(relay, window):
                seen.append((stream, payload))
                key = "audio" if stream == "audio" else (
                    "events" if stream == "events" else str(payload.get("kind")))
                by_kind.setdefault(key, []).append(payload)

        listened = MIN_WAIT
        # Health arrives every 30 s by default and carries the cadences everything
        # below is judged against, so a first window of 8 s discovers it about one
        # run in four. Without it the harness falls back to the defaults, believes
        # it waited long enough, and fails a station that told it otherwise — the
        # outcome depending on where the station's health tick happened to land.
        if not by_kind.get("health") and listened < HEALTH_PERIOD + 2:
            extra = min(max_listen, HEALTH_PERIOD + 2) - listened
            if extra > 0:
                absorb(extra)
                listened += extra

        cadence = cadence_from(by_kind)          # health may have only just arrived
        if cadence != DEFAULT_CADENCE:
            stated = ", ".join(f"{k} {v:g}s" for k, v in sorted(cadence.items())
                               if DEFAULT_CADENCE.get(k) != v)
            notes.append(f"station reports its own cadence: {stated}")

        missing = [k for k in cadence if not by_kind.get(k)]
        if missing:
            want = min(max_listen, max(cadence[k] * PERIODS for k in missing))
            extra = want - listened
            if extra > 0:
                absorb(extra)
                listened += extra

        # Audio is the one thing worth waiting for that no cadence describes.
        #
        # It is not a telemetry kind and it has no period: it arrives when
        # somebody keys a microphone, which on a real channel may be minutes
        # apart. The extension above only stretches for a *missing telemetry
        # stream*, so a station publishing everything else promptly finished
        # the window in eight seconds and reported the two audio rules
        # untested — and `--listen-for`, the flag whose entire purpose is to
        # wait longer, did nothing at all because nothing was missing.
        #
        # So: if this station has a receiver and has not yet sent audio, wait
        # out the rest of the budget. Costs nothing on a busy channel, because
        # `absorb` stops as soon as the window closes, and it is the difference
        # between certifying the expensive rules and skipping them.
        if ("radio" not in unavailable_now(by_kind) and by_kind.get("radio")
                and not by_kind.get("audio") and listened < max_listen):
            absorb(max_listen - listened)
            listened = max_listen

        for kind in cadence:
            payloads = by_kind.get(kind, [])
            unavailable = [p for p in payloads if p.get("available") is False]
            needed = cadence[kind] * PERIODS
            if unavailable:
                # A station saying "I have no receiver for this" is conformant.
                # Demanding a payload it cannot honestly fill is what makes a
                # station invent numbers, which is what this harness exists to
                # prevent.
                #
                # But the reason has to be a reason. `minLength: 1` is satisfied by
                # a space, and README promises a station "is failed for pretending"
                # — a blank reason on every stream is the shape of pretending.
                # Type-checked before `.strip()`, because everything here is
                # station-supplied and section 3 is the part that reports a wrong
                # type. A list here used to raise `AttributeError` in section 1 and
                # take the whole run down before the schema check that would have
                # explained it ever printed.
                raw_reason = unavailable[-1].get("unavailable_reason")
                reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
                check(f"{kind} unavailable, with a reason", bool(reason),
                      "unavailable_reason is blank, whitespace, or not a string")
                notes.append(f"{kind} declared unavailable: {reason or '(blank)'}")
            elif not payloads and needed > listened + 0.5:
                # Absent, but this run never waited long enough to expect it. The
                # contract lets a metered site slow a stream down and says a
                # station must not be failed for it, so MAX_WAIT capping the
                # window is the harness's limitation and not the station's fault.
                # Reporting it as a failure — with a duration the harness never
                # actually waited — is how correctly-configured hardware gets
                # rejected on site.
                skipped_streams.append(kind)
                skip(f"publishes {kind}",
                     f"reported cadence needs {needed:g}s and this run listened "
                     f"for {listened:g}s; re-run with --listen-for {needed:.0f}")
            else:
                check(f"publishes {kind}", bool(payloads),
                      f"nothing received in {listened:g}s")

        print("\n2. Envelope")
        # The relay format is the newest surface in the contract and the one with
        # no schema, so it is checked here rather than by a validator.
        check("frames match the relay envelope", not relay.envelope_faults,
              "; ".join(sorted(set(relay.envelope_faults))[:3]))
        # A station that never pongs cannot tell a dead link from a quiet one,
        # and neither can the platform: commands are unrequested, so a silent
        # hour is normal and proves nothing either way.
        check("answers a WebSocket ping", relay.pongs > 0,
              "no pong in the opening window; a half-open socket would go "
              "unnoticed at both ends")

        print("\n3. Schema")
        for kind, payloads in sorted(by_kind.items()):
            if kind == "audio":
                schema = audio_schema
            elif kind == "events":
                schema = events_schema
            elif kind in cadence or kind in OPTIONAL_KINDS:
                schema = telemetry_schema
            else:
                notes.append(f"unknown kind '{kind}' ignored, as the contract allows")
                continue
            first: list = []
            bad = 0
            for payload in payloads:
                errs = sorted(schema.iter_errors(payload), key=str)
                if errs:
                    bad += 1
                    first = first or errs
            detail = "; ".join(e.message for e in first[:2])
            if bad:
                detail += f"  ({bad} of {len(payloads)} frame(s))"
            check(f"{kind} matches schema ({len(payloads)} frame(s))", not bad, detail)

        # Events are validated and deliberately NOT acknowledged: `events.ack`
        # means "durably stored, delete your copy", this stores nothing, and the
        # file runs against real sites. A conformance check must never cost a
        # station its undelivered history.
        if by_kind.get("events"):
            notes.append("events validated but NOT acknowledged — this harness "
                         "stores nothing, and acking would tell the station to "
                         "delete them")

        print("\n4. Audio is gated")
        # Every audio frame must sit against a squelch that was open. A station may
        # publish audio on a faster sub-tick than its 1 Hz radio telemetry, so a
        # transmission starting mid-interval legitimately puts audio on the wire
        # just before the radio frame announcing the open gate — frames are
        # therefore judged against the state either side, and only audio bracketed
        # by a closed gate is a failure.
        ordered = [p for _, p in seen]
        if "radio" in unavailable_now(by_kind):
            notes.append("radio unavailable, so audio gating was not tested")
        elif not by_kind.get("radio"):
            check("audio only while squelch is open", False, "no radio telemetry")
        elif not by_kind.get("audio"):
            # Not a pass. Nothing was gated because nothing was sent.
            unexercised.append("audio gating")
            skip("audio only while squelch is open",
                 "no audio in the window; raise --listen-for, or run on a "
                 "busier channel")
        else:
            def open_at(index: int, step: int) -> bool | None:
                i = index + step
                while 0 <= i < len(ordered):
                    if ordered[i].get("kind") == "radio":
                        return bool(ordered[i].get("squelch_open")
                                    or ordered[i].get("monitor"))
                    i += step
                return None

            ungated = 0
            for index, payload in enumerate(ordered):
                if payload.get("kind") != "audio":
                    continue
                before, after = open_at(index, -1), open_at(index, 1)
                if before is False and after is not True:
                    ungated += 1
            check("audio only while squelch is open", ungated == 0,
                  f"{ungated} audio frame(s) with the squelch closed either side")

        print("\n5. Commands take effect")
        unavailable_kinds = unavailable_now(by_kind)
        exercised = 0
        for kind, body, report_kind, field, expect in COMMANDS:
            if report_kind in unavailable_kinds:
                skip(f"{kind} -> {report_kind}.{field}",
                     f"{report_kind} is declared unavailable")
                continue
            if not relay.send_command({"kind": kind, **body}):
                skip(f"{kind} -> {report_kind}.{field}", "the station disconnected")
                continue
            got, ok = await_report(relay, report_kind, field, expect, COMMAND_TIMEOUT)
            check(f"{kind} -> {report_kind}.{field}", ok, f"reported {got!r}")
            exercised += 1

        print("\n6. Audio stops when the lease lapses")
        # The second gate, and the expensive one. Checking only squelch gating
        # would pass a station that ignores the lease entirely and streams whenever
        # the band is busy — which is precisely the behaviour the lease was added
        # to prevent, and which the reference implementation once shipped. So: ask
        # for a short lease, stop asking, and confirm the audio stops.
        if "radio" in unavailable_kinds:
            notes.append("radio unavailable, so the audio lease was not tested")
        elif not by_kind.get("audio"):
            unexercised.append("the audio lease")
            skip("audio stops when the lease lapses", "no audio seen at all")
        else:
            relay.send_command({"kind": "radio.audio", "on": True, "lease_seconds": 5})
            collect(relay, 9.0)                      # let it lapse, unrenewed
            after = collect(relay, 6.0)
            leftover = [p for _, p in after if p.get("kind") == "audio"]
            opened = any(p.get("kind") == "radio"
                         and (p.get("squelch_open") or p.get("monitor"))
                         for _, p in after)
            check("audio stops when the lease lapses", not leftover,
                  f"{len(leftover)} audio frame(s) sent with no live lease")
            if not opened and not leftover:
                notes.append("the band was quiet after the lease lapsed, so silence "
                             "there proves less than it looks; re-run on a busy "
                             "channel to be sure")

    except Exception as exc:                  # a bug in this harness
        # Never a silent traceback and never an exit 0. The site still
        # gets put back below, and the run is loudly not a certificate.
        traceback.print_exc()
        failures.append(f"the harness itself raised {exc!r} — this is a "
                        "harness fault, not evidence about the station")
    finally:
        restore(relay, by_kind, unavailable_kinds)

    if skipped:
        print("\nNot tested")
        for s in skipped:
            print(f"  - {s}")
    if notes:
        print("\nNotes")
        for n in notes:
            print(f"  - {n}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    if skipped_streams:
        print(
            f"INCONCLUSIVE: {', '.join(skipped_streams)} never observed. The\n"
            "station declared a cadence longer than this run listened for, so\n"
            "those streams were not tested — and the cadence is a number the\n"
            "station itself supplies, so this is not evidence of anything.\n"
            "Re-run with --listen-for long enough to see them."
        )
        return 2
    if unexercised:
        print(
            f"INCONCLUSIVE: {', '.join(unexercised)} could not be tested — no\n"
            "audio arrived in this run. Nothing here is wrong: airband is quiet\n"
            "most of the time and a station with nothing to send is behaving\n"
            "correctly. But the squelch gate and the lease are the two most\n"
            "expensive rules in this contract, and a run that never heard a\n"
            "transmission has proved nothing about either.\n"
            "Raise --listen-for, or run against a busier channel."
        )
        return 2
    if disconnected:
        print(
            f"INCONCLUSIVE: the socket ended mid-run ({disconnected[0]}). Every\n"
            "check after that point had nothing to fail on — a dead socket\n"
            "delivers no audio, so the lease rule in particular was certified\n"
            "on no evidence at all. Nothing here says the station is wrong;\n"
            "nothing here proves it is right. Check the notes above: the site\n"
            "may have been left mistuned."
        )
        return 2
    if exercised == 0:
        # A station that declares every stream unavailable skips every command
        # check and reaches this point with nothing tested but its ability to
        # say "no hardware here". That is a conformant thing to say and it is
        # not a pass — reporting it as one lets the emptiest possible station
        # collect a certificate, which is the opposite of what this exists for.
        print("INCONCLUSIVE: nothing was exercised — every command was skipped\n"
              "because its stream was declared unavailable. Nothing here is\n"
              "wrong; nothing here was tested either. Run against a station\n"
              "with hardware fitted before believing this.")
        return 2
    print(f"All checks passed — {exercised} of {len(COMMANDS)} commands "
          f"exercised, this station satisfies the contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
