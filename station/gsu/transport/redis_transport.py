"""Redis pub/sub, which is the development broker.

Two things here are not incidental:

**It authenticates as `gsu:{station_id}` from the first connection.** Redis'
`default` user is still open on the development stack, so publishing without
credentials appears to work — and code that never authenticated stops working
everywhere at once the day `default` is closed. The credential also *is* the
station's identity at the broker; using it is the point of having it.

**It never queues.** A publish while the link is down returns False and the
frame is gone. Reconnection is attempted on a backoff by a thread that does not
hold up the sensing loop, so a station under an obstruction keeps sensing,
recording and alerting at full rate and simply stops talking.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import redis

from . import Handler, Transport

log = logging.getLogger("gsu.transport")

#: Reconnection backoff. Starlink obstructions are seconds to a minute, so the
#: first retries are quick; the cap keeps a long outage from hammering.
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0

#: Short timeouts: a publish that blocks is a tick that does not happen, and the
#: sensing loop must never wait on the network.
CONNECT_TIMEOUT = 3.0
SOCKET_TIMEOUT = 3.0


class RedisTransport(Transport):
    def __init__(self, url: str, username: str | None = None, password: str | None = None):
        self.url = url
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
            return True
        except (redis.RedisError, OSError) as exc:
            self._fail(exc)
            self._dropped += 1
            return False

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

    def _new_client(self) -> redis.Redis:
        kwargs = dict(
            socket_connect_timeout=CONNECT_TIMEOUT,
            socket_timeout=SOCKET_TIMEOUT,
            socket_keepalive=True,
            health_check_interval=15,
        )
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

    def _fail(self, exc: Exception) -> None:
        if self._connected or self._last_error != str(exc):
            log.warning("Uplink down (%s): %s", self.url, exc)
        self._connected = False
        self._last_error = str(exc)
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
    def connected(self) -> bool:
        return self._connected

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def last_error(self) -> str | None:
        return self._last_error
