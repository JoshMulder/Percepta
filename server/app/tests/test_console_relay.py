"""The console relay's request/response multiplexing and the reply adaptation,
driven without a station or a browser.

Two things this proves. First, that a request sent down a station's socket is
resolved by exactly the response frame that carries its id, that a dropped socket
fails the requests waiting on it rather than hanging them, and that a second
socket supersedes the first (`realtime/console.py`). Second, that a reply from a
box's own console — written to be served at `http://<box>:8088/` — is adapted so
it works framed under `/api/platform/…/console/` (`api/console._adapt_response`):
its links rewritten, its frame-blocking headers relaxed, and nothing else touched
— device paths like `/dev/ttyUSB0` in particular must survive intact.

Plain coroutines run to completion, like `test_media_relay.py`: there is no
database in this path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid

import pytest

from backend.api.console import (
    _adapt_response,
    _console_cookie,
    _handle_response_frame,
    _rewrite_location,
)
from backend.realtime.console import (
    ConsoleError,
    ConsoleRelay,
    ConsoleResponse,
)


def _make_link(relay: ConsoleRelay, sent: list[str]):
    async def send(frame: str) -> None:
        sent.append(frame)

    return relay.station_connected(uuid.uuid4(), uuid.uuid4(), send)


async def _wait_sent(sent: list[str]) -> dict:
    """Yield until the request task has put its frame on the wire."""
    while not sent:
        await asyncio.sleep(0)
    return json.loads(sent[-1])


# --- the multiplexing ----------------------------------------------------


def test_a_request_is_resolved_by_the_frame_that_echoes_its_id() -> None:
    async def scenario() -> ConsoleResponse:
        relay = ConsoleRelay()
        sent: list[str] = []
        link = _make_link(relay, sent)
        pending = asyncio.create_task(
            link.request("GET", "/connection", {"cookie": "x"}, b"")
        )
        frame = await _wait_sent(sent)
        assert frame["t"] == "req"
        assert frame["method"] == "GET" and frame["path"] == "/connection"
        # The station answers, on the ingest endpoint's path.
        _handle_response_frame(
            link,
            link.station_id,
            json.dumps({
                "t": "resp",
                "id": frame["id"],
                "status": 200,
                "headers": {"Content-Type": "text/html"},
                "body_b64": base64.b64encode(b"<html>").decode(),
            }),
        )
        return await pending

    response = asyncio.run(scenario())
    assert response.status == 200 and response.body == b"<html>"


def test_a_request_carries_its_body_down_base64() -> None:
    async def scenario() -> bytes:
        relay = ConsoleRelay()
        sent: list[str] = []
        link = _make_link(relay, sent)
        pending = asyncio.create_task(
            link.request("POST", "/enrol", {}, b"token=ABCD")
        )
        frame = await _wait_sent(sent)
        _handle_response_frame(
            link, link.station_id,
            json.dumps({"t": "resp", "id": frame["id"], "status": 303,
                        "headers": {}, "body_b64": ""}),
        )
        await pending
        return base64.b64decode(frame["body_b64"])

    assert asyncio.run(scenario()) == b"token=ABCD"


def test_an_error_frame_raises_rather_than_returning_a_page() -> None:
    async def scenario() -> None:
        relay = ConsoleRelay()
        sent: list[str] = []
        link = _make_link(relay, sent)
        pending = asyncio.create_task(link.request("GET", "/", {}, b""))
        frame = await _wait_sent(sent)
        _handle_response_frame(
            link, link.station_id,
            json.dumps({"t": "err", "id": frame["id"], "error": "boom"}),
        )
        with pytest.raises(ConsoleError):
            await pending

    asyncio.run(scenario())


def test_a_dropped_socket_fails_the_requests_waiting_on_it() -> None:
    async def scenario() -> None:
        relay = ConsoleRelay()
        sent: list[str] = []
        link = _make_link(relay, sent)
        pending = asyncio.create_task(link.request("GET", "/", {}, b""))
        await _wait_sent(sent)
        # The station's socket closes with a request still in flight.
        relay.station_gone(link.station_id, link)
        with pytest.raises(ConsoleError):
            await pending

    asyncio.run(scenario())


def test_a_second_socket_supersedes_the_first() -> None:
    async def scenario() -> None:
        relay = ConsoleRelay()
        sent: list[str] = []
        first = _make_link(relay, sent)
        pending = asyncio.create_task(first.request("GET", "/", {}, b""))
        await _wait_sent(sent)
        # The station reconnects; the old link's in-flight request is broken
        # rather than left to answer on a socket the relay has forgotten.
        second = relay.station_connected(first.station_id, first.organization_id,
                                         lambda _f: None)
        assert relay.get(first.station_id) is second
        with pytest.raises(ConsoleError):
            await pending

    asyncio.run(scenario())


def test_a_request_times_out_when_the_station_never_answers() -> None:
    async def scenario() -> None:
        relay = ConsoleRelay()
        link = _make_link(relay, [])
        with pytest.raises(ConsoleError):
            await link.request("GET", "/", {}, b"", timeout=0.05)

    asyncio.run(scenario())


def test_wait_for_returns_the_link_the_instant_the_station_connects() -> None:
    async def scenario() -> bool:
        relay = ConsoleRelay()
        station_id = uuid.uuid4()
        waiter = asyncio.create_task(relay.wait_for(station_id, timeout=1.0))
        await asyncio.sleep(0)  # let the waiter register
        link = relay.station_connected(station_id, uuid.uuid4(), lambda _f: None)
        return (await waiter) is link

    assert asyncio.run(scenario()) is True


def test_wait_for_times_out_when_the_station_does_not_open() -> None:
    async def scenario():
        return await ConsoleRelay().wait_for(uuid.uuid4(), timeout=0.05)

    assert asyncio.run(scenario()) is None


# --- adapting the reply for the frame ------------------------------------

_BASE = "/api/platform/stations/abc/console"


def _html_reply(body: bytes, csp: str) -> ConsoleResponse:
    return ConsoleResponse(
        status=200,
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": csp,
            "Set-Cookie": "gsu_setup=abc; Path=/; HttpOnly; SameSite=Strict",
            "Content-Length": str(len(body)),
        },
        body=body,
    )


def test_root_relative_links_forms_and_assets_are_rewritten_onto_the_base() -> None:
    body = (
        b'<a href="/connection">c</a>'
        b'<form action="/enrol"></form>'
        b'<img src="/frame.jpg">'
        b'<script>fetch("/status.json");still.src="/frame.jpg?t="+n;</script>'
    )
    out = _adapt_response(_html_reply(body, "default-src 'none'"), _BASE)
    assert (_BASE + "/connection").encode() in out.body
    assert (_BASE + "/enrol").encode() in out.body
    assert (_BASE + "/frame.jpg").encode() in out.body
    assert (_BASE + "/status.json").encode() in out.body
    # The script literal's query survives the rewrite.
    assert (_BASE + '/frame.jpg?t="').encode() in out.body


def test_device_paths_and_input_values_are_left_alone() -> None:
    # The Devices tab renders /dev/... paths as text and in value="…" inputs. A
    # blanket "rewrite every quoted /…" would corrupt them; the attribute
    # allowlist is exactly what stops that.
    body = b'<input value="/dev/ttyUSB0"><code>/dev/serial/by-id/x</code>'
    out = _adapt_response(_html_reply(body, "default-src 'none'"), _BASE)
    assert b'value="/dev/ttyUSB0"' in out.body
    assert b"/dev/serial/by-id/x" in out.body
    assert _BASE.encode() not in out.body


def test_the_frame_blocking_headers_are_relaxed_for_the_admin_frame() -> None:
    out = _adapt_response(
        _html_reply(b"<html>", "default-src 'none'; frame-ancestors 'none'"), _BASE
    )
    assert "x-frame-options" not in out.headers
    assert "frame-ancestors 'self'" in out.headers["content-security-policy"]
    assert "frame-ancestors 'none'" not in out.headers["content-security-policy"]
    # The rest of the console's CSP is untouched.
    assert "default-src 'none'" in out.headers["content-security-policy"]


def test_the_cookie_is_scoped_to_the_console_and_the_length_is_recomputed() -> None:
    out = _adapt_response(_html_reply(b"<html>", "default-src 'none'"), _BASE)
    assert f"Path={_BASE}" in out.headers["set-cookie"]
    assert "Path=/;" not in out.headers["set-cookie"]
    # We drop the station's Content-Length and let the response recompute it, so
    # a rewritten (longer) body is not described by a stale length.
    assert out.headers["content-length"] == str(len(out.body))


def test_a_non_html_reply_is_passed_through_unrewritten() -> None:
    body = b'{"slots":{"radio":{"resource":"/dev/ttyUSB0"}}}'
    reply = ConsoleResponse(200, {"Content-Type": "application/json"}, body)
    out = _adapt_response(reply, _BASE)
    assert out.body == body  # a JSON body is data, not markup, and stays verbatim


def test_a_redirect_location_is_re_pathed_onto_the_base() -> None:
    assert _rewrite_location("/connection#location", _BASE) == _BASE + "/connection#location"
    # Absolute and protocol-relative targets are left alone.
    assert _rewrite_location("https://elsewhere/x", _BASE) == "https://elsewhere/x"
    assert _rewrite_location("//host/x", _BASE) == "//host/x"


def test_only_the_console_cookie_is_forwarded_to_the_box() -> None:
    # The platform session cookie rides the same header on these requests, and a
    # field box — which could be compromised — must never see it: that token
    # would let it become a platform admin. Only the console's own cookie goes.
    header = "session=PLATFORM-ADMIN-TOKEN; gsu_setup=abc123; theme=dark"
    assert _console_cookie(header) == "gsu_setup=abc123"
    assert _console_cookie("session=only-the-platform-token") == ""
