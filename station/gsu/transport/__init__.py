"""The only part of this station that knows what the broker is.

`contract/transport.md`: a WebSocket relay on 443 in production, Redis pub/sub
direct on a bench, and the difference confined to one place. This is that
place, and the interface is deliberately narrow enough that it can be: publish
a JSON payload
to a topic, subscribe to a topic, and say whether the link is up.

Both transports are fire-and-forget from the station's point of view and the
contract assumes nothing stronger, so `publish` returns whether the frame left
the box and *no caller waits for an acknowledgement*. A False is a dropped
frame, which telemetry is explicitly allowed to be.

Topic strings come from the enrolment response, not from string-building here.
The station is told its three channels (`contract/enrolment.md` §4) and uses
exactly those, which is also why MQTT needs no translation layer: the same
slash-separated names are valid MQTT topics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

#: Called with (topic, payload) for every message received on a subscription.
Handler = Callable[[str, dict], None]


def split_credentials(url: str) -> tuple[str, str | None, str | None]:
    """Separate any `user:pass@` in a broker URL from the address.

    This exists because of a genuine trap in redis-py: `ConnectionPool.from_url`
    ends with `kwargs.update(url_options)`, so **the URL wins over the keyword
    arguments**. A URL carrying its own credentials silently discards the
    station's identity — which either fails as `WRONGPASS`, or succeeds as
    whoever the URL names, which is much worse. A station that authenticates as
    something other than `gsu:{station_id}` has quietly left the tenancy model
    the whole platform rests on.

    So the address and the identity are separated here and the identity is
    always passed as arguments. The platform's `broker.url` is deliberately
    credential-free; this defends the case where someone puts a password into
    `GSU_BROKER_URL` because it worked in `redis-cli`.
    """
    scheme, separator, rest = url.partition("://")
    if not separator:
        return url, None, None
    authority, slash, tail = rest.partition("/")
    if "@" not in authority:
        return url, None, None
    userinfo, _, host = authority.rpartition("@")
    username, _, password = userinfo.partition(":")
    return f"{scheme}://{host}{slash}{tail}", (username or None), (password or None)


def redact_url(url: str | None) -> str | None:
    """A URL safe to show on the console and to publish in health telemetry.

    The local console has no authentication and health frames cross the wire;
    neither is a place to render a password that somebody pasted into an
    environment variable.
    """
    if not url:
        return url
    address, username, password = split_credentials(url)
    if username is None and password is None:
        return url
    scheme, _, rest = address.partition("://")
    return f"{scheme}://{'user' if username else ''}:***@{rest}"


class Transport(ABC):
    """One link to the platform. Everything above this is broker-agnostic."""

    @abstractmethod
    def start(self) -> None:
        """Begin connecting. Must not block on the link being up: a station
        boots and works whether or not the platform is reachable."""

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def publish(self, topic: str, payload: dict) -> bool:
        """Send one JSON payload. True if it left the box.

        Never raises, never blocks for long, never queues. Telemetry is current
        state, not a ledger — a frame that cannot be sent now is worth less than
        the one along in a second, and replaying stale readings into a live
        console is worse than a gap (`contract/transport.md`).
        """

    @abstractmethod
    def subscribe(self, topic: str, handler: Handler) -> None:
        """Deliver messages on `topic` to `handler`, and keep the subscription
        alive across reconnects. A station whose command subscription silently
        dies looks identical to one that ignores commands."""

    @abstractmethod
    def set_credentials(self, username: str, password: str) -> None:
        """Use these from the next connection. Called after a renewal; the old
        credential keeps working through the overlap window, so this does not
        need to interrupt anything."""

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
        """Topics the broker refused this station, and what it said.

        A refused channel and an unreachable broker both come back as a failed
        publish, and they are completely different faults: the first is an ACL
        that does not grant what the station was built to send, and it will
        never fix itself. Kept per topic because it is per topic — a station may
        be entitled to publish telemetry and not video, which is exactly the
        state the video channel is in today (CONTRACT-QUESTIONS.md item 12).

        Not abstract: a transport that cannot tell the difference reports none
        rather than being unable to exist.
        """
        return {}


def build_transport(
    url: str,
    username: str | None,
    password: str | None,
    trust=None,
) -> Transport:
    """Pick a transport from the broker URL the platform handed out.

    Two transports: `wss://` is the deployment one — the relay in `relay.py`,
    which reaches the broker over the only port that is open everywhere — and
    `rediss://` is the direct connection, for a bench where the broker's own
    port is reachable.

    There was a third, `mqtt.py`, which was a stub that never connected to
    anything. MQTT is the better protocol for this traffic and it lost on one
    thing: port 8883, which is shut wherever 6380 is. Carrying a second broker
    and a client library to arrive at what the relay does in two files with no
    dependency was not worth it. See DECISIONS.md.

    `trust` is the station's pinned CA (`gsu/tls.py`). Every transport takes it
    and every transport must refuse rather than connect without it, which is
    why it is a parameter here and not a lookup inside one implementation.
    """
    scheme = (url.split("://", 1)[0] or "").lower()
    if scheme in ("redis", "rediss", "unix"):
        from .redis_transport import RedisTransport

        return RedisTransport(url, username=username, password=password, trust=trust)
    if scheme in ("ws", "wss"):
        # The deployment transport. See relay.py: 443 is the one port open
        # everywhere, and this is a message relay rather than a Redis proxy so
        # that a station can reach the broker without being able to address
        # anybody else's channels.
        from .relay import RelayTransport

        return RelayTransport(url, username=username, password=password,
                              trust=trust)
    raise ValueError(f"No transport knows how to speak {scheme!r} (from {url!r})")
