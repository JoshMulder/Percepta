import maplibregl from "maplibre-gl";
import { memo, useEffect, useRef, useState } from "react";
import { iconFor } from "../adsbIcons";
import type { Aircraft, MapConfig } from "../types";
import { ContactDetail } from "./ContactDetail";

/**
 * Range rings, in kilometres.
 *
 * A fixed set rather than a spacing derived from the cache radius. The rings
 * exist to answer "how far away is that" at the distances this station is
 * actually judged at, and evenly dividing a 40km tile-cache radius put the
 * first ring 10km out - past everything close enough to matter. The cache
 * radius is a storage decision and was never the right thing to scale these to.
 */
const RING_KM = [1, 2, 5, 8, 10];

/** A circle on the globe as a GeoJSON ring. Longitude degrees shrink with
 *  latitude, which is a large correction at the ~45S these stations sit at. */
function ringCoords(lat: number, lon: number, km: number, points = 128): number[][] {
  const latR = km / 110.574;
  const lonR = km / (111.32 * Math.cos((lat * Math.PI) / 180));
  const coords: number[][] = [];
  for (let i = 0; i <= points; i++) {
    const t = (i / points) * 2 * Math.PI;
    coords.push([lon + lonR * Math.cos(t), lat + latR * Math.sin(t)]);
  }
  return coords;
}

/**
 * Aircraft plotted on the station's basemap.
 *
 * MapLibre GL rather than Leaflet, matching DroneOps. The difference that
 * matters is the renderer, not the data: Leaflet composites raster tiles in the
 * DOM and snaps to whole zoom levels, so one wheel notch jumps a power of two
 * and the map lurches. MapLibre draws through WebGL and zooms continuously,
 * scaling the tiles it already has while the next ones arrive.
 *
 * Tiles still come from our own API, never straight from a provider. The server
 * serves them from its cache and fetches upstream once on a miss, so a second
 * viewer of the same station costs nothing, the map keeps working on a degraded
 * link, and opening a station tells no third party where a customer's site is or
 * when someone is watching it. DroneOps does the same thing - MapLibre with
 * plain raster sources, no vector tiles and no API key.
 *
 * **No glyphs, so no external fonts.** Text in a MapLibre symbol layer needs a
 * glyph server, and the usual one is a third-party URL the browser fetches at
 * view time - which would give away exactly the property the tile proxy exists
 * to protect. Every label here is an HTML marker instead: a DOM node each,
 * against not phoning home.
 *
 * The station stays at the centre: panning is off, and zoom is anchored on the
 * centre rather than the pointer. A ground station does not move, so the useful
 * frame is always the one around it - and a map that has been dragged somewhere
 * else is a map an operator has to re-orient before they can read it, which is
 * the wrong thing to be doing when something is happening.
 */
/** Flightradar24's aircraft gold. Matched deliberately: it is the colour
 *  anyone who has looked at a tracking map already reads as "aircraft", and
 *  this panel is not the place to be original. */
const CONTACT_COLOUR = "#f5c518";
/** Alerting contacts break that convention on purpose. */
const ALERT_COLOUR = "#ff7a45";

/** Applied on top of tar1090's per-shape scale. Their sizes assume a map that
 *  fills a screen; this one is often a third of one. */
const DISPLAY_SCALE = 0.9;

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
  const mapRef = useRef<maplibregl.Map | null>(null);
  /** Markers by ICAO address, reused between updates. */
  const markersRef = useRef(new Map<string, maplibregl.Marker>());
  const readyRef = useRef(false);
  const [style, setStyle] = useState(config.default_basemap);
  /** ICAO of the contact whose detail panel is open, or null.
   *
   *  Held as the address rather than the contact object so that the panel
   *  re-reads the live array every render: a selected aircraft is still moving,
   *  and a panel frozen at the values it had when clicked would quietly become
   *  wrong while being read. */
  const [pinned, setPinned] = useState<string | null>(null);
  /** The contact under the pointer. Separate from `pinned` so that reading a
   *  panel you deliberately opened is not interrupted by the pointer crossing
   *  something else on the way to it — a pin wins until it is dismissed. */
  const [hovered, setHovered] = useState<string | null>(null);
  const selected = pinned ?? hovered;
  /** So the marker handlers, which are created once per contact and live
   *  outside React, can set state without being rebuilt on every selection. */
  const pinRef = useRef(setPinned);
  pinRef.current = setPinned;
  const hoverRef = useRef(setHovered);
  hoverRef.current = setHovered;
  /** Where the open panel should sit, in screen pixels.
   *
   *  The panel used to be pinned bottom-left, which meant that on a map with
   *  several contacts nothing tied the numbers to the aircraft they described
   *  — you clicked one, read a panel in the corner, and had to remember which
   *  dot you had clicked. Anchored to the target it is unambiguous, and it has
   *  to keep up: the aircraft moves every second and the map moves on every
   *  zoom. */
  const [anchor, setAnchor] = useState<
    { x: number; y: number; below: boolean; left: boolean } | null
  >(null);

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
    if (!config.basemaps.length) return;

    const lat = config.latitude;
    const lon = config.longitude;
    const centre: [number, number] = [lon, lat];

    // Every basemap is a source in one style and switching toggles layer
    // visibility. The alternative - setStyle per switch - discards every custom
    // layer and marker and needs them re-added on styledata, which is a lot of
    // ceremony for three raster layers.
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
                `${window.location.origin}/api/stations/${stationId}/tiles/${b.key}/{z}/{x}/{y}.png`,
              ],
              tileSize: 256,
              // Past this MapLibre upscales the deepest tile it has rather than
              // requesting a level that 404s and renders blank.
              maxzoom: b.max_zoom,
            },
          ]),
        ),
        layers: config.basemaps.map((b) => ({
          id: `base-${b.key}`,
          type: "raster" as const,
          source: b.key,
          layout: { visibility: (b.key === style ? "visible" : "none") as "visible" | "none" },
        })),
      },
      center: centre,
      zoom: Math.min(maxZoom, Math.max(config.min_zoom, compact ? 9 : 12)),
      minZoom: config.min_zoom,
      maxZoom,
      attributionControl: false,
      // Locked to the station. Dragging, rotation, pitch and double-click zoom
      // are all off, so it cannot drift off centre by any route.
      dragPan: false,
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      keyboard: false,
      doubleClickZoom: false,
      maxPitch: 0,
    });
    mapRef.current = map;

    // Zoom is handled here rather than by the built-in scroll handler.
    //
    // MapLibre zooms toward the pointer, and `scrollZoom.enable({around:
    // "center"})` did not change that - measured, not assumed: six wheel steps
    // with the pointer off-centre walked the station 465px right and 324px down.
    // This map is pinned to its station, so that is not a preference.
    //
    // setZoom keeps the centre by definition. Applying it per wheel event is
    // still smooth because MapLibre renders continuously and scales the tiles
    // it already has - the smoothness comes from the renderer, not from an
    // animation wrapped around each step.
    map.scrollZoom.disable();
    map.touchZoomRotate.disable();
    let onWheel: ((e: WheelEvent) => void) | null = null;
    if (!compact) {
      onWheel = (e: WheelEvent) => {
        e.preventDefault();
        // deltaMode 1 is lines rather than pixels, which some mice and most of
        // Firefox report; without scaling it a single notch would jump levels.
        const px = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
        map.setZoom(map.getZoom() - px / 260);
      };
      holder.addEventListener("wheel", onWheel, { passive: false });
    }

    map.on("load", () => {
      readyRef.current = true;

      const features: GeoJSON.Feature[] = RING_KM.map((km) => ({
        type: "Feature" as const,
        properties: { km },
        geometry: {
          type: "LineString" as const,
          coordinates: ringCoords(lat, lon, km),
        },
      }));
      map.addSource("rings", {
        type: "geojson",
        data: { type: "FeatureCollection", features },
      });
      // Two passes: a dark halo under a bright line. Satellite imagery is
      // busy and varies from pale surf to near-black water, so a single
      // stroke that reads over one is invisible over the other - the old
      // 1px #2c3d49 dashes disappeared entirely against the sea, which is
      // most of this station's view. The halo gives the line something
      // constant to sit on, which is the same reason a chart draws contours
      // that way.
      map.addLayer({
        id: "rings-halo",
        type: "line",
        source: "rings",
        paint: {
          "line-color": "#050a0e",
          "line-width": 3.5,
          "line-opacity": 0.55,
          "line-blur": 1,
        },
      });
      map.addLayer({
        id: "rings",
        type: "line",
        source: "rings",
        paint: {
          "line-color": "#7fe3c0",
          "line-width": 1.4,
          "line-opacity": 0.75,
          "line-dasharray": [3, 3],
        },
      });

      // Ring labels, due north of the station. Without them the rings are
      // decoration: an operator can see something is about two rings out and
      // has no idea what that is in kilometres.
      if (!compact) {
        for (const km of RING_KM) {
          const el = document.createElement("div");
          el.className = "map-ring-label";
          el.textContent = `${km} km`;
          new maplibregl.Marker({ element: el })
            .setLngLat([lon, lat + km / 110.574])
            .addTo(map);
        }
      }

      const dot = document.createElement("div");
      dot.className = "map-station";
      new maplibregl.Marker({ element: dot }).setLngLat(centre).addTo(map);
    });

    return () => {
      readyRef.current = false;
      if (onWheel) holder.removeEventListener("wheel", onWheel);
      markersRef.current.clear();
      map.remove();
      mapRef.current = null;
    };
    // `style` is deliberately not a dependency: switching basemaps toggles
    // layer visibility below rather than rebuilding the map, which would throw
    // away the zoom the operator had chosen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stationId, config, compact, maxZoom]);

  // Basemap switch, and the dark treatment.
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
      // Applied to the canvas rather than per layer: MapLibre's raster paint
      // properties adjust brightness, saturation and hue but cannot invert, and
      // inversion is what turns a street map drawn for white paper into one that
      // belongs on this console. Imagery must never be inverted - it would show
      // false colour - which is why this follows the basemap and is not a theme.
      map.getCanvas().style.filter = basemap.invert_for_dark
        ? "invert(1) hue-rotate(180deg) brightness(0.88) contrast(0.95)"
        : "";
    };
    if (map.isStyleLoaded()) apply();
    else map.once("load", apply);
  }, [basemap, config.basemaps]);

  /**
   * Update contacts in place rather than clearing and rebuilding.
   *
   * Recreating every marker once a second destroys and recreates a dozen DOM
   * nodes and forces a layout pass, every second, forever - a steady cost paid
   * for nothing, since the aircraft are the same aircraft and have only moved.
   */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    const existing = markersRef.current;
    const seen = new Set<string>();

    for (const contact of aircraft) {
      if (contact.latitude === null || contact.longitude === null) continue;
      seen.add(contact.icao);

      const pos: [number, number] = [contact.longitude, contact.latitude];
      const colour = contact.alert ? ALERT_COLOUR : CONTACT_COLOUR;
      const label = contact.callsign?.trim() || contact.icao;
      const icon = iconFor(contact.emitter_type);

      let marker = existing.get(contact.icao);
      if (!marker) {
        const el = document.createElement("div");
        el.className = "map-contact";
        el.innerHTML = "<svg></svg><span></span>";
        // Pointer events are off for the marker as a whole (see STYLE) so the
        // label never eats a click meant for the map behind it; the glyph opts
        // back in — but only where there is a panel to open. In the mini
        // viewer the detail panel would cover most of the airspace it exists
        // to show, so contacts are not clickable there at all rather than
        // clickable and inert, which is what a cursor change would promise.
        // `compact` is fixed for the life of this map: Console keys the
        // component on it, so a swap remounts rather than mutating it.
        if (!compact) {
          el.classList.add("clickable");
          const glyph = el.querySelector("svg");
          // Hover shows, click keeps. Reading a contact is by far the common
          // case and asking for a click first made it a two-step; committing
          // to one so it survives the pointer moving away is the rarer
          // intent, so that is what the click is for.
          glyph?.addEventListener("pointerenter", () => {
            hoverRef.current(contact.icao);
          });
          glyph?.addEventListener("pointerleave", () => {
            // Only clears if it is still this contact: pointing straight from
            // one aircraft to another fires the leave after the next enter.
            hoverRef.current((h) => (h === contact.icao ? null : h));
          });
          // Clicking pins rather than toggling: a second click on an already
          // open contact is far more often a missed drag than a request to
          // close, and Close is right there.
          glyph?.addEventListener("click", (e) => {
            e.stopPropagation();
            pinRef.current(contact.icao);
          });
        }
        marker = new maplibregl.Marker({ element: el }).setLngLat(pos).addTo(map);
        existing.set(contact.icao, marker);
      } else {
        marker.setLngLat(pos);
      }

      const el = marker.getElement();
      el.classList.toggle("alert", Boolean(contact.alert));
      el.classList.toggle("selected", contact.icao === selected);
      // Ground vehicles and obstacles are not rotated at all, and a contact
      // that sent no track is left pointing north rather than being asserted
      // to be heading north — `track ?? 0` would have invented a heading.
      const svg = el.querySelector("svg") as SVGElement | null;
      if (svg) {
        // Rebuilt only when the silhouette itself changes, which is rare: a
        // transponder can begin reporting a category it was not reporting
        // before, but not on most frames. Re-parsing this markup for every
        // contact on every update is the expensive thing on this path.
        if (el.dataset.shape !== icon.name) {
          el.dataset.shape = icon.name;
          svg.setAttribute("viewBox", icon.viewBox);
          svg.innerHTML = icon.body;
        }
        // tar1090's own per-category scale, so a light aircraft is not drawn
        // the size of a widebody. `w` and `h` are the shape's natural display
        // size and are not equal — a ground vehicle seen from above is 7.2 by
        // 18 — so squaring them off turns the vans into slivers. The viewBox is
        // a different coordinate space again; see IconShape.
        const k = icon.scale * DISPLAY_SCALE;
        svg.setAttribute("width", String(Math.round(icon.w * k)));
        svg.setAttribute("height", String(Math.round(icon.h * k)));
        // Ground furniture and balloons have no meaningful heading, and a
        // contact that sent no track is left pointing north rather than being
        // asserted to be heading north — `track ?? 0` would invent a heading.
        const track =
          !icon.noRotate && contact.track_deg !== null ? contact.track_deg : 0;
        svg.style.transform = `rotate(${track}deg)`;
        // One property tints the whole silhouette: the shapes fill with
        // `currentColor` and keep their own dark outline, which is what makes
        // them legible over a city on satellite imagery.
        svg.style.color = colour;
      }
      const span = el.querySelector("span");
      if (span) {
        // Compact hides the label: at that size a dozen callsigns overlap into
        // an unreadable smear over the contacts they name.
        span.textContent = compact ? "" : label;
        span.style.color = colour;
      }
    }

    for (const [icao, marker] of existing) {
      if (seen.has(icao)) continue;
      marker.remove();
      existing.delete(icao);
    }
  }, [aircraft, compact, selected]);

  /** A selected contact that has left the airspace must not leave a panel of
   *  values behind that no longer describe anything. Handled here rather than
   *  in the marker loop so it also fires when the whole stream goes away. */
  useEffect(() => {
    if (selected && !aircraft.some((c) => c.icao === selected)) {
      setPinned(null);
      setHovered(null);
    }
  }, [aircraft, selected]);

  /** Keep the panel over its aircraft.
   *
   *  Recomputed from the contact's own coordinates rather than from the
   *  marker's current screen position, so a telemetry update and a zoom are
   *  the same event to this. `move` covers zooming — the map cannot be panned,
   *  so that is the only way the projection changes without new telemetry. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !selected) {
      setAnchor(null);
      return;
    }
    const place = () => {
      const contact = aircraft.find((c) => c.icao === selected);
      if (!contact || contact.latitude === null || contact.longitude === null) {
        setAnchor(null);
        return;
      }
      const point = map.project([contact.longitude, contact.latitude]);
      // Which side of the target the panel hangs from. Fixed at "above and to
      // the right" it went off the top of the map for anything in the upper
      // part of the sky, and the header — the callsign, the thing you clicked
      // to find out — was the first part to be lost.
      //
      // The map holder is the frame, so the decision is made against it rather
      // than the window: the compact viewer and the full page are different
      // sizes and a panel that fits one escapes the other.
      const frame = map.getContainer().getBoundingClientRect();
      setAnchor({
        x: point.x,
        y: point.y,
        below: point.y < frame.height / 2,
        // Later than half: the panel is much wider than it is off-centre, so
        // flipping at the midpoint sent it left while there was still room.
        left: point.x > frame.width * 0.62,
      });
    };
    place();
    map.on("move", place);
    return () => {
      map.off("move", place);
    };
  }, [selected, aircraft]);

  // Escape closes it, like every other overlay in the console. Bound only while
  // something is open so the console is not carrying a key listener per map for
  // the 99% of the time nothing is selected.
  useEffect(() => {
    if (!selected) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPinned(null);
        setHovered(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  if (config.latitude === null || config.longitude === null) {
    return <div className="not-permitted">Station has no location set</div>;
  }

  const openContact = selected
    ? aircraft.find((c) => c.icao === selected) ?? null
    : null;

  return (
    <div className="map-holder">
      {/* Clicking the map itself dismisses. The glyph handlers stop
          propagation, so this only fires on empty sky. */}
      <div
        ref={holderRef}
        className="map-canvas"
        onClick={() => setPinned(null)}
      />
      {!compact && config.basemaps.length > 1 && (
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
      {/* Not in compact: the mini viewer is a thumbnail, and a detail panel
          over it would cover most of the airspace it exists to show. Clicking
          a contact there is not offered rather than offered and useless. */}
      {!compact && openContact && anchor && (
        <div
          className={`contact-anchor${anchor.below ? " below" : ""}${
            anchor.left ? " left" : ""
          }`}
          // The panel counts as part of what is being hovered. Without this,
          // moving the pointer off the aircraft to read the panel dismissed
          // the panel on the way — the usual hover-menu gap, and the reason a
          // hover-opened panel has to hold itself open.
          onPointerEnter={() => setHovered(openContact.icao)}
          onPointerLeave={() => setHovered(null)}
          // Offset clear of the glyph so the panel does not cover the aircraft
          // it describes, on whichever side has the room — see `below`/`left`
          // above. `translate(-50%)` is deliberately not used: the panel is
          // wider than most of the map and centring it on a contact near an
          // edge pushes half of it off screen.
          style={{
            left: `${Math.round(anchor.x)}px`,
            top: `${Math.round(anchor.y)}px`,
          }}
        >
          <ContactDetail
            contact={openContact}
            onClose={() => {
              setPinned(null);
              setHovered(null);
            }}
          />
        </div>
      )}
    </div>
  );
}

export const AdsbMap = memo(AdsbMapInner);
