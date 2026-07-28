"""Basemap styles, matching DroneOps.

The same three DroneOps offers, so an operator moving between the two products
sees the same map: Esri satellite imagery, OSM street, OpenTopoMap terrain.
Max zooms are DroneOps' values and its reasoning holds here too - capping at what
the provider actually has makes the client upscale the deepest available tile
rather than requesting a level that 404s and renders blank.

Note Esri's path is {z}/{y}/{x}, not {z}/{x}/{y}. Getting that round the wrong
way produces a map that looks plausible and is silently wrong.

USAGE POLICY. These are public endpoints run at someone else's expense. Serving
them through our own cache is gentler than pointing every browser straight at
them - one fetch per tile per deployment rather than per viewer - but bulk
prefetching them is a different matter and OSM's tile usage policy prohibits it.
For anything beyond development, move to a provider you are licensed to cache or
your own tile server, and change nothing else: only these URLs need to differ.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Basemap:
    key: str
    label: str
    url: str
    max_zoom: int
    attribution: str
    #: Raster street maps are drawn for white paper. The console inverts them so
    #: they do not punch a hole in a dark UI at night. Imagery must never be
    #: inverted - it would render the world in false colour.
    invert_for_dark: bool


BASEMAPS: dict[str, Basemap] = {
    "satellite": Basemap(
        key="satellite",
        label="Satellite",
        url=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        max_zoom=19,
        attribution="Tiles © Esri",
        invert_for_dark=False,
    ),
    "street": Basemap(
        key="street",
        label="Street",
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        max_zoom=19,
        attribution="© OpenStreetMap contributors",
        invert_for_dark=True,
    ),
    "terrain": Basemap(
        key="terrain",
        label="Terrain",
        url="https://tile.opentopomap.org/{z}/{x}/{y}.png",
        max_zoom=17,
        attribution="© OpenTopoMap, © OpenStreetMap contributors",
        invert_for_dark=True,
    ),
}

DEFAULT_BASEMAP = "satellite"


def get(key: str) -> Basemap | None:
    return BASEMAPS.get(key)
