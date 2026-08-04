"""The registration/type proxy over adsbdb.

Driven through the endpoint rather than the async service directly, so the test
needs no async runner — the TestClient awaits it — and exercises the same path a
console does. The upstream is always mocked: a suite that reached the real
adsbdb would be slow, flaky, and rude to a free community service.
"""

from __future__ import annotations

import httpx
import pytest

from backend.services import aircraft_lookup

#: One adsbdb hit, in its own shape: the record nests under response.aircraft,
#: with the code in `icao_type` and the readable model in `type`.
SAMPLE = {
    "response": {
        "aircraft": {
            "type": "Boeing 737-838",
            "icao_type": "B738",
            "manufacturer": "Boeing",
            "registration": "VH-VYE",
            "registered_owner": "Qantas",
        }
    }
}


@pytest.fixture(autouse=True)
def _clear_cache():
    # The cache is module-global and would otherwise carry a hit from one test
    # into the next, so the second test's "one network call" assertion would see
    # zero. Cleared around every test.
    aircraft_lookup._cache.clear()
    yield
    aircraft_lookup._cache.clear()


def _patch_upstream(monkeypatch, *, payload=None, status=200, exc=None):
    """Replace adsbdb with a fake, and return the list of URLs it was asked for."""
    calls: list[str] = []

    class _Response:
        status_code = status

        def json(self):
            return payload or {}

        def raise_for_status(self):
            if self.status_code >= 400 and self.status_code != 404:
                raise httpx.HTTPStatusError("upstream error", request=None, response=None)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            calls.append(url)
            if exc is not None:
                raise exc
            return _Response()

    monkeypatch.setattr(aircraft_lookup.httpx, "AsyncClient", _Client)
    return calls


def test_looks_up_the_registration_and_type(client, monkeypatch):
    calls = _patch_upstream(monkeypatch, payload=SAMPLE)
    body = client.get("/api/aircraft/7c6db8").json()
    assert body["registration"] == "VH-VYE"
    assert body["type_code"] == "B738"
    assert body["model"] == "Boeing 737-838"
    assert body["operator"] == "Qantas"
    assert len(calls) == 1


def test_serves_a_repeat_from_cache(client, monkeypatch):
    calls = _patch_upstream(monkeypatch, payload=SAMPLE)
    client.get("/api/aircraft/7c6db8")
    client.get("/api/aircraft/7c6db8")
    assert len(calls) == 1, "the second lookup went back to the network"


def test_an_unknown_aircraft_is_a_shell_not_an_error(client, monkeypatch):
    # adsbdb answers a miss with 404. The card renders the absence, so this is a
    # 200 carrying just the hex, not an error the console has to handle.
    _patch_upstream(monkeypatch, status=404, payload={"response": "unknown aircraft"})
    response = client.get("/api/aircraft/abcdef")
    assert response.status_code == 200
    body = response.json()
    assert body["icao"] == "abcdef"
    assert body["registration"] is None


def test_an_upstream_failure_degrades_to_a_shell(client, monkeypatch):
    # The service being down must not take the contact card down with it.
    _patch_upstream(monkeypatch, exc=httpx.ConnectTimeout("adsbdb unreachable"))
    body = client.get("/api/aircraft/abcdef").json()
    assert body["registration"] is None
    assert body["model"] is None


def test_a_bad_hex_never_touches_the_network(client, monkeypatch):
    calls = _patch_upstream(monkeypatch, payload=SAMPLE)
    body = client.get("/api/aircraft/nothex").json()
    assert body["registration"] is None
    assert calls == [], "a non-hex id was sent upstream"


def test_a_record_without_a_tail_number_is_treated_as_unknown(client, monkeypatch):
    # A registry row that carries a type but no registration is not worth showing
    # over the category the glyph already has.
    _patch_upstream(
        monkeypatch,
        payload={"response": {"aircraft": {"icao_type": "B738", "registration": ""}}},
    )
    body = client.get("/api/aircraft/7c6db8").json()
    assert body["registration"] is None
    assert body["type_code"] is None
