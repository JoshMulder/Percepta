"""Where a station is, in words.

A latitude and a longitude are exact and unreadable. "Timaru, Canterbury" is
what somebody says on the phone, and it is what makes a station in a list
recognisable without opening it.

**The station never asks.** It reports its coordinates as it already does and
the server looks them up, for the same reason the map tiles go through a proxy:
a station calling a geocoding API tells a third party where a customer's site is
and when somebody is looking at it, and it fails whenever the station's own link
is down. One lookup here serves every viewer, and the station needs no outbound
access at all.

**Once per position, then never again.** A fixed site does not move, so the
result is stored on the station row and only recomputed when the coordinates
actually change. That keeps us inside any provider's usage policy without a rate
limiter, because the steady-state request rate is zero.

**A failure is silence, not a guess.** No locality is a station that shows its
coordinates, which is what it did before. Nothing here is load-bearing: it is a
label, and a wrong label on a map is worse than no label.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

#: Nominatim's usage policy requires an identifying User-Agent and refuses
#: generic ones. It is also the reason for the caching above: the policy is
#: about sustained load, and this makes ours a handful of requests per station
#: for the lifetime of the deployment.
USER_AGENT = "Percepta/1.0 (ground station console; +https://percepta.local)"
ENDPOINT = "https://nominatim.openstreetmap.org/reverse"

#: Zoom 10 is roughly "town or suburb". Deeper starts returning a street the
#: station is not on, which reads as false precision about a mast in a paddock.
ZOOM = 10
TIMEOUT_S = 8.0

#: Nominatim spreads the useful name across several keys depending on how the
#: place is classified, and only one of them is present. In order of how well
#: each answers "what would somebody call this place".
LOCALITY_KEYS = (
    "city", "town", "village", "hamlet", "suburb", "locality",
    "municipality", "county",
)
REGION_KEYS = ("state", "region", "province", "state_district")


def describe(latitude: float, longitude: float) -> dict | None:
    """Locality and region for a coordinate, or None if it cannot be resolved.

    Synchronous and called off the request path (see `station_ingest`), because
    it is a network call to somebody else's service and nothing waiting on a
    console render should depend on it being up.
    """
    try:
        response = httpx.get(
            ENDPOINT,
            params={
                "lat": f"{latitude:.5f}",
                "lon": f"{longitude:.5f}",
                "format": "jsonv2",
                "zoom": ZOOM,
                # Their own advice for machine use: skip the prose address.
                "addressdetails": 1,
            },
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=TIMEOUT_S,
            follow_redirects=True,
        )
        response.raise_for_status()
        address = (response.json() or {}).get("address") or {}
    except Exception as exc:  # noqa: BLE001 - a label is never worth an error
        log.info("Reverse geocode failed for %.4f,%.4f: %s", latitude, longitude, exc)
        return None

    locality = next((address[k] for k in LOCALITY_KEYS if address.get(k)), None)
    region = next((address[k] for k in REGION_KEYS if address.get(k)), None)
    country = address.get("country")

    if not locality and not region:
        # Open ocean, Antarctica, and anywhere else with no populated place
        # nearby. Returning the country alone would put "New Zealand" under a
        # station that is plainly in New Zealand on the map above it.
        return None

    return {
        "locality": locality,
        "region": region,
        "country": country,
        # What a person would say. Built here so every consumer says the same
        # thing rather than each joining the parts its own way.
        "label": ", ".join(part for part in (locality, region) if part),
    }
