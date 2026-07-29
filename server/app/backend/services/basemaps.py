"""Basemap styles.

Esri satellite imagery only. DroneOps also offers OSM street and OpenTopoMap
terrain; those were dropped here because a security site is judged on what is
actually on the ground - buildings, vehicles, vegetation - and a drawn map shows
none of it. Max zoom is DroneOps' value and its reasoning holds: capping at what
the provider actually has makes the client upscale the deepest available tile
rather than requesting a level that 404s and renders blank.

Adding one back is a dict entry. The console switches basemaps when it is
offered more than one and hides the control when it is not, so nothing else
needs to change.

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
}

DEFAULT_BASEMAP = "satellite"


def get(key: str) -> Basemap | None:
    return BASEMAPS.get(key)
