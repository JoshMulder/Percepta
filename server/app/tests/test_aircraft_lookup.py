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


#: A one-row slice of the real NZ CAA register export, columns verbatim. The
#: quoted owner address carries a comma on purpose, so the test exercises the CSV
#: parser rather than a split that would happen to work on unquoted rows.
CAA_CSV = (
    "Model Category,Registration Mark,Registered on,Manufacturer,Model,"
    "Serial No.,MCTOW (Kg),Owner Name,Owner Address,Mode S Code HEX,"
    "Mode S Code Binary,Flight manual no.\r\n"
    "Aeroplane (Aircraft),ZK-AAC,01/06/2011,Cessna Aircraft Company,162,"
    '16200060,598,RPM White Limited,"PO Box 2240, TAUPO 3351, New Zealand",'
    "C81E56,110010000001111001010110,3180\r\n"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    # The caches are module-global and would otherwise carry a hit from one test
    # into the next, so the second test's "one network call" assertion would see
    # zero. Both the per-hex cache and the NZ register index are cleared around
    # every test.
    aircraft_lookup._cache.clear()
    aircraft_lookup._caa_index = None
    aircraft_lookup._caa_expires = 0.0
    yield
    aircraft_lookup._cache.clear()
    aircraft_lookup._caa_index = None
    aircraft_lookup._caa_expires = 0.0


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


# --- New Zealand gap-fill -----------------------------------------------------
#
# When adsbdb has nothing for an NZ airframe, the lookup falls through to the
# CAA's register CSV. These drive both upstreams: adsbdb answers a miss, and the
# register is served (or not) depending on the hex.


def _patch_sources(
    monkeypatch,
    *,
    adsbdb_status=404,
    adsbdb_payload=None,
    caa_body=CAA_CSV,
    caa_status=200,
    caa_exc=None,
):
    """Fake both upstreams, dispatching by URL, and return the URLs asked for."""
    calls: list[str] = []

    class _Response:
        def __init__(self, status, *, payload=None, content=b""):
            self.status_code = status
            self._payload = payload or {}
            self.content = content

        def json(self):
            return self._payload

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
            if url.startswith(aircraft_lookup.CAA_REGISTER_URL):
                if caa_exc is not None:
                    raise caa_exc
                return _Response(caa_status, content=caa_body.encode("utf-8"))
            return _Response(adsbdb_status, payload=adsbdb_payload)

    monkeypatch.setattr(aircraft_lookup.httpx, "AsyncClient", _Client)
    return calls


def test_fills_an_nz_gap_from_the_caa_register(client, monkeypatch):
    # adsbdb misses this NZ hex; the register carries it, so the card gets a tail
    # and a readable model instead of the bare emitter category.
    calls = _patch_sources(monkeypatch)
    body = client.get("/api/aircraft/c81e56").json()
    assert body["registration"] == "ZK-AAC"
    assert body["model"] == "Cessna Aircraft Company 162"
    assert body["operator"] == "RPM White Limited"
    # The register has no ICAO type designator; the model stands in for it.
    assert body["type_code"] is None
    assert any("adsbdb" in url for url in calls), "adsbdb was not tried first"
    assert any(url.startswith(aircraft_lookup.CAA_REGISTER_URL) for url in calls)


def test_a_foreign_miss_never_fetches_the_register(client, monkeypatch):
    # 7c6db8 is in the Australian block, so the NZ register cannot hold it and is
    # never downloaded — the gap-fill is New Zealand's alone.
    calls = _patch_sources(monkeypatch)
    body = client.get("/api/aircraft/7c6db8").json()
    assert body["registration"] is None
    assert not any(url.startswith(aircraft_lookup.CAA_REGISTER_URL) for url in calls)


def test_the_register_is_fetched_once_for_many_nz_hexes(client, monkeypatch):
    # A megabyte of register is downloaded once and answers every NZ hex from the
    # in-memory index, hit or miss.
    calls = _patch_sources(monkeypatch)
    client.get("/api/aircraft/c81e56")  # in the register
    client.get("/api/aircraft/c80999")  # NZ block, not in this slice
    fetches = [u for u in calls if u.startswith(aircraft_lookup.CAA_REGISTER_URL)]
    assert len(fetches) == 1, "the register was downloaded more than once"


def test_adsbdb_wins_over_the_register(client, monkeypatch):
    # An NZ hex adsbdb *does* know is answered from adsbdb, and the register is
    # never consulted — so its download does not happen at all.
    calls = _patch_sources(monkeypatch, adsbdb_status=200, adsbdb_payload=SAMPLE)
    body = client.get("/api/aircraft/c81e56").json()
    assert body["registration"] == "VH-VYE"
    assert not any(url.startswith(aircraft_lookup.CAA_REGISTER_URL) for url in calls)


def test_a_register_that_is_down_degrades_to_a_shell(client, monkeypatch):
    # adsbdb missed and the register is unreachable: the card shows the hex and
    # no more, exactly as when adsbdb alone is down.
    _patch_sources(monkeypatch, caa_exc=httpx.ConnectTimeout("caa unreachable"))
    body = client.get("/api/aircraft/c81e56").json()
    assert body["registration"] is None
