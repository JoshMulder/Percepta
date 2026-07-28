"""Download and cache the basemap around a ground station.

    docker compose exec app python -m backend.scripts.cache_map --list
    docker compose exec app python -m backend.scripts.cache_map "Kaikoura Ridge"
    docker compose exec app python -m backend.scripts.cache_map --all

A station is fixed, so this runs once per station (and again only if its zoom
range or radius changes). It reports the tile count and estimated size and asks
before fetching, because tile count grows 4x per zoom level and the difference
between max_zoom 14 and 17 is roughly 64x.

Cache-through (see api/tiles.py) already fills the cache for whatever people
look at. This is for filling it deliberately and in advance, so a station works
before anyone has opened it and keeps working if the provider is unreachable.

Style defaults to the satellite basemap; pass --style to prefetch another.
"""

import argparse
import logging
import sys
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from backend.core.config import settings
from backend.database.models.ground_station import GroundStation
from backend.services.basemaps import BASEMAPS, DEFAULT_BASEMAP
from backend.database.session import PrivilegedSessionLocal
from backend.services import tile_cache

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("cache_map")

# Polite fixed pacing rather than parallel fetching. This is a one-off bulk
# download against someone else's server; hammering it is how a licence gets
# withdrawn, and the operation is not latency-sensitive.
DELAY_SECONDS = 0.1


def stations(db, name: str | None, every: bool) -> list[GroundStation]:
    query = select(GroundStation).where(GroundStation.is_active.is_(True))
    if not every and name:
        query = query.where(GroundStation.name == name)
    return list(db.execute(query.order_by(GroundStation.name)).scalars())


def cache_one(station: GroundStation, style: str, assume_yes: bool) -> bool:
    if station.latitude is None or station.longitude is None:
        log.warning("%s has no coordinates; skipping.", station.name)
        return False

    top = min(station.map_max_zoom, BASEMAPS[style].max_zoom)
    tiles = tile_cache.tiles_for_area(
        station.latitude,
        station.longitude,
        station.map_radius_km,
        station.map_min_zoom,
        top,
    )
    size = tile_cache.estimate(
        station.latitude,
        station.longitude,
        station.map_radius_km,
        station.map_min_zoom,
        top,
    )
    log.info(
        "\n%s  (%.4f, %.4f)  [%s]\n  zoom %d-%d, radius %.0f km\n"
        "  %d tiles, ~%.1f MB, ~%.0f min at %.0f ms/tile",
        station.name,
        station.latitude,
        station.longitude,
        style,
        station.map_min_zoom,
        top,
        station.map_radius_km,
        size["tiles"],
        size["estimated_mb"],
        len(tiles) * DELAY_SECONDS / 60,
        DELAY_SECONDS * 1000,
    )

    if not assume_yes:
        try:
            if input("  Fetch? [y/N] ").strip().lower() not in ("y", "yes"):
                log.info("  skipped")
                return False
        except EOFError:
            log.info("  no tty and --yes not given; skipped")
            return False

    fetched = skipped = failed = 0
    with httpx.Client(follow_redirects=True) as client:
        for index, (z, x, y) in enumerate(tiles, start=1):
            try:
                if tile_cache.read_cached(style, z, x, y) is not None:
                    skipped += 1
                    continue
                if tile_cache.fetch(client, style, z, x, y) is not None:
                    fetched += 1
                    time.sleep(DELAY_SECONDS)
                else:
                    skipped += 1
            except tile_cache.TileError as exc:
                log.error("  %s", exc)
                return False
            except Exception as exc:
                failed += 1
                if failed <= 3:
                    log.warning("  tile %d/%d/%d failed: %s", z, x, y, exc)
                if failed > 50:
                    log.error("  too many failures; stopping.")
                    break
            if index % 200 == 0:
                log.info("  %d/%d", index, len(tiles))

    log.info("  fetched %d, already cached %d, failed %d", fetched, skipped, failed)

    if fetched or skipped:
        with PrivilegedSessionLocal() as db:
            db.execute(
                GroundStation.__table__.update()
                .where(GroundStation.id == station.id)
                .values(map_cached_at=datetime.now(UTC))
            )
            db.commit()
    return failed == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("station", nargs="?", help="station name")
    parser.add_argument("--all", action="store_true", help="every active station")
    parser.add_argument("--list", action="store_true", help="show stations and status")
    parser.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    parser.add_argument(
        "--style", default=DEFAULT_BASEMAP, choices=sorted(BASEMAPS),
        help=f"basemap to prefetch (default {DEFAULT_BASEMAP})",
    )
    args = parser.parse_args()

    with PrivilegedSessionLocal() as db:
        found = stations(db, args.station, args.all or args.list)

    if args.list:
        current = tile_cache.cache_size()
        log.info("Cache: %d tiles, %.1f MB at %s",
                 current["tiles"], current["mb"], settings.tile_cache_dir)
        log.info("Styles: %s", ", ".join(sorted(BASEMAPS)))
        log.info("Live fetch on miss: %s", "on" if settings.tile_live_fetch else "off")
        for s in found:
            log.info(
                "  %-24s zoom %d-%d  %.0f km  %s",
                s.name, s.map_min_zoom, s.map_max_zoom, s.map_radius_km,
                s.map_cached_at.strftime("%Y-%m-%d %H:%M") if s.map_cached_at
                else "not cached",
            )
        return 0

    if not found:
        log.error("No matching station. Use --list to see them.")
        return 1

    log.warning(
        "Prefetching '%s'. These are public tile servers run at someone else's\n"
        "expense, and bulk downloading is against OpenStreetMap's usage policy.\n"
        "Fine for development; for production move to a provider you are\n"
        "licensed to cache, or your own tile server (services/basemaps.py).",
        args.style,
    )

    ok = all(cache_one(s, args.style, args.yes) for s in found)
    final = tile_cache.cache_size()
    log.info("\nCache now %d tiles, %.1f MB", final["tiles"], final["mb"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
