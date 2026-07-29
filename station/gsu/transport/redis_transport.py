"""Redis pub/sub, which is the broker on both sides of the TLS change.

Three things here are not incidental:

**It authenticates as `gsu:{station_id}` from the first connection.** Redis'
`default` user is closed now, so this is the only way in — but it was written
this way while `default` was still open, because code that never authenticated
stops working everywhere at once on the day it is closed.

**It verifies the broker against the pinned CA, or it does not connect.**
`rediss://` with `ssl_ca_certs` set to the CA from the enrolment response,
`ssl_cert_reqs=required` and `ssl_check_hostname=True`, all passed explicitly
rather than left to whichever redis-py the field box has. There is no plaintext
fallback and no unverified mode: a failed handshake surfaces as a dropped
uplink and a health condition, which is visible, rather than as a quiet
downgrade, which is not.

**It never queues.** A publish while the link is down returns False and the
frame is gone. Reconnection is attempted on a backoff by a thread that does not
hold up the sensing loop, so a station under an obstruction keeps sensing,
recording and alerting at full rate and simply stops talking.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time

import redis

from ..tls import Refusal, Trust, is_tls
from . import Handler, Transport, redact_url, split_credentials

log = logging.getLogger("gsu.transport")

#: TLS settings this station will not connect without. If the installed
#: redis-py is too old to accept one of them, that is a refusal rather than a
#: warning: silently connecting with hostname checking off is precisely the
#: kind of downgrade nobody would find out about.
MANDATORY_TLS_KWARGS = ("ssl_cert_reqs", "ssl_check_hostname", "ssl_ca_certs")

#: Reconnection backoff. Starlink obstructions are seconds to a minute, so the
#: first retries are quick; the cap keeps a long outage from hammering.
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0

#: Short timeouts: a publish that blocks is a tick that does not happen, and the
#: sensing loop must never wait on the network.
CONNECT_TIMEOUT = 3.0
SOCKET_TIMEOUT = 3.0

#: What Redis says when a principal is not granted a channel. Matched as text as
#: well as by exception class because older redis-py versions raise a plain
#: `ResponseError` for it, and a station that mistook this for a broken link
#: would drop everything else too.
_PERMISSION_MARKERS = ("noperm", "no permissions")


def _is_permission_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _PERMISSION_MARKERS)


class RedisTransport(Transport):
    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        trust: Trust | None = None,
    ):
        # redis-py's `from_url` lets the URL override the keyword arguments, so
        # a URL carrying `user:pass@` would silently replace this station's
        # identity with whatever it names. Strip it, and connect as the station
        # or not at all — publishing as another principal is worse than not
        # publishing (`contract/README.md` rule 1).
        self.url, url_user, url_password = split_credentials(url)
        if url_user or url_password:
            log.warning(
                "The broker URL carried credentials; ignoring them and "
                "authenticating as %s. A station publishes as itself or not at "
                "all. Put the address in GSU_BROKER_URL and nothing else.",
                username or "(no username)",
            )
        self.trust = trust or Trust()
        # Refuse here, at construction, rather than at the first publish: the
        # agent turns this into a health condition and a line on the local
        # console, and a station that will not publish must say so immediately
        # rather than looking merely offline.
        self.trust.check(self.url, "the broker")
        self._tls_kwargs = self._resolve_tls_kwargs()
        self._username = username
        self._password = password
        self._client: redis.Redis | None = None
        self._connected = False
        self._dropped = 0
        self._next_attempt = 0.0
        self._backoff = BACKOFF_START
        self._lock = threading.Lock()
        self._subscriptions: list[tuple[str, Handler]] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_error: str | None = None
        #: Topics this station's principal is not granted, and what the broker
        #: said about each. Cleared per topic on the first successful publish to
        #: it, so a fixed ACL clears the condition without a restart.
        self._refusals: dict[str, str] = {}
        #: Set when the last failure was a rejected certificate rather than an
        #: unreachable host. The agent reports the two differently: one is a
        #: dropout, the other is a station talking to the wrong broker.
        self.tls_failed = False

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        # Connecting is left to the first publish and to the subscriber thread.
        # Nothing here blocks: a box that would not boot without the platform is
        # a box that stops working when the link does.

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()
        with self._lock:
            self._close()

    def set_credentials(self, username: str, password: str) -> None:
        with self._lock:
            self._username = username
            self._password = password
            # Drop the connection so the next one authenticates with the new
            # secret. The overlap window means the old one still works, so this
            # is a tidy-up rather than a race.
            self._close()

    # --- publishing -----------------------------------------------------

    def publish(self, topic: str, payload: dict) -> bool:
        client = self._ensure_client()
        if client is None:
            self._dropped += 1
            return False
        try:
            client.publish(topic, json.dumps(payload, separators=(",", ":")))
            if not self._connected:
                log.info("Uplink restored (%s).", self.url)
            self._connected = True
            self._backoff = BACKOFF_START
            self._last_error = None
            self._refusals.pop(topic, None)
            return True
        except redis.exceptions.NoPermissionError as exc:
            # The connection is fine and this station is authenticated; it is
            # simply not granted this channel. Treating that as a dead link —
            # which is what it used to do — would close a working connection and
            # back off the whole uplink because of one topic, so telemetry would
            # start dropping because video is not permitted. Recorded per topic,
            # counted as a drop, and nothing else disturbed.
            self._note_refusal(topic, exc)
            self._dropped += 1
            return False
        except (redis.RedisError, OSError) as exc:
            if _is_permission_error(exc):
                self._note_refusal(topic, exc)
                self._dropped += 1
                return False
            self._fail(exc)
            self._dropped += 1
            return False

    def _note_refusal(self, topic: str, exc: Exception) -> None:
        text = str(exc)
        if self._refusals.get(topic) != text:
            log.error(
                "The broker refused %s for %s: %s. The connection is up and "
                "authenticated — this is an ACL that does not grant the channel, "
                "and it will not clear on its own.",
                topic, self._username or "(no username)", text,
            )
        self._refusals[topic] = text

    # --- subscribing ----------------------------------------------------

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subscriptions.append((topic, handler))
        thread = threading.Thread(
            target=self._subscribe_forever, args=(topic, handler),
            name=f"gsu-sub-{topic}", daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def _subscribe_forever(self, topic: str, handler: Handler) -> None:
        while not self._stop.is_set():
            pubsub = None
            try:
                client = self._new_client()
                pubsub = client.pubsub()
                pubsub.subscribe(topic)
                log.info("Subscribed to %s as %s.", topic, self._username or "default")
                self._backoff = BACKOFF_START
                while not self._stop.is_set():
                    message = pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if message is None:
                        continue
                    self._deliver(message, topic, handler)
            except (redis.RedisError, OSError) as exc:
                # Losing the command subscription is invisible from the outside
                # — the station simply appears to ignore everything — so it is
                # logged every time rather than once.
                log.warning("Command subscription to %s dropped: %s", topic, exc)
                self._stop.wait(self._backoff)
                self._backoff = min(BACKOFF_MAX, self._backoff * 2)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _deliver(self, message: dict, topic: str, handler: Handler) -> None:
        channel = message.get("channel")
        if isinstance(channel, bytes):
            channel = channel.decode()
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode()
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            log.warning("Dropping malformed message on %s.", channel)
            return
        if not isinstance(payload, dict):
            log.warning("Dropping non-object message on %s.", channel)
            return
        handler(channel or topic, payload)

    # --- connection -----------------------------------------------------

    def _resolve_tls_kwargs(self) -> dict:
        """The TLS settings, checked against the redis-py that is installed.

        `from_url` passes unknown keywords straight to the connection class, so
        an old redis-py fails with an unhelpful TypeError at the first publish —
        on a remote box, in the dark. Checking the signature here turns that
        into one clear line at start-up, and into a refusal rather than a
        connection made on weaker terms.
        """
        wanted = self.trust.redis_kwargs(self.url)
        if not wanted:
            return {}
        try:
            accepted = set(
                inspect.signature(redis.connection.SSLConnection.__init__).parameters
            )
        except (AttributeError, ValueError):  # pragma: no cover - defensive
            accepted = set(wanted)
        missing = [
            name for name in MANDATORY_TLS_KWARGS
            if name in wanted and name not in accepted
        ]
        if missing:
            raise Refusal(
                f"The installed redis-py ({redis.__version__}) does not accept "
                f"{', '.join(missing)}, so this station cannot prove it is "
                "talking to the right broker. Upgrade redis-py (>=5.0) rather "
                "than connecting without it."
            )
        dropped = [name for name in wanted if name not in accepted]
        for name in dropped:
            # Only ever an optional hardening extra — the mandatory three are
            # handled above — so say it and carry on rather than stopping.
            log.warning(
                "redis-py %s does not support %s; connecting without it.",
                redis.__version__, name,
            )
        return {name: value for name, value in wanted.items() if name in accepted}

    def _new_client(self) -> redis.Redis:
        kwargs = dict(
            socket_connect_timeout=CONNECT_TIMEOUT,
            socket_timeout=SOCKET_TIMEOUT,
            socket_keepalive=True,
            health_check_interval=15,
        )
        kwargs.update(self._tls_kwargs)
        if self._username:
            kwargs["username"] = self._username
        if self._password:
            kwargs["password"] = self._password
        client = redis.Redis.from_url(self.url, **kwargs)
        client.ping()
        return client

    def _ensure_client(self) -> redis.Redis | None:
        with self._lock:
            if self._client is not None:
                return self._client
            if time.monotonic() < self._next_attempt:
                return None
            try:
                self._client = self._new_client()
                return self._client
            except (redis.RedisError, OSError) as exc:
                self._fail(exc)
                return None

    #: Substrings that mean the handshake was refused rather than the network
    #: being down. The two need different words in front of a technician: one
    #: is weather, the other is a certificate nobody will notice otherwise.
    _TLS_MARKERS = ("certificate verify failed", "ssl", "tlsv1", "wrong version number")

    def _fail(self, exc: Exception) -> None:
        text = str(exc)
        self.tls_failed = any(marker in text.lower() for marker in self._TLS_MARKERS)
        if self._connected or self._last_error != text:
            if self.tls_failed:
                log.error(
                    "Refusing the broker at %s: its certificate did not verify "
                    "against the pinned CA (%s). Nothing is being published. "
                    "This station will not fall back to an unverified "
                    "connection — check the broker's certificate and the CA at "
                    "%s. Error: %s",
                    self.url, self.trust.describe(),
                    self.trust.path or "(none installed)", exc,
                )
            else:
                log.warning("Uplink down (%s): %s", self.url, exc)
        self._connected = False
        self._last_error = text
        self._close()
        self._next_attempt = time.monotonic() + self._backoff
        self._backoff = min(BACKOFF_MAX, self._backoff * 2)

    def _close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # --- state ----------------------------------------------------------

    @property
    def secure(self) -> bool:
        """Whether this link is encrypted and verified. Reported, not assumed."""
        return is_tls(self.url)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def refusals(self) -> dict[str, str]:
        return dict(self._refusals)

    @property
    def last_error(self) -> str | None:
        return self._last_error
