import { useEffect, useRef, useState } from "react";
import { memo } from "react";
import L from "leaflet";
import type { Aircraft, MapConfig } from "../types";

/** Round a ring spacing to something a person would actually say: 1, 2, 5, 10,
 *  20, 50... Raw radius/4 gives values like 12.5 km, which reads as noise. */
function niceStep(raw: number): number {
  const magnitude = 10 ** Math.floor(Math.log10(Math.max(raw, 0.1)));
  const scaled = raw / magnitude;
  const step = scaled >= 5 ? 5 : scaled >= 2 ? 2 : 1;
  return step * magnitude;
}

/**
 * Aircraft plotted on the station's basemap.
 *
 * Tiles come from our own API, never straight from a provider. The server serves
 * them from its cache and fetches upstream once on a miss, so a second viewer of
 * the same station costs nothing, the map keeps working on a degraded link, and
 * opening a station tells no third party where a customer's site is or when
 * someone is watching it.
 *
 * Zoom is clamped to the lower of the station's configured maximum and what the
 * chosen basemap actually has. Letting a user past it shows a blank grid, which
 * reads as a broken map rather than a limit.
 *
 * The station stays at the centre: panning is off, and zoom recentres on it.
 * A ground station does not move, so the useful frame is always the one around
 * it - and a map that has been dragged somewhere else is a map an operator has
 * to re-orient before they can read it, which is the wrong thing to be doing
 * when something is happening.
 */
function AdsbMapInner({
  stationId,
  config,
  aircraft,
  compact,
}: {
  stationId: string;
  config: MapConfig;
  aircraft: Aircraft[];
  compact?: boolean;
}) {
  const holderRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);
  /** Markers by ICAO address, reused between updates. */
  const markersRef = useRef(new Map<string, { marker: L.Marker; ring?: L.CircleMarker }>());
  const [style, setStyle] = useState(config.default_basemap);

  const basemap =
    config.basemaps.find((b) => b.key === style) ?? config.basemaps[0];
  // As deep as the chosen basemap actually has tiles. The station's own
  // map_max_zoom governs how far a *prefetch* goes, not how far an operator may
  // zoom: with cache-through, going deeper than what was prefetched simply
  // fetches those tiles on demand. Capping the display at the prefetch depth
  // would stop someone reading a numberplate for no reason but bookkeeping.
  const maxZoom = basemap?.max_zoom ?? config.max_zoom;

  useEffect(() => {
    const holder = holderRef.current;
    if (!holder || config.latitude === null || config.longitude === null) return;
    if (!basemap) return;

    const centre: L.LatLngExpression = [config.latitude, config.longitude];
    const map = L.map(holder, {
      center: centre,
      zoom: Math.min(maxZoom, Math.max(config.min_zoom, compact ? 9 : 12)),
      minZoom: config.min_zoom,
      maxZoom,
      zoomControl: !compact,
      // No attribution control at all. Note that the tile providers' terms
      // generally *require* their notice to be displayed - Esri's and
      // OpenStreetMap's both do - so this is the same licensing question as the
      // tile sourcing itself, and is tracked with it in
      // docs/04-production-readiness.md rather than being quietly dropped.
      attributionControl: false,
      // Locked to the station. Dragging, keyboard panning and double-click zoom
      // (which recentres on the click point) are all off, so the station cannot
      // drift off centre by any route.
      dragging: false,
      keyboard: false,
      doubleClickZoom: false,
      // Scroll zoom stays on for the main map - it zooms about the centre when
      // dragging is disabled, so it cannot shift the frame.
      scrollWheelZoom: !compact,
      touchZoom: !compact,
    });

    L.tileLayer(`/api/stations/${stationId}/tiles/${basemap.key}/{z}/{x}/{y}.png`, {
      minZoom: config.min_zoom,
      maxZoom,
      // A tile the server has neither cached nor could fetch 404s. Showing
      // nothing is right - the aircraft overlay stays usable over a gap.
      errorTileUrl:
        "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
      className: basemap.invert_for_dark ? "tile-dark" : undefined,
    }).addTo(map);

    L.circleMarker(centre, {
      radius: 5,
      color: "#35c48a",
      fillColor: "#35c48a",
      fillOpacity: 1,
      weight: 2,
    }).addTo(map);

    if (!compact) {
      // Rings scale with the configured extent rather than being fixed, so a
      // 15 km site and a 120 km site both get four useful references instead of
      // one ring or a dozen.
      const step = niceStep(config.radius_km / 4);
      for (let km = step; km <= config.radius_km; km += step) {
        L.circle(centre, {
          radius: km * 1000,
          color: "rgba(255,255,255,0.22)",
          weight: 1,
          fill: false,
          dashArray: "4 6",
          interactive: false,
        }).addTo(map);

        // Label each ring due north of the station. Without this the rings are
        // decoration - an operator can see something is 'about two rings out'
        // but not what that means in kilometres.
        const north = L.latLng(
          config.latitude + km / 111.0,
          config.longitude,
        );
        L.marker(north, {
          interactive: false,
          keyboard: false,
          icon: L.divIcon({
            className: "range-label",
            html: `${km} km`,
            iconSize: [44, 14],
            iconAnchor: [22, 7],
          }),
        }).addTo(map);
      }
    }

    // NO moveend re-centring here, deliberately.
    //
    // An earlier version snapped the view back on `moveend` as belt and braces.
    // That recurses without end: setView -> panBy -> fires moveend -> setView,
    // and floating-point centres never compare exactly equal, so the guard never
    // stops. It blew the stack on every load and pinned the main thread, which
    // is what made the sidebar scale look like it never settled and stopped the
    // rest of the console initialising at all.
    //
    // The map is already locked by its options above - dragging, keyboard
    // panning and double-click zoom are off, and scroll zoom keeps the centre.
    // There is nothing left that can move it, so there is nothing to correct.

    layerRef.current = L.layerGroup().addTo(map);
    markersRef.current.clear();
    mapRef.current = map;

    // Leaflet measures its container on creation; inside a flex/grid panel that
    // can still be zero at that moment, leaving a grey box until something
    // forces a relayout.
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(holder);

    return () => {
      observer.disconnect();
      map.remove();
      mapRef.current = null;
      layerRef.current = null;
    };
  }, [stationId, config, compact, basemap, maxZoom]);

  /**
   * Update the contacts in place rather than clearing and rebuilding.
   *
   * The previous version called clearLayers() and recreated every marker and
   * permanent tooltip once a second. Each rebuild destroys and recreates a dozen
   * DOM nodes and forces a layout pass, every second, forever - which is a
   * steady cost paid for nothing, since the aircraft are the same aircraft and
   * have only moved.
   */
  useEffect(() => {
    const layer = layerRef.current;
    if (!layer) return;
    const existing = markersRef.current;
    const seen = new Set<string>();

    for (const contact of aircraft) {
      if (contact.latitude === null || contact.longitude === null) continue;
      seen.add(contact.icao);

      const pos: L.LatLngExpression = [contact.latitude, contact.longitude];
      const colour = contact.alert ? "#ff7a45" : "#e8b04b";
      const label = contact.callsign?.trim() || contact.icao;
      const html = `<svg viewBox="0 0 18 18" width="18" height="18"
                 style="transform: rotate(${contact.track ?? 0}deg)">
                 <path d="M9 1 L13 15 L9 12 L5 15 Z" fill="${colour}"
                   stroke="#0b0f13" stroke-width="0.8"/>
               </svg>`;
      const tooltip = `${label}${
        contact.altitude !== null ? ` · ${Math.round(contact.altitude)} m` : ""
      }${contact.speed !== null ? ` · ${Math.round(contact.speed)} kt` : ""}`;

      let entry = existing.get(contact.icao);
      if (!entry) {
        const marker = L.marker(pos, {
          icon: L.divIcon({
            className: "aircraft-icon",
            iconSize: [18, 18],
            iconAnchor: [9, 9],
            html,
          }),
          keyboard: false,
        }).addTo(layer);
        if (!compact) {
          marker.bindTooltip(tooltip, {
            permanent: true,
            direction: "right",
            offset: [8, 0],
            className: `aircraft-label${contact.alert ? " alert" : ""}`,
          });
        }
        entry = { marker };
        existing.set(contact.icao, entry);
      } else {
        entry.marker.setLatLng(pos);
        // Rewrite the icon's SVG in place; replacing the icon would recreate
        // the element and undo the point of this.
        const el = entry.marker.getElement();
        if (el && el.innerHTML !== html) el.innerHTML = html;
        if (!compact) entry.marker.setTooltipContent(tooltip);
      }

      // The proximity ring comes and goes with the alert flag.
      if (contact.alert && !entry.ring) {
        entry.ring = L.circleMarker(pos, {
          radius: 13,
          color: "#ff7a45",
          weight: 1,
          fill: false,
          interactive: false,
        }).addTo(layer);
      } else if (contact.alert && entry.ring) {
        entry.ring.setLatLng(pos);
      } else if (!contact.alert && entry.ring) {
        layer.removeLayer(entry.ring);
        entry.ring = undefined;
      }
    }

    // Anything that dropped off the feed.
    for (const [icao, entry] of existing) {
      if (seen.has(icao)) continue;
      layer.removeLayer(entry.marker);
      if (entry.ring) layer.removeLayer(entry.ring);
      existing.delete(icao);
    }
  }, [aircraft, compact]);

  if (config.latitude === null || config.longitude === null) {
    return <div className="not-permitted">Station has no location set</div>;
  }

  return (
    <div className="map-holder">
      <div ref={holderRef} className="map-canvas" />
      {!compact && config.basemaps.length > 1 && (
        <div className="basemap-switch" role="group" aria-label="Basemap">
          {config.basemaps.map((b) => (
            <button
              key={b.key}
              type="button"
              className={`basemap-btn${b.key === style ? " active" : ""}`}
              onClick={() => setStyle(b.key)}
            >
              {b.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Memoised. Telemetry arrives on several streams at about 1 Hz each, so the
 * console re-renders a few times a second; without this every panel re-rendered
 * on every frame regardless of whose data it was. The map is the expensive one -
 * reconciling it also re-ran its contact update.
 */
export const AdsbMap = memo(AdsbMapInner);
