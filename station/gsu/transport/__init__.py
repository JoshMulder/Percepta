"""The only part of this station that knows how it reaches the platform.

`contract/transport.md`: one authenticated WebSocket to `/broker`, carrying
frames of exactly two keys.

    ->  {"stream": "t", "payload": {…}}     telemetry
    ->  {"stream": "a", "payload": {…}}     audio
    ->  {"stream": "e", "payload": {…}}     an events batch
    <-  {"stream": "c", "payload": {…}}     commands, unrequested

**There is nothing here for a station to name.** No channel, no topic, no
station id — the platform resolves all three from the credential on the socket.
That is not a simplification, it is the confinement: a station cannot address
another station's channel because there is no field in which to say one. This
interface is narrow for the same reason, and the narrowness is the point.

Until 2.0 this took a topic string and the topic strings came from the
enrolment response. Both are gone. A transport that still accepted a topic
would be asking every caller to hold a name that means nothing and asking
enrolment to keep issuing names the contract deleted.

Publishing is fire-and-forget and no caller waits for an acknowledgement — a
False is a dropped frame, which telemetry is explicitly allowed to be. The one
exception is the events stream, which is a ledger rather than current state;
its delivery guarantee lives in `gsu/events.py`, above this layer, because it
is built out of acknowledgements and re-sends rather than out of anything a
socket can promise.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

#: The contract this station speaks, reported in every health frame.
#:
#: Declared, never negotiated. A platform reads it to know what a station is
#: capable of; there is no handshake and nothing to agree. Bump the minor when
#: this station starts *emitting* something a 2.0 platform would not recognise
#: — that is free and requires no coordination. Bumping the major is a fleet
#: operation and is not a thing to do by editing this line.
CONTRACT_VERSION = "2.0"

#: Relay stream codes, per `contract/transport.md`. A station publishes on the
#: first three and only ever receives on the fourth.
TELEMETRY = "t"
AUDIO = "a"
EVENTS = "e"
COMMAND = "c"

#: Streams a station is permitted to publish on. Anything else earns a
#: `refused` frame from the platform with the socket left up.
PUBLISHABLE = frozenset({TELEMETRY, AUDIO, EVENTS})

#: Called with one command payload. No channel argument: there is exactly one
#: downward stream and the platform already knows whose commands these are.
Handler = Callable[[dict], None]


def redact_url(url: str | None) -> str | None:
    """A URL safe to show on the console and to publish in health telemetry.

    The local console has no authentication and health frames cross the wire;
    neither is a place to render a secret somebody pasted into an environment
    variable. The platform's `broker.url` is credential-free by contract, so
    this defends against a hand-edited one rather than against the normal case.
    """
    if not url:
        return url
    scheme, separator, rest = url.partition("://")
    if not separator or "@" not in rest.partition("/")[0]:
        return url
    authority, slash, tail = rest.partition("/")
    userinfo, _, host = authority.rpartition("@")
    username, _, _ = userinfo.partition(":")
    return f"{scheme}://{'user' if username else ''}:***@{host}{slash}{tail}"


class Transport(ABC):
    """One link to the platform. Everything above this is transport-agnostic."""

    @abstractmethod
    def start(self) -> None:
        """Begin connecting. Must not block on the link being up: a station
        boots and works whether or not the platform is reachable."""

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def publish(self, stream: str, payload: dict) -> bool:
        """Send one payload on one stream. True if it left the box.

        Never raises, never blocks for long, never queues. Telemetry is current
        state, not a ledger — a frame that cannot be sent now is worth less than
        the one along in a second, and replaying stale readings into a live
        console is worse than a gap (`contract/transport.md`).
        """

    @abstractmethod
    def on_command(self, handler: Handler) -> None:
        """Deliver every command to `handler`, across reconnects.

        Nothing is sent to ask for this. There is no subscribe handshake in 2.0:
        commands arrive from the moment the socket opens because the credential
        already determines whose they are, and a station that tried to subscribe
        would be naming a channel.
        """

    @abstractmethod
    def set_credential(self, secret: str) -> None:
        """Use this from the next connection. Called after a renewal; the old
        credential keeps working through the 24 h overlap, so this does not need
        to interrupt anything to take effect."""

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @property
    @abstractmethod
    def dropped(self) -> int:
        """Frames that could not be sent since start. Reported in health
        telemetry, because a station that is quietly dropping everything looks
        exactly like a quiet site."""

    @property
    def refusals(self) -> dict[str, str]:
        """Streams the platform refused this station, and what it said.

        A refusal and an unreachable platform both surface as a failed publish
        and are completely different faults: the first will never fix itself.
        Kept per stream because that is the granularity the platform refuses at.
        """
        return {}


def build_transport(url: str, secret: str | None, trust=None) -> Transport:
    """The relay, which is the only transport contract 2.0 defines.

    There were two. `redis_transport.py` connected to the broker directly for a
    bench where 6380 was reachable, and it was deleted with 2.0 rather than
    ported: it spoke a topic-based protocol that no contract document describes
    any more, and it was the only reason this interface needed to stay
    topic-shaped. A second wire format that nothing tests is not a convenience.

    The bench case is better served now than it was — `contract/conformance/
    check_station.py` is a platform that speaks this exact protocol, needs no
    Redis, no database and no station id, and answers whether the station is
    *conformant* rather than whether bytes moved.

    `trust` is the station's pinned CA (`gsu/tls.py`), passed rather than looked
    up because a transport must refuse instead of connecting without it.
    """
    scheme = (url.split("://", 1)[0] or "").lower()
    if scheme in ("ws", "wss"):
        from .relay import RelayTransport

        return RelayTransport(url, secret=secret, trust=trust)
    raise ValueError(
        f"No transport knows how to speak {scheme!r} (from {url!r}). "
        "Contract 2.0 defines one transport: wss:// to the platform's /broker."
    )
