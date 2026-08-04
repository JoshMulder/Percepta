"""What ADS-B does not carry: a tail number and a type.

The transponder broadcasts a 24-bit ICAO address, a callsign and a coarse
emitter category — never a registration or a model. Those come from a lookup
keyed by the address, which is what every FlightRadar-style label is underneath.

**The platform asks — not the station, not the browser.** Same rule as the
geocoder and the tile proxy: one egress point. A third party learns only that
*somebody* on this platform opened a card for this hex — never which operator,
never where the aircraft was, never when it was seen. And it is cached, so a
given airframe leaves here at most once: a registration does not change.

**A miss is silence.** Military airframes are often absent or blocked and new
tails lag the registries, so many contacts simply are not in any database. The
card renders those as unknown, which is what it did before this existed —
nothing here is load-bearing, it is a label.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

#: adsbdb: free, no key, keyed by the ICAO hex. A miss is a 404 with a body of
#: `{"response": "unknown aircraft"}`; a hit nests the record under
#: `response.aircraft`.
ENDPOINT = "https://api.adsbdb.com/v0/aircraft/"
USER_AGENT = "Percepta/1.0 (ground station console; +https://percepta.local)"
TIMEOUT_S = 6.0

#: A tail number is immutable for an airframe, so a hit is kept for a month; a
#: miss is kept only a day, because an aircraft absent today may be added
#: tomorrow. Either way a given hex leaves the platform at most once a day.
HIT_TTL_S = 30 * 24 * 3600
MISS_TTL_S = 24 * 3600

#: hex -> (expires_at monotonic, result-or-None). Per worker, which is enough:
#: the point is not to hammer adsbdb, and card-open traffic is a handful of
#: lookups a session. A shared cache would save a few first-hits across workers
#: and is not worth a Redis round trip on this path.
_cache: dict[str, tuple[float, dict | None]] = {}


def _is_hex(value: str) -> bool:
    return bool(value) and all(c in "0123456789abcdef" for c in value)


async def lookup(icao: str) -> dict | None:
    """Registration and type for an ICAO hex, or None if unknown or unreachable.

    Cached, so the same airframe is fetched at most once a day whatever a console
    does. None on anything that is not a clean hit: a bad hex, a miss, a timeout,
    a service that is down — all the same to the caller, which is a card that
    shows what it has and no more.
    """
    key = (icao or "").strip().lower()
    if not _is_hex(key):
        return None
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    result = await _fetch(key)
    _cache[key] = (now + (HIT_TTL_S if result else MISS_TTL_S), result)
    return result


async def _fetch(icao: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.get(
                ENDPOINT + icao, headers={"User-Agent": USER_AGENT},
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        aircraft = ((response.json() or {}).get("response") or {}).get("aircraft")
    except Exception as exc:  # noqa: BLE001 - a label is never worth an error
        log.info("Aircraft lookup failed for %s: %s", icao, exc)
        return None

    if not isinstance(aircraft, dict):
        return None
    registration = aircraft.get("registration")
    if not registration:
        # A record with no tail number is not worth showing over the category
        # the glyph already carries.
        return None
    return {
        "icao": icao,
        "registration": registration,
        # `icao_type` is the code (B738); `type` is the readable model. Both, so
        # the console can show the model and fall back to the code.
        "type_code": aircraft.get("icao_type") or None,
        "model": aircraft.get("type") or None,
        "manufacturer": aircraft.get("manufacturer") or None,
        "operator": aircraft.get("registered_owner") or None,
    }
