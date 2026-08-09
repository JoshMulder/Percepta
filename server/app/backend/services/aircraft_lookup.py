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

**New Zealand fills its own gap.** adsbdb is thin on the local light fleet — a
microlight or a Cub is often a bare category with no tail — so a miss in the NZ
Mode S block falls through to the CAA's public register, which lists the whole
ZK- fleet with the Mode S hex beside the registration. That hex is the one join
that makes it work: the register is *searchable* by tail, but the downloadable
file *carries* the hex, so the platform can go the direction ADS-B needs. It is
fetched whole, once, and cached like everything else here — and only ever for a
hex in New Zealand's block, so a foreign miss never touches it.
"""

from __future__ import annotations

import asyncio
import csv
import io
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
    if result is None:
        # adsbdb has nothing; for an NZ airframe the national register might.
        # Only NZ hexes reach the register, and only on a miss, so the common
        # path is exactly as it was.
        result = await _caa_lookup(key)
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


# --- New Zealand gap-fill -----------------------------------------------------
#
# The CAA publishes the whole ZK- fleet as a single CSV that lists the Mode S
# hex beside each registration. That is what makes this possible at all: the
# register's own search takes a tail and gives a hex, the wrong direction for a
# console holding a hex off the air — but the *file* has both columns, so a
# one-time fetch is enough to turn it around into the hex -> tail lookup adsbdb
# is missing for the local light fleet.

CAA_REGISTER_URL = (
    "https://www.aviation.govt.nz/assets/aircraft/aircraft-register/"
    "Aircraft-Register-for-website-.csv"
)
#: New Zealand's ICAO 24-bit block. Everything in the register falls inside it
#: and nothing outside it can be in the register, so this one range is both the
#: gate on consulting the index and the gate on ever building it.
NZ_ICAO_LOW = 0xC80000
NZ_ICAO_HIGH = 0xC87FFF
#: The CAA refreshes the file about weekly. A week-stale index still holds every
#: airframe but the very newest, and the fetch is ~1 MB — not a thing to chase
#: more often than the data actually moves.
CAA_INDEX_TTL_S = 7 * 24 * 3600
#: A failed build is retried within the hour rather than on the next miss: the
#: file is large and a site that is down stays down for more than one contact.
CAA_INDEX_RETRY_S = 3600
#: Generous beside adsbdb's few-KB reply — this is the whole register.
CAA_TIMEOUT_S = 30.0

#: hex -> record for the whole NZ fleet, built once and reused. None until the
#: first NZ contact asks for it, so a deployment that never sees ZK- traffic
#: never fetches the file.
_caa_index: dict[str, dict] | None = None
_caa_expires: float = 0.0
_caa_lock: asyncio.Lock | None = None
_caa_lock_loop: asyncio.AbstractEventLoop | None = None


def _is_nz(icao: str) -> bool:
    try:
        return NZ_ICAO_LOW <= int(icao, 16) <= NZ_ICAO_HIGH
    except ValueError:
        return False


def _caa_singleflight() -> asyncio.Lock:
    """One build at a time, so a burst of NZ contacts triggers a single fetch
    rather than one per contact.

    Recreated when the running loop changes: a deployment keeps one loop for its
    life, but the test suite spins a fresh one per client and an ``asyncio.Lock``
    belongs to the loop it was first awaited on.
    """
    global _caa_lock, _caa_lock_loop
    loop = asyncio.get_running_loop()
    if _caa_lock is None or _caa_lock_loop is not loop:
        _caa_lock = asyncio.Lock()
        _caa_lock_loop = loop
    return _caa_lock


async def _caa_lookup(icao: str) -> dict | None:
    """The register record for an NZ hex, or None.

    A foreign hex returns at once and never triggers a build; an NZ hex builds
    the index on first need and reuses it thereafter.
    """
    if not _is_nz(icao):
        return None
    return (await _caa_get_index()).get(icao)


async def _caa_get_index() -> dict[str, dict]:
    global _caa_index, _caa_expires
    now = time.monotonic()
    if _caa_index is not None and _caa_expires > now:
        return _caa_index
    async with _caa_singleflight():
        # A waiter that blocked while the first caller built the index finds it
        # fresh here and does not fetch again.
        now = time.monotonic()
        if _caa_index is not None and _caa_expires > now:
            return _caa_index
        built = await _fetch_caa_index()
        if built is not None:
            _caa_index, _caa_expires = built, now + CAA_INDEX_TTL_S
        else:
            # Keep a stale index over none, and either way hold off rebuilding so
            # a down site costs one fetch an hour, not one per contact.
            _caa_index = _caa_index if _caa_index is not None else {}
            _caa_expires = now + CAA_INDEX_RETRY_S
        return _caa_index


async def _fetch_caa_index() -> dict[str, dict] | None:
    try:
        async with httpx.AsyncClient(timeout=CAA_TIMEOUT_S) as client:
            response = await client.get(
                CAA_REGISTER_URL, headers={"User-Agent": USER_AGENT},
            )
        response.raise_for_status()
        # No BOM in the file today, but utf-8-sig strips one if the CAA ever adds
        # it; replace keeps an odd byte in an owner name from sinking the parse.
        body = response.content.decode("utf-8-sig", errors="replace")
    except Exception as exc:  # noqa: BLE001 - a label is never worth an error
        log.info("NZ CAA register fetch failed: %s", exc)
        return None
    index = _parse_caa(body)
    log.info("NZ CAA register indexed: %d airframes", len(index))
    return index


def _parse_caa(body: str) -> dict[str, dict]:
    """hex -> record from the register CSV, in the same shape adsbdb returns.

    Keyed by the Mode S hex, lowercased to match the adsbdb path. The register
    has no ICAO type designator (B738), so ``type_code`` stays empty and the
    readable model — manufacturer and model joined — carries the Type row alone.
    """
    index: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(body)):
        hex_code = (row.get("Mode S Code HEX") or "").strip().lower()
        registration = (row.get("Registration Mark") or "").strip()
        if not registration or not _is_hex(hex_code):
            continue
        manufacturer = (row.get("Manufacturer") or "").strip() or None
        model = (row.get("Model") or "").strip() or None
        readable = " ".join(part for part in (manufacturer, model) if part) or None
        index[hex_code] = {
            "icao": hex_code,
            "registration": registration,
            "type_code": None,
            "model": readable,
            "manufacturer": manufacturer,
            "operator": (row.get("Owner Name") or "").strip() or None,
        }
    return index
