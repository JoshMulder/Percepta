"""Basemap tiles: cached on disk, fetched upstream on a miss.

A ground station is fixed, so the map around it is a finite set of tiles. Two
ways to fill the cache, and they complement each other:

  * cache-through (default). A tile the console asks for and we do not have is
    fetched once, stored, and served. The cache warms itself over the areas
    people actually look at, and a second viewer of the same station costs
    nothing upstream.
  * prefetch (scripts/cache_map.py). Deliberately walk a station's whole area so
    it works before anyone has looked at it, and keeps working if the upstream
    provider is unreachable.

Either way the console only ever talks to us. That keeps the map working when
the backhaul is degraded, costs no metered bandwidth per viewer, and means
opening a station tells no third party where a customer's site is or when
someone is watching it.

See basemaps.py for the styles and the usage-policy note that goes with them.
"""

import logging
import math
import os
import re
from pathlib import Path

import httpx

from backend.core.config import settings
from backend.services.basemaps import Basemap, get as get_basemap

log = logging.getLogger(__name__)

MAX_ZOOM = 19

# Tiles are addressed straight from a URL path, so coordinates are range-checked
# before they touch the filesystem, and the style is resolved through the
# basemap registry rather than interpolated. A traversal here would read
# arbitrary files out of the container.
_STYLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class TileError(Exception):
    pass


def _checked_style(style: str) -> Basemap:
    if not _STYLE_RE.match(style):
        raise TileError("bad style")
    basemap = get_basemap(style)
    if basemap is None:
        raise TileError("unknown style")
    return basemap


def tile_path(style: str, z: int, x: int, y: int) -> Path:
    basemap = _checked_style(style)
    if not (0 <= z <= min(MAX_ZOOM, basemap.max_zoom)):
        raise TileError("zoom out of range")
    limit = 1 << z
    if not (0 <= x < limit and 0 <= y < limit):
        raise TileError("tile out of range")
    return Path(settings.tile_cache_dir) / basemap.key / str(z) / str(x) / f"{y}.png"


def source_url(style: str, z: int, x: int, y: int) -> str:
    basemap = _checked_style(style)
    return (
        basemap.url.replace("{z}", str(z))
        .replace("{x}", str(x))
        .replace("{y}", str(y))
    )


def read_cached(style: str, z: int, x: int, y: int) -> bytes | None:
    path = tile_path(style, z, x, y)
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        log.warning("Could not read cached tile %s %s/%s/%s", style, z, x, y)
        return None


def store(style: str, z: int, x: int, y: int, data: bytes) -> None:
    path = tile_path(style, z, x, y)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then rename, so a fetch interrupted halfway cannot leave a truncated
    # tile that would be served as valid forever after.
    # PID in the temp name so two workers fetching the same missing tile at the
    # same moment cannot half-write over each other.
    temporary = path.with_suffix(f".{os.getpid()}.part")
    temporary.write_bytes(data)
    temporary.replace(path)


def fetch(client: httpx.Client, style: str, z: int, x: int, y: int) -> bytes | None:
    """Fetch one tile upstream and cache it. None if upstream refused it."""
    response = client.get(
        source_url(style, z, x, y),
        headers={"User-Agent": settings.tile_user_agent},
        timeout=15.0,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.content
    if not data:
        return None
    store(style, z, x, y, data)
    return data


def deg_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    """Standard slippy-map projection (Web Mercator)."""
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_for_area(
    lat: float, lon: float, radius_km: float, min_zoom: int, max_zoom: int
) -> list[tuple[int, int, int]]:
    """Every (z, x, y) covering a square of `radius_km` around the point.

    A square rather than a circle: the corners are cheap at these zooms, and an
    operator panning to the edge of the configured area should not find the map
    falling away diagonally.
    """
    tiles: list[tuple[int, int, int]] = []
    # Latitude degrees per km is near enough constant; longitude narrows with
    # latitude, which is a large correction at the ~45S these stations sit at.
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.05, math.cos(math.radians(lat))))

    for z in range(min_zoom, max_zoom + 1):
        x_min, y_max = deg_to_tile(lat - dlat, lon - dlon, z)
        x_max, y_min = deg_to_tile(lat + dlat, lon + dlon, z)
        for x in range(min(x_min, x_max), max(x_min, x_max) + 1):
            for y in range(min(y_min, y_max), max(y_min, y_max) + 1):
                tiles.append((z, x, y))
    return tiles


def estimate(lat: float, lon: float, radius_km: float, lo: int, hi: int) -> dict:
    """What a prefetch would cost, so it can be reported before it is run."""
    tiles = tiles_for_area(lat, lon, radius_km, lo, hi)
    # ~15 kB is typical for a raster tile; enough to tell 40 MB from 4 GB, which
    # is the decision actually being made.
    return {"tiles": len(tiles), "estimated_mb": round(len(tiles) * 15 / 1024, 1)}


def cache_size() -> dict:
    root = Path(settings.tile_cache_dir)
    if not root.is_dir():
        return {"tiles": 0, "mb": 0.0}
    total = count = 0
    for path in root.rglob("*.png"):
        try:
            total += path.stat().st_size
            count += 1
        except OSError:
            continue
    return {"tiles": count, "mb": round(total / (1024 * 1024), 1)}
