import type { FleetStation } from "./types";

/**
 * Which stations the map can actually place, and when the view should re-fit.
 *
 * Pure, and separated from the map for exactly that reason: jsdom has no WebGL
 * and the suite's maplibre stub has no `fitBounds` or `easeTo`, so an assertion
 * written against the map component passes against nothing. The same argument
 * `fleetAircraftLayer.ts` already makes for the aircraft features.
 */

/**
 * Stations with a usable position.
 *
 * `Number.isFinite`, not a null check. The digest omitted latitude and longitude
 * once and the wall went down on `Invalid LngLat object: (NaN, NaN)` — because
 * `!== null` admits `undefined`, which is exactly what arrived. A station
 * without a position is dropped from the MAP, not from the fleet.
 */
export function located(stations: FleetStation[]): FleetStation[] {
  return stations.filter(
    (s) => Number.isFinite(s.latitude) && Number.isFinite(s.longitude),
  );
}

/**
 * A key that changes when the set of placeable stations changes.
 *
 * THE FIT USED TO RUN ONCE, EVER. It was guarded by a boolean ref set on the
 * first batch containing any located station — and never cleared, while the
 * effect that builds the map re-runs whenever the map config identity changes.
 * So a refetched config rebuilt the map at the whole-of-New-Zealand default and
 * the fit never ran again for the life of the component. Nothing looked broken:
 * the map was simply always zoomed out, and every station a dot.
 *
 * Keyed on the ID SET rather than a count, so a station being replaced by
 * another re-fits, and sorted so the same set in a different order does not.
 * Deliberately NOT keyed on coordinates: a station whose position is corrected
 * by a metre must not yank the operator's view back mid-shift.
 */
export function fitKey(stations: FleetStation[]): string {
  return located(stations)
    .map((s) => s.id)
    .sort()
    .join(",");
}
