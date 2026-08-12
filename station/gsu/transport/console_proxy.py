"""The station's own setup console, reached back down its outbound link.

A field station is behind Starlink CGNAT: nothing dials *in*. The full setup
console — enrolment, the per-slot device inventory, radio, location,
transcription, factory reset, logs — is the HTTP server `gsu/console.py` runs on
`127.0.0.1:8088`, and from outside the box it is unreachable. This is how a
platform admin reaches it anyway: on a `console.open` command the station opens a
WebSocket *outward* to the platform's `/console/ingest`, receives the admin's
HTTP requests as frames, replays each one as a **loopback** request to its own
console, and sends the response back.

WHY THE AGENT PROXIES ITS OWN CONSOLE — NO PRIVILEGED HELPER
------------------------------------------------------------
The setup console is served by *this* process, on loopback, and
`gsu/setup_access.py` makes loopback callers auth-exempt (they arrived over SSH,
already authenticated — a second password in front of the first protects
nothing). So the agent making a request to `http://127.0.0.1:8088` from inside
the box is already an authenticated caller; there is nothing privileged to add
and no helper container to run. The whole feature is a request/response HTTP
tunnel over the socket the station already knows how to open.

THREE SAFEGUARDS, FROM LINE ONE
-------------------------------
**Opt-in, off by default.** A station that has not set `GSU_CONSOLE_PROXY`
refuses to open the socket at all — the same refuse-to-bind posture
`setup_access.py` takes about the LAN listener. Reaching into a box remotely is a
trust escalation, so it is a deliberate per-box choice, not a default.

**Time-boxed.** The socket is not a standing fixture. Each `console.open` asks
for a bounded lease, activity extends it, and when the admin stops using the page
the window lapses and the station closes the socket itself — the `Gate` window in
`setup_access.py`, expressed for this socket.

**Nothing streams through it.** The tunnel is request/response for pages, JSON
and small cached images. The console's endless bodies — `/stream.mp4`,
`/audio.wav` — have their own path (`/media`) and would never fit here, so the
loopback read is capped and an oversized body becomes an error rather than a
socket that pours forever.

TERMINATE AND RE-ORIGINATE
--------------------------
Each request is made fresh against loopback with `Host: 127.0.0.1`, which is what
`console.py`'s host check and DNS-rebinding guard want, and the browser's own
`Origin`/`Host`/`Cookie` are handled at the platform boundary (`api/console.py`)
so the console's same-origin guard is not tripped. The station never sees the
admin's address and the admin never sees the station's.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..media.websocket import WebSocket

log = logging.getLogger("gsu.console_proxy")

#: The path the platform serves the ingest socket on. Beside the URL derivation
#: rather than buried in it, because it is the platform's to change — the same
#: shape `stream.py` keeps `INGEST_PATH` in.
INGEST_PATH = "/console/ingest"

#: How long a loopback request may take, and how large its body may be. A setup
#: page is local state read off the box's own disk, so it is quick and small; a
#: cold camera preview is the slow outlier and a factory reset the one with any
#: heft. The body cap is below the socket frame cap once base64 inflation (4/3)
#: and JSON overhead are accounted for, so a reply that fits here fits the frame.
LOOPBACK_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 6 * 1024 * 1024

#: How often the manager thread reconsiders whether the socket should be up.
MANAGE_TICK_S = 1.0

#: The least time between connect attempts, so an unreachable platform is not
#: hammered while a lease is live. The socket is opened once and used for a
#: session, not reopened per request, so this only bounds recovery after a drop.
REOPEN_INTERVAL_S = 5.0

#: How many loopback requests may be served at once. The console is a handful of
#: assets per page and a `status.json` poll; a small pool keeps the socket's
#: reader thread free (it only dispatches) without letting a burst fork without
#: bound.
MAX_INFLIGHT = 4


def console_ingest_url(config, enrolment=None) -> str | None:
    """Where the platform's console ingest is.

    `GSU_CONSOLE_URL` wins, for the same reason `GSU_MEDIA_URL` and
    `GSU_BROKER_URL` exist: the address a platform states may only be routable
    from inside its own network. Otherwise it is derived from the platform API's
    address — the same host that serves `/media/ingest` and `/broker` — with the
    scheme switched to WebSocket and this module's path appended.
    """
    override = getattr(config, "console_url", None)
    if override:
        return override
    api = getattr(config, "platform_url", "") or ""
    scheme, separator, rest = api.partition("://")
    if not separator or not rest:
        return None
    ws = "wss" if scheme.lower() == "https" else "ws"
    return f"{ws}://{rest.rstrip('/')}{INGEST_PATH}"


class ConsoleProxy:
    """Opens the console socket on demand, serves loopback requests over it.

    One manager thread owns the socket's existence — connecting when a lease is
    live and closing when it lapses — so a slow connect never runs on the relay's
    reader thread or the sensing loop. Requests arrive on the socket's own reader
    thread and are dispatched to a small pool, so serving one never blocks the
    next, or the pings that keep the socket alive.
    """

    def __init__(
        self,
        url: str | None,
        secret: str | None,
        trust=None,
        *,
        enabled: bool = False,
        loopback_host: str = "127.0.0.1",
        loopback_port: int = 8088,
        lease_seconds: float = 300.0,
    ) -> None:
        self.url = url
        self.secret = secret
        self.trust = trust
        self.enabled = enabled
        self.loopback_host = loopback_host
        self.loopback_port = loopback_port
        self.lease_seconds = lease_seconds

        self._socket: WebSocket | None = None
        self._lock = threading.Lock()
        self._deadline = 0.0
        self._last_open_attempt = float("-inf")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(
            max_workers=MAX_INFLIGHT, thread_name_prefix="gsu-console"
        )

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._manage, name="gsu-console-proxy", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
        self._close()
        self._pool.shutdown(wait=False)

    # --- the two commands ------------------------------------------------

    def open(self, lease_seconds: float | None = None) -> str:
        """`console.open`: keep the console socket up for a bounded window.

        Refused unless the box has opted in, which is the whole safety property:
        there is no path from a `console.open` to an open socket on a station that
        has not set `GSU_CONSOLE_PROXY`, so the default is a box that cannot be
        reached this way rather than one that can.

        Idempotent while a session is live: a second `open` extends the window
        rather than opening a second socket. Activity extends it too, so an admin
        using the page keeps it alive without the platform having to renew.
        """
        if not self.enabled:
            log.warning(
                "Refusing console.open: remote console access is not enabled on "
                "this station (set GSU_CONSOLE_PROXY to allow it)."
            )
            return "refused: remote console not enabled"
        if not self.url or not self.secret:
            return "refused: no platform console URL or credential"
        lease = self.lease_seconds if lease_seconds is None else float(lease_seconds)
        self._extend(lease)
        self._wake.set()
        return f"console window open for {int(lease)}s"

    def close(self, reason: str = "closed by the platform") -> str:
        """`console.close`: drop the window now rather than waiting it out."""
        with self._lock:
            self._deadline = 0.0
        self._wake.set()
        return reason

    def _extend(self, seconds: float) -> None:
        with self._lock:
            self._deadline = max(self._deadline, time.monotonic() + seconds)

    def _wanted(self) -> bool:
        with self._lock:
            return time.monotonic() < self._deadline

    # --- the manager thread ----------------------------------------------

    def _manage(self) -> None:
        """Own the socket's existence: connect while wanted, close when not."""
        while not self._stop.is_set():
            try:
                if self._wanted():
                    self._ensure_open()
                elif self._socket is not None:
                    log.info("Console window lapsed; closing the socket.")
                    self._close()
            except Exception:  # noqa: BLE001 - a manager that dies is silent
                log.exception("Console proxy manager tick failed; continuing.")
            # Woken early by open()/close(); otherwise a slow poll, since the
            # thing it watches (a deadline) moves in whole seconds.
            self._wake.wait(MANAGE_TICK_S)
            self._wake.clear()
        self._close()

    def _ensure_open(self) -> None:
        socket = self._socket
        if socket is not None and socket.connected:
            return
        now = time.monotonic()
        if now - self._last_open_attempt < REOPEN_INTERVAL_S:
            return
        self._last_open_attempt = now
        socket = WebSocket(
            self.url,
            headers={"Authorization": f"Bearer {self.secret}",
                     "User-Agent": "percepta-gsu"},
            trust=self.trust,
            on_message=self._on_message,
            what="the console proxy",
        )
        try:
            socket.connect()
        except Exception as exc:  # noqa: BLE001 - incl. tls.Refusal (a RuntimeError)
            # Reported, never raised upward — the manager retries on the next
            # tick, rate-limited by REOPEN_INTERVAL_S above. A trust refusal
            # (plaintext, or nothing to verify against) surfaces here too, so a
            # misconfigured URL is a logged line, not a dead thread.
            log.warning("Console proxy could not open: %s", exc)
            return
        self._socket = socket
        log.info("Console proxy open to %s.", self.url)

    def _close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            socket.close("console window closed")

    # --- serving requests ------------------------------------------------

    def _on_message(self, _opcode: int, payload: bytes) -> None:
        """A request frame from the platform, on the socket's reader thread.

        Only dispatches — the loopback request is served on the pool, so a slow
        one never stalls the reader thread that answers pings and reads the next
        frame. Activity extends the window, so a page in active use stays open.
        """
        try:
            frame = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(frame, dict) or frame.get("t") != "req":
            return
        self._extend(self.lease_seconds)
        try:
            self._pool.submit(self._serve, frame)
        except RuntimeError:
            # The pool is shutting down; the socket is on its way out too.
            pass

    def _serve(self, frame: dict) -> None:
        request_id = frame.get("id")
        try:
            status, headers, body = self._loopback(
                str(frame.get("method") or "GET"),
                str(frame.get("path") or "/"),
                frame.get("headers") if isinstance(frame.get("headers"), dict) else {},
                base64.b64decode(frame.get("body_b64") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - reported to the admin as a 502
            self._send({"t": "err", "id": request_id, "error": str(exc)[:200]})
            return
        self._send({
            "t": "resp",
            "id": request_id,
            "status": status,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
        })

    def _loopback(
        self, method: str, path: str, headers: dict, body: bytes
    ) -> tuple[int, dict, bytes]:
        """Replay one request against the box's own console, on loopback.

        `Host: 127.0.0.1` is set deliberately: `console.py` requires an IP
        literal, `localhost` or a `.local` name, which is what stops a rebound
        public name driving this form. The admin's `Origin` was already dropped
        at the platform boundary, so the console judges this by its loopback peer
        (auth-exempt) and the CSRF token in the form, exactly as it does for a
        request typed at the box.
        """
        forwarded = {
            name: value for name, value in headers.items()
            if name.lower() in ("cookie", "content-type")
        }
        request_headers = {"Host": "127.0.0.1", **forwarded}
        if body:
            request_headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection(
            self.loopback_host, self.loopback_port, timeout=LOOPBACK_TIMEOUT_S
        )
        try:
            conn.request(method, path, body=body or None, headers=request_headers)
            response = conn.getresponse()
            # One byte over the cap is enough to know it is over — this is where
            # an endless body (/stream.mp4, /audio.wav) is refused rather than
            # poured through a request/response tunnel it does not belong in.
            data = response.read(MAX_BODY_BYTES + 1)
            if len(data) > MAX_BODY_BYTES:
                raise ValueError(
                    "the response body is too large for the console tunnel "
                    "(streaming endpoints are served over /media, not here)"
                )
            out_headers = {name: value for name, value in response.getheaders()}
            return response.status, out_headers, data
        finally:
            conn.close()

    def _send(self, frame: dict) -> None:
        socket = self._socket
        if socket is None or not socket.connected:
            return
        socket.send_text(json.dumps(frame, separators=(",", ":")))
