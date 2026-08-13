import maplibregl from "maplibre-gl";
import { collapseMapCredit } from "../mapCredit";
import { memo, useEffect, useRef, useState } from "react";
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
  const aircraftMarkers = useRef(new Map<string, maplibregl.Marker>());
  const fittedRef = useRef(false);
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
    });

    return () => {
      readyRef.current = false;
      stationMarkers.current.clear();
      aircraftMarkers.current.clear();
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
      (s) => s.latitude !== null && s.longitude !== null,
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

    // Fit to the fleet once, on the first batch that has any located station.
    if (!fittedRef.current && located.length) {
      fittedRef.current = true;
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

  // Aircraft — the conglomerated ADS-B. Small chevrons, rotated to track.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    const existing = aircraftMarkers.current;
    const seen = new Set<string>();

    for (const a of aircraft) {
      seen.add(a.icao);
      const pos: [number, number] = [a.longitude, a.latitude];
      let marker = existing.get(a.icao);
      if (!marker) {
        const el = document.createElement("div");
        el.className = "fleet-ac";
        // A minimal chevron — a fleet view wants density, not the per-shape
        // silhouettes the single-station map draws.
        el.innerHTML =
          "<svg viewBox='-6 -6 12 12' width='13' height='13'>" +
          "<path d='M0,-5 L4,4 L0,2 L-4,4 Z' fill='" +
          CONTACT_COLOUR +
          "' stroke='#0b1220' stroke-width='0.8'/></svg>";
        marker = new maplibregl.Marker({ element: el, subpixelPositioning: true })
          .setLngLat(pos)
          .addTo(map);
        existing.set(a.icao, marker);
      } else {
        marker.setLngLat(pos);
      }
      const el = marker.getElement();
      const svg = el.querySelector("svg");
      if (svg) svg.style.transform = `rotate(${a.track_deg ?? 0}deg)`;
      el.title = `${a.callsign?.trim() || a.icao}${
        a.heard_by > 1 ? ` · heard by ${a.heard_by}` : ""
      }`;
    }

    for (const [icao, marker] of existing) {
      if (seen.has(icao)) continue;
      marker.remove();
      existing.delete(icao);
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
