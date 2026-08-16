import maplibregl from "maplibre-gl";
import { collapseMapCredit } from "../mapCredit";
import { memo, useEffect, useRef, useState } from "react";
import {
  AIRCRAFT_ICON,
  AIRCRAFT_LAYER,
  AIRCRAFT_SOURCE,
  TRAIL_LAYER,
  TRAIL_SOURCE,
  aircraftFeatures,
  aircraftTrails,
  chevronImage,
} from "../fleetAircraftLayer";
import { fitKey } from "../fleetStationFit";
import type { FleetAircraft, FleetStation, PlatformMapConfig } from "../types";

/**
 * The whole fleet on one map. Platform admins only.
 *
 * Deliberately NOT AdsbMap: that one is centre-locked to a single station with
 * panning off, because a ground station does not move and its operator wants the
 * frame around it. This is the opposite problem — many stations spread across a
 * country — so panning and zoom are on, and the view fits itself to the stations
 * it is given. What it keeps from AdsbMap is the parts that are about not phoning
 * home: raster tiles through our own API (here the platform-wide endpoint, not a
 * per-station one), HTML-node markers rather than a glyph server, and the invert
 * filter that turns a street map drawn for white paper into one for this console.
 *
 * Two overlays: one marker per station coloured by whether it is being heard, and
 * the conglomerated ADS-B — every aircraft any station in the fleet can see,
 * merged to one contact each. Stations refresh slowly; aircraft every few
 * seconds. Both are reconciled in place rather than rebuilt.
 */

/** Flightradar24's aircraft gold, the same as the per-station map, so a contact
 *  reads as a contact wherever it is drawn. */
const CONTACT_COLOUR = "#f5c518";

/** Station marker colours by state — green heard, amber quiet, red gone dark,
 *  grey never seen. The console's own tokens, resolved once. */
const STATUS_COLOUR: Record<string, string> = {
  online: "#35c48a",
  offline: "#f5a623",
  dark: "#ff4d4d",
  never: "#6b7a86",
};

function stationColour(s: FleetStation): string {
  if (s.status === "online") return STATUS_COLOUR.online;
  if (s.status === "never") return STATUS_COLOUR.never;
  return s.dark ? STATUS_COLOUR.dark : STATUS_COLOUR.offline;
}

function FleetMapInner({
  config,
  stations,
  aircraft,
}: {
  config: PlatformMapConfig;
  stations: FleetStation[];
  aircraft: FleetAircraft[];
}) {
  const holderRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const readyRef = useRef(false);
  /** Station markers by id, and aircraft markers by ICAO — reused across
   *  updates so a refresh moves a dot rather than recreating the DOM. */
  const stationMarkers = useRef(new Map<string, maplibregl.Marker>());
  //: The set of placeable stations the view was last fitted to. A KEY, not a
  //: boolean — the boolean was set on the first batch and never cleared, while
  //: the effect that builds the map re-runs on a new config identity, so a
  //: refetched config rebuilt the map at the default view and the fit never
  //: ran again. Nothing looked broken; the map was just always zoomed out.
  const fittedKeyRef = useRef<string | null>(null);
  const [style, setStyle] = useState(config.default_basemap);

  const basemap =
    config.basemaps.find((b) => b.key === style) ?? config.basemaps[0];

  useEffect(() => {
    const holder = holderRef.current;
    if (!holder || !config.basemaps.length) return;

    const map = new maplibregl.Map({
      container: holder,
      style: {
        version: 8,
        sources: Object.fromEntries(
          config.basemaps.map((b) => [
            b.key,
            {
              type: "raster" as const,
              tiles: [
                `${window.location.origin}/api/platform/tiles/${b.key}/{z}/{x}/{y}.png`,
              ],
              tileSize: 256,
              attribution: b.attribution,
              maxzoom: b.max_zoom,
            },
          ]),
        ),
        layers: config.basemaps.map((b) => ({
          id: `base-${b.key}`,
          type: "raster" as const,
          source: b.key,
          layout: {
            visibility: (b.key === style ? "visible" : "none") as
              | "visible"
              | "none",
          },
        })),
      },
      // A whole-country default until fitBounds runs; overwritten the moment
      // there is a station to frame.
      center: [172, -41],
      zoom: config.min_zoom + 1,
      minZoom: config.min_zoom,
      maxZoom: config.max_zoom,
      attributionControl: { compact: true },
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      maxPitch: 0,
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
    // Same as AdsbMap: compact starts expanded until something moves the map.
    collapseMapCredit(map);

    map.on("load", () => {
      readyRef.current = true;
      // Built here, synchronously, because a source added before `load` is
      // discarded and one added later races the first data effect — which
      // fails as an empty map rather than as an error.
      if (!map.hasImage(AIRCRAFT_ICON)) {
        const image = chevronImage(CONTACT_COLOUR);
        // Null where there is no 2D context. Skipping the layer leaves a wall
        // with no aircraft, which is degraded; throwing here would leave no map.
        if (image) map.addImage(AIRCRAFT_ICON, image, { pixelRatio: 2 });
      }
      if (!map.getSource(TRAIL_SOURCE)) {
        map.addSource(TRAIL_SOURCE, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
      }
      // Added BEFORE the symbol layer so trails draw underneath the chevrons —
      // a line over the aircraft it belongs to reads as a different contact.
      if (!map.getLayer(TRAIL_LAYER)) {
        map.addLayer({
          id: TRAIL_LAYER,
          type: "line",
          source: TRAIL_SOURCE,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": CONTACT_COLOUR,
            "line-width": 1.2,
            // Faint: the trail is context for the chevron, not a thing to read
            // in its own right. At fleet scale a wall of full-strength lines
            // becomes the loudest thing on the map.
            "line-opacity": 0.35,
          },
        });
      }
      if (!map.getSource(AIRCRAFT_SOURCE)) {
        map.addSource(AIRCRAFT_SOURCE, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
      }
      if (!map.getLayer(AIRCRAFT_LAYER) && map.hasImage(AIRCRAFT_ICON)) {
        map.addLayer({
          id: AIRCRAFT_LAYER,
          type: "symbol",
          source: AIRCRAFT_SOURCE,
          layout: {
            "icon-image": AIRCRAFT_ICON,
            // Read straight off the feature: rotation is per-contact data now
            // rather than a CSS transform written per element.
            "icon-rotate": ["get", "track"],
            // Rotate with the MAP, not with the screen. A track is a bearing
            // over the ground, so it has to follow the map when it rotates or
            // every aircraft points the wrong way the moment somebody drags.
            "icon-rotation-alignment": "map",
            // Contacts are the data; letting the placer hide overlapping ones
            // would silently thin exactly the busy airspace worth looking at.
            "icon-allow-overlap": true,
            "icon-ignore-placement": true,
          },
        });
      }
    });

    return () => {
      readyRef.current = false;
      stationMarkers.current.clear();
      map.remove();
      mapRef.current = null;
    };
    // Rebuilt only if the basemap set changes; a `style` switch toggles layer
    // visibility below rather than tearing the map down.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config]);

  // Basemap switch + the dark treatment, exactly as AdsbMap does it.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !basemap) return;
    const apply = () => {
      for (const b of config.basemaps) {
        if (map.getLayer(`base-${b.key}`)) {
          map.setLayoutProperty(
            `base-${b.key}`,
            "visibility",
            b.key === basemap.key ? "visible" : "none",
          );
        }
      }
      map.getCanvas().style.filter = basemap.invert_for_dark
        ? "invert(1) hue-rotate(180deg) brightness(0.88) contrast(0.95)"
        : "";
    };
    if (map.isStyleLoaded()) apply();
    else map.once("load", apply);
  }, [basemap, config.basemaps]);

  // Station markers.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    const existing = stationMarkers.current;
    const seen = new Set<string>();
    const located = stations.filter(
      // Number.isFinite, not `!== null`. The old test admitted `undefined` and
      // `NaN` — and when a station arrived without its coordinates at all, both
      // passed straight through and maplibre threw "Invalid LngLat object:
      // (NaN, NaN)", taking the entire platform view down with it. A guard for
      // one particular absent value is not a guard.
      (s) => Number.isFinite(s.latitude) && Number.isFinite(s.longitude),
    );

    for (const s of located) {
      seen.add(s.id);
      const pos: [number, number] = [s.longitude as number, s.latitude as number];
      let marker = existing.get(s.id);
      if (!marker) {
        const el = document.createElement("div");
        el.className = "fleet-station";
        el.innerHTML = "<span class='fleet-dot'></span><span class='fleet-name'></span>";
        marker = new maplibregl.Marker({ element: el, subpixelPositioning: true })
          .setLngLat(pos)
          .addTo(map);
        existing.set(s.id, marker);
      } else {
        marker.setLngLat(pos);
      }
      const el = marker.getElement();
      const dot = el.querySelector(".fleet-dot") as HTMLElement | null;
      if (dot) {
        dot.style.background = stationColour(s);
        el.classList.toggle("simulated", s.is_simulated);
      }
      const name = el.querySelector(".fleet-name");
      if (name) name.textContent = s.name;
      const where = [s.locality, s.organization_name].filter(Boolean).join(" · ");
      el.title = `${s.name}${where ? " — " + where : ""}\n${
        s.status === "never" ? "never connected" : s.dark ? "dark" : s.status
      }`;
    }

    for (const [id, marker] of existing) {
      if (seen.has(id)) continue;
      marker.remove();
      existing.delete(id);
    }

    // Re-fit whenever the SET of placeable stations changes — a station
    // enrolled, removed, or given a position for the first time. Not on every
    // poll: that would yank the view back while somebody was panning.
    const key = fitKey(stations);
    if (key && key !== fittedKeyRef.current && located.length) {
      fittedKeyRef.current = key;
      if (located.length === 1) {
        map.easeTo({
          center: [located[0].longitude as number, located[0].latitude as number],
          zoom: 9,
          duration: 0,
        });
      } else {
        const bounds = new maplibregl.LngLatBounds();
        for (const s of located) {
          bounds.extend([s.longitude as number, s.latitude as number]);
        }
        map.fitBounds(bounds, { padding: 70, maxZoom: 11, duration: 0 });
      }
    }
  }, [stations]);

  // Aircraft — the conglomerated ADS-B, as MAP DATA rather than as DOM.
  //
  // This was one MapLibre Marker per contact, reconciled against a Map keyed by
  // ICAO on every poll. Fine at three stations; at fleet scale it is thousands
  // of elements created, positioned and swept every six seconds, and MapLibre
  // DOM markers cannot cluster or cull. One `setData` hands the whole set to the
  // GPU and the cost stops scaling with contact count.
  //
  // See fleetAircraftLayer.ts for what was given up (native tooltips) and why
  // there are no labels (the style ships no glyphs).
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    const source = map.getSource(AIRCRAFT_SOURCE) as
      | maplibregl.GeoJSONSource
      | undefined;
    if (!source) return;
    source.setData(aircraftFeatures(aircraft) as unknown as GeoJSON.FeatureCollection);
    const trails = map.getSource(TRAIL_SOURCE) as
      | maplibregl.GeoJSONSource
      | undefined;
    if (trails) {
      trails.setData(
        aircraftTrails(aircraft) as unknown as GeoJSON.FeatureCollection,
      );
    }
  }, [aircraft]);

  if (!config.basemaps.length) {
    return <div className="not-permitted">No basemap configured</div>;
  }

  return (
    <div className="fleet-map">
      <div ref={holderRef} className="fleet-map-canvas" />
      {config.basemaps.length > 1 && (
        <div className="basemap-switch" role="group" aria-label="Basemap">
          {config.basemaps.map((b) => (
            <button
              key={b.key}
              type="button"
              className={`basemap-btn${b.key === style ? " active" : ""}`}
              onClick={() => setStyle(b.key)}
              title={b.attribution}
            >
              {b.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export const FleetMap = memo(FleetMapInner);
