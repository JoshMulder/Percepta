import type { FleetAircraft } from "./types";

/**
 * The fleet's aircraft as map data rather than as DOM.
 *
 * WHY THIS STOPPED BEING A DOM MARKER. Two hundred station markers are fine —
 * they are few, they need labels and click targets, and they stay. Aircraft are
 * different in kind: this is ADS-B merged across every receiver in the fleet, so
 * the count scales with airspace activity times receiver count, and each contact
 * was an element, a MapLibre Marker instance and a per-frame transform write.
 * At three stations that is nothing. At two hundred it is thousands of DOM nodes
 * reconciled every six seconds against a Map keyed by ICAO, and the browser
 * spends its time on layout instead of on the wall.
 *
 * A GeoJSON source with a symbol layer hands the whole set to the GPU in one
 * `setData`. The cost stops scaling with contact count in any way an operator
 * can feel.
 *
 * WHAT WAS GIVEN UP, honestly: the per-marker `title` tooltip. Native tooltips
 * came free with a DOM element and do not exist for a symbol. The rotation,
 * colour and density all survive, and hover detail — if it is wanted — becomes a
 * `queryRenderedFeatures` handler rather than a browser affordance.
 *
 * NO TEXT LABELS on this layer, and it is not an oversight. A symbol layer's
 * `text-field` needs a glyph source, and the fleet map's style has none — adding
 * one means shipping or proxying a font PBF set. Callsigns on a fleet-wide view
 * would be unreadable overplotting anyway; the single-station map is where a
 * label belongs.
 */

export const AIRCRAFT_SOURCE = "fleet-aircraft";
export const AIRCRAFT_LAYER = "fleet-aircraft-symbols";
export const AIRCRAFT_ICON = "fleet-aircraft-chevron";

export interface AircraftFeatureCollection {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    id?: number;
    geometry: { type: "Point"; coordinates: [number, number] };
    properties: { icao: string; track: number; label: string; heard_by: number };
  }>;
}

/**
 * Contacts to GeoJSON. Pure, and separated from the map for exactly that reason:
 * it is the only part of this conversion a test runner can reach, since jsdom
 * has no WebGL and the suite's maplibre stub has no `addSource`.
 *
 * Contacts without a usable position are DROPPED rather than defaulted. A
 * missing latitude became `NaN` once before and MapLibre answered with
 * "Invalid LngLat object: (NaN, NaN)" — a hard throw that took the whole wall
 * down, from one aircraft in one frame. `Number.isFinite` rather than a null
 * check, because `undefined` passes `!== null` and is exactly what arrived.
 */
export function aircraftFeatures(
  aircraft: FleetAircraft[],
): AircraftFeatureCollection {
  const features: AircraftFeatureCollection["features"] = [];
  for (const a of aircraft) {
    if (!Number.isFinite(a.longitude) || !Number.isFinite(a.latitude)) continue;
    features.push({
      type: "Feature",
      geometry: {
        type: "Point",
        coordinates: [a.longitude as number, a.latitude as number],
      },
      properties: {
        icao: a.icao,
        // A contact with no track points north rather than disappearing: a
        // stationary-looking chevron is a fair rendering of "we do not know
        // which way this is going", and dropping it would hide the aircraft.
        track: Number.isFinite(a.track_deg) ? (a.track_deg as number) : 0,
        label: a.callsign?.trim() || a.icao,
        heard_by: a.heard_by ?? 1,
      },
    });
  }
  return { type: "FeatureCollection", features };
}

/**
 * The chevron, baked to pixels once.
 *
 * `map.addImage` takes image data, not markup, so the SVG that used to be
 * written into each marker's innerHTML becomes one canvas draw at load. Drawn at
 * 2x and declared `pixelRatio: 2` so it stays sharp on the high-density panels
 * these walls actually run on.
 *
 * Returns null where there is no 2D context — jsdom, and any browser that has
 * lost its canvas — so the caller can skip the layer rather than throw. A wall
 * with no aircraft is degraded; a wall that threw during map setup is blank.
 */
export function chevronImage(colour: string): ImageData | null {
  const size = 26; // 13 CSS pixels at 2x
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const half = size / 2;
  ctx.translate(half, half);
  const scale = size / 12;
  ctx.beginPath();
  ctx.moveTo(0 * scale, -5 * scale);
  ctx.lineTo(4 * scale, 4 * scale);
  ctx.lineTo(0 * scale, 2 * scale);
  ctx.lineTo(-4 * scale, 4 * scale);
  ctx.closePath();
  ctx.fillStyle = colour;
  ctx.fill();
  // The same dark outline the DOM chevron carried: without it a yellow contact
  // over a yellow-ish basemap disappears, which is the one thing a contact must
  // never do.
  ctx.strokeStyle = "#0b1220";
  ctx.lineWidth = 0.8 * scale;
  ctx.stroke();

  return ctx.getImageData(0, 0, size, size);
}
