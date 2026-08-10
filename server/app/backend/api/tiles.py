import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth.capabilities import Capability
from backend.auth.dependencies import require_capability
from backend.auth.identity import Identity
from backend.auth.platform import require_platform_admin
from backend.core.config import settings
from backend.database.dependencies import get_db
from backend.database.models.ground_station import GroundStation
from backend.services import tile_cache
from backend.services.basemaps import BASEMAPS, DEFAULT_BASEMAP

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["map"])

# One client for the process. Creating one per tile would open a fresh TLS
# connection for every miss, which is both slow and rude to the upstream.
_client = httpx.Client(follow_redirects=True, timeout=15.0)


class BasemapOption(BaseModel):
    key: str
    label: str
    max_zoom: int
    attribution: str
    invert_for_dark: bool


class MapConfig(BaseModel):
    latitude: float | None
    longitude: float | None
    min_zoom: int
    max_zoom: int
    radius_km: float
    cached_at: str | None
    default_basemap: str
    basemaps: list[BasemapOption]
    live_fetch: bool


@router.get("/stations/{station_id}/map", response_model=MapConfig)
def map_config(
    station_id: uuid.UUID,
    identity: Identity = Depends(require_capability(Capability.STATION_VIEW)),
    db: Session = Depends(get_db),
) -> MapConfig:
    """What the console needs to render the basemap for this station."""
    station = db.execute(
        select(GroundStation).where(GroundStation.id == station_id)
    ).scalar_one_or_none()
    if station is None:
        raise HTTPException(status_code=404, detail="Station not available")

    return MapConfig(
        latitude=station.latitude,
        longitude=station.longitude,
        min_zoom=station.map_min_zoom,
        max_zoom=station.map_max_zoom,
        radius_km=station.map_radius_km,
        cached_at=station.map_cached_at.isoformat() if station.map_cached_at else None,
        default_basemap=DEFAULT_BASEMAP,
        basemaps=[
            BasemapOption(
                key=b.key,
                label=b.label,
                max_zoom=b.max_zoom,
                attribution=b.attribution,
                invert_for_dark=b.invert_for_dark,
            )
            for b in BASEMAPS.values()
        ],
        live_fetch=settings.tile_live_fetch,
    )


def _serve_tile(style: str, z: int, x: int, y: int) -> Response:
    """Serve a tile: from the shared cache, or fetched upstream once and cached.

    The console never talks to a tile provider - every request lands here. That
    is what keeps the map working on a degraded link, stops each viewer costing
    metered bandwidth, and means opening a map tells no third party where a
    customer's site is or when someone is watching it.

    With TILE_LIVE_FETCH off this is cache-only, and an uncached tile 404s so the
    map renders an empty background with aircraft still plotted over it.

    The cache is keyed by style/z/x/y alone — it is shared across tenants, and
    across the per-station and platform-wide callers — so a tile any station has
    fetched is already warm for the fleet map.
    """
    try:
        data = tile_cache.read_cached(style, z, x, y)
        if data is None and settings.tile_live_fetch:
            data = tile_cache.fetch(_client, style, z, x, y)
    except tile_cache.TileError:
        raise HTTPException(status_code=400, detail="Bad tile request")
    except httpx.HTTPError as exc:
        # Upstream being unreachable is not our error to surface as a 500: the
        # map just has a hole in it, and the console handles a missing tile.
        log.warning("Upstream tile fetch failed (%s %s/%s/%s): %s", style, z, x, y, exc)
        raise HTTPException(status_code=404, detail="Tile unavailable")

    if data is None:
        raise HTTPException(status_code=404, detail="Tile not available")

    return Response(
        content=data,
        media_type="image/png",
        headers={
            # A fixed tile does not change. immutable stops revalidation
            # entirely, which matters on a link with real latency.
            "Cache-Control": "public, max-age=604800, immutable",
            # No cache hit/miss header. The tile cache is shared across every
            # tenant, so "hit" for an out-of-the-way tile told the caller that
            # some other tenant has a station there and has looked at it — a
            # cross-tenant location oracle on the one surface built precisely so
            # opening a map "tells no third party where a customer's site is".
            # (Response timing still leaks the same bit on a live-fetch miss;
            # that is a deeper fix, but the free header is not worth handing out.)
        },
    )


@router.get("/stations/{station_id}/tiles/{style}/{z}/{x}/{y}.png")
def tile(
    station_id: uuid.UUID,
    style: str,
    z: int,
    x: int,
    y: int,
    identity: Identity = Depends(require_capability(Capability.STATION_VIEW)),
) -> Response:
    """A basemap tile for one station's map, authorised by station view."""
    return _serve_tile(style, z, x, y)


@router.get("/platform/tiles/{style}/{z}/{x}/{y}.png")
def platform_tile(
    style: str,
    z: int,
    x: int,
    y: int,
    identity: Identity = Depends(require_platform_admin),
) -> Response:
    """A basemap tile for the platform fleet map — wide-area, not bounded to any
    one station's radius. Same shared cache and upstream as the per-station
    tiles; platform-admin only, since the fleet map spans every tenant."""
    return _serve_tile(style, z, x, y)


class PlatformMapConfig(BaseModel):
    min_zoom: int
    max_zoom: int
    default_basemap: str
    basemaps: list[BasemapOption]
    live_fetch: bool


@router.get("/platform/map", response_model=PlatformMapConfig)
def platform_map(
    identity: Identity = Depends(require_platform_admin),
) -> PlatformMapConfig:
    """Basemap options for the fleet map. Unlike the per-station config there is
    no centre or radius — the map fits itself to the stations it is given — and
    it may zoom right out, so `min_zoom` is low enough to see a whole country."""
    return PlatformMapConfig(
        min_zoom=3,
        max_zoom=max(b.max_zoom for b in BASEMAPS.values()),
        default_basemap=DEFAULT_BASEMAP,
        basemaps=[
            BasemapOption(
                key=b.key,
                label=b.label,
                max_zoom=b.max_zoom,
                attribution=b.attribution,
                invert_for_dark=b.invert_for_dark,
            )
            for b in BASEMAPS.values()
        ],
        live_fetch=settings.tile_live_fetch,
    )
