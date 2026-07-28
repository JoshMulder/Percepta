"""MQTT over TLS — the intended production transport, deliberately not faked.

There is no MQTT broker to develop against here and no certificate to present,
so writing an implementation now would produce code that has never connected to
anything. That is worse than an honest gap: it looks tested.

What it has to do, so the work is bounded and the seam is provably the only one:

* Connect to `broker.url` from the enrolment response, verifying the server
  against `broker.ca_pem` — **pinned**, not the system trust store. That part is
  now built and in use on the Redis path: `gsu/tls.py` persists the CA and
  `Trust.context()` produces the verifying context. This class takes the same
  `Trust` and must call `trust.check(url, …)` before connecting and
  `trust.context()` to wrap the socket. No fallback to plaintext, and no
  `CERT_NONE`, on this transport either.
* Authenticate as `broker.username` with the credential secret as the password,
  and later as an mTLS client certificate (§3) — at which point this class grows
  a keypair and CSR and nothing above the transport changes.
* Publish with QoS 0. The contract is explicit that telemetry may be dropped and
  that ordering is per channel only; QoS 1 would buy a delivery guarantee for
  data whose value expires in a second, at the cost of a queue that replays
  stale readings into a live console after an outage.
* Subscribe to the command topic with QoS 1 and a *clean session*. A command is
  a request, not a guarantee; a persistent session would deliver an hour-old
  "light on" to a station that has since been told otherwise.
* Set a short keepalive and reconnect on a backoff, never blocking the sensing
  loop.

Topics need no translation: the platform issues slash-separated names
(`gsu/{id}/telemetry`, `cmd/gsu/{id}`) which are already valid MQTT topics.
"""

from __future__ import annotations

from . import Handler, Transport


class MqttTransport(Transport):
    def __init__(self, url: str, username: str | None = None,
                 password: str | None = None, trust=None):
        raise NotImplementedError(
            "MQTT transport is not built. Production is MQTT over TLS with a "
            "per-station credential; see the module docstring for exactly what "
            "it must do, and station/DECISIONS.md for the broker-hosting "
            "decision it waits on (contract/enrolment.md §9.4)."
        )

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def publish(self, topic: str, payload: dict) -> bool: ...
    def subscribe(self, topic: str, handler: Handler) -> None: ...
    def set_credentials(self, username: str, password: str) -> None: ...

    @property
    def connected(self) -> bool:
        return False

    @property
    def dropped(self) -> int:
        return 0
