"""The only part of this station that knows what the broker is.

`contract/transport.md`: Redis pub/sub today, MQTT over TLS in production, and
the difference is to be confined to one place. This is that place, and the
interface is deliberately narrow enough that it can be: publish a JSON payload
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


def build_transport(url: str, username: str | None, password: str | None) -> Transport:
    """Pick a transport from the broker URL the platform handed out.

    The one function that has to change when production moves to MQTT, and the
    reason `mqtt.py` exists as a stub rather than as a surprise.
    """
    scheme = (url.split("://", 1)[0] or "").lower()
    if scheme in ("redis", "rediss", "unix"):
        from .redis_transport import RedisTransport

        return RedisTransport(url, username=username, password=password)
    if scheme in ("mqtt", "mqtts", "ssl", "tcp", "ws", "wss"):
        from .mqtt import MqttTransport

        return MqttTransport(url, username=username, password=password)
    raise ValueError(f"No transport knows how to speak {scheme!r} (from {url!r})")
