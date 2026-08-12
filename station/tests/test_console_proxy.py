"""The station's console proxy: opt-in, loopback re-origination, and framing.

The three properties that make this safe to have on a box in a field, each with
a test so it cannot quietly regress:

  * **opt-in** — a station without `GSU_CONSOLE_PROXY` refuses to open the socket
    at all, and there is no path from a `console.open` to a connection on it.
  * **terminate and re-originate** — a request becomes a fresh loopback request
    with `Host: 127.0.0.1` and the admin's `Origin` dropped, which is exactly
    what `console.py`'s host check and same-origin guard need.
  * **nothing streams through it** — an oversized body is refused rather than
    poured through a request/response tunnel it does not belong in.
"""

from __future__ import annotations

import base64
import http.server
import threading

import pytest

import gsu.transport.console_proxy as cp
from gsu.commands import build_handlers
from gsu.transport.console_proxy import ConsoleProxy, console_ingest_url


class _Cfg:
    def __init__(self, console_url=None, platform_url="https://app.percepta.nz"):
        self.console_url = console_url
        self.platform_url = platform_url


def test_the_ingest_url_is_derived_from_the_platform_host() -> None:
    assert console_ingest_url(_Cfg()) == "wss://app.percepta.nz/console/ingest"
    # http derives ws, for a development stack.
    assert (
        console_ingest_url(_Cfg(platform_url="http://localhost:8000"))
        == "ws://localhost:8000/console/ingest"
    )
    # An explicit override wins, like GSU_MEDIA_URL does.
    assert console_ingest_url(_Cfg(console_url="wss://via/console")) == "wss://via/console"


def test_an_opted_out_station_refuses_and_never_wants_the_socket() -> None:
    proxy = ConsoleProxy("wss://x/console/ingest", "secret", enabled=False)
    effect = proxy.open(300)
    assert effect.startswith("refused")
    # The whole safety property: no deadline was set, so the manager thread has
    # nothing to connect to. Starting it changes nothing.
    assert proxy._wanted() is False


def test_an_opted_in_station_opens_a_bounded_window() -> None:
    proxy = ConsoleProxy("wss://x/console/ingest", "secret", enabled=True)
    effect = proxy.open(120)
    assert "120s" in effect
    assert proxy._wanted() is True
    proxy.close()
    assert proxy._wanted() is False


def test_console_handlers_are_registered_only_with_a_proxy() -> None:
    assert "console.open" not in build_handlers(None, None, None)
    proxy = ConsoleProxy(None, None, enabled=False)
    handlers = build_handlers(None, None, None, console_proxy=proxy)
    assert "console.open" in handlers and "console.close" in handlers
    # The command falls through to a refusal on an opted-out box rather than
    # opening anything.
    assert handlers["console.open"]({"lease_seconds": 10}).startswith("refused")


# --- loopback re-origination ---------------------------------------------


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    seen: dict = {}
    reply: bytes = b"<a href='/connection'>x</a>"

    def do_GET(self):  # noqa: N802
        _CapturingHandler.seen = {
            "host": self.headers.get("Host"),
            "origin": self.headers.get("Origin"),
            "cookie": self.headers.get("Cookie"),
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.reply)))
        self.end_headers()
        self.wfile.write(self.reply)

    def log_message(self, *args):  # noqa: A002
        pass


@pytest.fixture()
def loopback_console():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def test_a_request_is_re_originated_against_loopback(loopback_console) -> None:
    proxy = ConsoleProxy(None, None, enabled=True, loopback_port=loopback_console)
    status, headers, body = proxy._loopback(
        "GET", "/summary",
        {"origin": "https://evil.example", "cookie": "gsu_setup=z"},
        b"",
    )
    assert status == 200
    # Host is the IP literal the console's DNS-rebinding guard demands...
    assert _CapturingHandler.seen["host"] == "127.0.0.1"
    # ...the admin's Origin was dropped, so the console's same-origin guard sees
    # a non-browser caller and judges by the loopback peer and the CSRF token...
    assert _CapturingHandler.seen["origin"] is None
    # ...and the console's own session cookie is forwarded, so a rendered form's
    # CSRF token still matches on the POST that follows.
    assert _CapturingHandler.seen["cookie"] == "gsu_setup=z"
    assert b"href='/connection'" in body


def test_serve_produces_a_response_frame_with_the_request_id(loopback_console) -> None:
    proxy = ConsoleProxy(None, None, enabled=True, loopback_port=loopback_console)
    sent: list[dict] = []
    proxy._send = sent.append  # type: ignore[assignment]
    proxy._serve({
        "t": "req", "id": 7, "method": "GET", "path": "/summary",
        "headers": {}, "body_b64": base64.b64encode(b"").decode(),
    })
    assert len(sent) == 1
    frame = sent[0]
    assert frame["t"] == "resp" and frame["id"] == 7 and frame["status"] == 200
    assert base64.b64decode(frame["body_b64"]).startswith(b"<a href")


def test_an_oversized_body_is_refused(loopback_console, monkeypatch) -> None:
    # The endless bodies (/stream.mp4, /audio.wav) belong on /media, not here; a
    # body over the cap is an error rather than a socket that pours forever.
    monkeypatch.setattr(cp, "MAX_BODY_BYTES", 4)
    proxy = ConsoleProxy(None, None, enabled=True, loopback_port=loopback_console)
    with pytest.raises(ValueError):
        proxy._loopback("GET", "/summary", {}, b"")
