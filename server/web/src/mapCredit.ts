import type { Map as MapLibreMap } from "maplibre-gl";

/**
 * Collapse the map credit to its "i" disc.
 *
 * `attributionControl: { compact: true }` does NOT start collapsed, despite how
 * it reads. MapLibre 4.7's `_updateCompact` adds *both* classes the first time
 * the control goes compact:
 *
 *     this._container.classList.add("maplibregl-compact", "maplibregl-compact-show")
 *
 * so the credit renders expanded, as a text box over the corner of the map, and
 * only minimises when `_updateCompactMinimize` fires — which is bound to map
 * movement. On a dashboard panel that nobody ever drags, that is never, and the
 * "compact" credit sits there permanently as the full string.
 *
 * There is no public API for the initial state, so this removes the class the
 * same way MapLibre's own minimiser does. It is safe against being re-shown:
 * `_updateCompact` short-circuits on `classList.contains("maplibregl-compact")`,
 * which stays on the element, so a later resize or source update cannot put
 * `-show` back. The click-to-expand toggle keeps working — `_toggleAttribution`
 * reads the same class and simply sees the collapsed state, which is what it
 * would have seen after any pan.
 *
 * Timing matters: the control is empty until the sources' attribution arrives,
 * and while empty it carries `maplibregl-attrib-empty` and `_updateCompact`
 * skips it entirely — so a `load` handler can run *before* the class we want to
 * remove has been added. `idle` is after the style and the first tiles have
 * settled, which is late enough for the credit to exist.
 *
 * The credit itself is never removed: Esri's imagery terms and OSM's ODbL both
 * require the provider to be shown on the map. The disc expands on click, which
 * is how MapLibre, Mapbox and Google all present the same obligation.
 */
export function collapseMapCredit(map: MapLibreMap): void {
  map.once("idle", () => {
    map
      .getContainer()
      .querySelectorAll(".maplibregl-ctrl-attrib.maplibregl-compact-show")
      .forEach((el) => el.classList.remove("maplibregl-compact-show"));
  });
}
