import maplibregl from "maplibre-gl";
import { memo, useEffect, useRef, useState } from "react";
import { iconFor, isRotorcraft } from "../adsbIcons";
import { cachedAircraftInfo, fetchAircraftInfo } from "../aircraftInfo";
import { buildLabel } from "../adsbLabel";
import { isCritical, useDisplayPrefs } from "../displayPrefs";
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
// A true red for a proximity alert, deliberately distinct from the amber
// `--danger` used for faults elsewhere: a fault is "something is wrong with the
// station", a close contact is "something is in the air right here", and they
// should not read as the same colour.
/** Alerting contacts break that convention on purpose. */
const ALERT_COLOUR = "#ff2d2d";

/** Applied on top of tar1090's per-shape scale. Their sizes assume a map that
 *  fills a screen; this one is often a third of one. */
const DISPLAY_SCALE = 0.9;

/**
 * How long a contact's track is kept after it was last heard.
 *
 * The station sends no position history — each ADS-B frame is a snapshot — so
 * the track an operator sees on click is built here, from every contact's
 * fixes as the frames arrive. A track is held from the first time a contact is
 * seen and for an hour after it was last seen, so one that drops out and
 * returns, or is clicked minutes after it left the airspace, still has its
 * path; after the hour it is dropped rather than kept forever.
 */
const TRAIL_TTL_MS = 60 * 60 * 1000;

/**
 * The zoom each map was last left at, kept across the remount a swap forces.
 *
 * Console keys this component on its size (`stationId-s` / `stationId-m`), so
 * moving airspace between the main stage and the sidebar preview tears the map
 * down and builds a new one — which would otherwise snap back to the default
 * zoom, losing the level the operator had set. Module-level so it outlives that
 * unmount; keyed per station so a different station starts from its default
 * rather than inheriting the last one's zoom. Saved on unmount, read on mount.
 */
const zoomMemory = new Map<string, number>();

/** One contact's track: its fixes oldest-first, and when it was last heard so
 *  the store can drop it an hour later (see TRAIL_TTL_MS). */
interface Track {
  points: [number, number][];
  lastSeen: number;
}

/** Where the tracks are persisted across reloads. Versioned so a future change
 *  to the stored shape can be ignored rather than mis-parsed. */
const TRACK_STORAGE_KEY = "percepta.adsb.tracks.v1";
/** Persisted at most this often. A reload loses at most this much of the newest
 *  tail — imperceptible — and it keeps a stringify of the whole store off the
 *  once-a-second render path. */
const TRACK_PERSIST_MS = 2000;

/** The store as it goes to storage: plain objects, since JSON cannot carry a
 *  Map. Station id → ICAO → track. */
type StoredTracks = Record<string, Record<string, Track>>;

/**
 * Rebuild the store from localStorage, dropping anything past its hour.
 *
 * Called once, at module load, so a reload comes back to the same tracks. The
 * TTL is applied here too: a tab reopened the next morning must not resurrect a
 * day-old track, and pruning on the way in is what stops the stored blob
 * growing without bound across sessions. Any failure — no storage, corrupt
 * JSON, a shape from an older build — starts empty rather than throwing on the
 * module's first import.
 */
function loadTrailStore(): Map<string, Map<string, Track>> {
  const store = new Map<string, Map<string, Track>>();
  try {
    const raw = localStorage.getItem(TRACK_STORAGE_KEY);
    if (!raw) return store;
    const now = Date.now();
    const parsed = JSON.parse(raw) as StoredTracks;
    for (const [stationId, tracks] of Object.entries(parsed)) {
      const m = new Map<string, Track>();
      for (const [icao, t] of Object.entries(tracks)) {
        if (
          t &&
          Array.isArray(t.points) &&
          typeof t.lastSeen === "number" &&
          now - t.lastSeen <= TRAIL_TTL_MS
        ) {
          m.set(icao, { points: t.points, lastSeen: t.lastSeen });
        }
      }
      if (m.size) store.set(stationId, m);
    }
  } catch {
    return new Map();
  }
  return store;
}

/** Write the store back. Best-effort: a full or unavailable localStorage must
 *  not take the live map down with it, so a failure is swallowed — the tracks
 *  are still in memory, only the reload copy is missed. */
function saveTrailStore(store: Map<string, Map<string, Track>>): void {
  try {
    const plain: StoredTracks = {};
    for (const [stationId, tracks] of store) {
      const obj: Record<string, Track> = {};
      for (const [icao, t] of tracks) obj[icao] = t;
      plain[stationId] = obj;
    }
    localStorage.setItem(TRACK_STORAGE_KEY, JSON.stringify(plain));
  } catch {
    /* quota exceeded or storage unavailable — persistence is best-effort */
  }
}

/**
 * Every contact's track, by station then ICAO address.
 *
 * Module-level for the same reason as zoomMemory: Console keys this component
 * on its size, so swapping airspace between the main stage and the sidebar
 * preview remounts it — and a track held in component state would reset on that
 * swap, losing history the operator was mid-way through building. Keyed by
 * station so switching stations does not cross one airspace's tracks into
 * another's. Accumulated for every contact, not only the selected one, because
 * the operator may click a contact long after it first appeared and expect its
 * whole path. Hydrated from localStorage so a full page reload keeps it too,
 * and written back on a throttle from the update effect.
 */
const trailStore = loadTrailStore();
/** When the store was last written, so the effect can throttle persistence. */
let trailPersistedAt = 0;

function tracksFor(stationId: string): Map<string, Track> {
  let m = trailStore.get(stationId);
  if (!m) {
    m = new Map();
    trailStore.set(stationId, m);
  }
  return m;
}

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
  const prefs = useDisplayPrefs();
  const wantRegistration = prefs.labelFields.includes("registration");
  /** Bumped when a batch of registration lookups resolves, purely to re-run the
   *  marker loop so it re-reads the now-populated cache. The registrations
   *  themselves live in the shared `aircraftInfo` cache, not here. */
  const [regVersion, setRegVersion] = useState(0);
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
    // Per station and per size (main / small preview), which is exactly how
    // Console keys this component — so each remembers its own last zoom.
    const zoomKey = `${stationId}-${compact ? "s" : "m"}`;

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
              // The provider's credit, carried on the source so MapLibre's
              // attribution control shows it — and shows only the one whose
              // basemap is currently visible. Esri's terms (like every tile
              // provider's) require it to be displayed on the map, not buried in
              // a tooltip, which is all it was before.
              attribution: b.attribution,
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
      // The zoom this size was last left at, or the default for a first mount.
      // Restoring it is what makes a swap to the preview and back return to the
      // level the operator had, rather than resetting (see zoomMemory).
      zoom: Math.min(
        maxZoom,
        Math.max(config.min_zoom, zoomMemory.get(zoomKey) ?? (compact ? 9 : 12)),
      ),
      minZoom: config.min_zoom,
      maxZoom,
      // On, and compact. The provider's credit is a licence condition — Esri's
      // imagery terms and OSM's ODbL both require it — so it is never removed;
      // compact collapses it to the standard "i" that expands on click, which is
      // how MapLibre and every major consumer of these tiles present it, and is
      // what FleetMap already used. This was `compact: false` deliberately, on
      // the stricter reading that the credit should always be legible; the panel
      // is small enough that a permanent text box was reading as clutter over
      // the picture. Fed from each source's `attribution` above; MapLibre
      // displays only the visible one.
      attributionControl: { compact: true },
      // Centred on the station, but the operator may drag away to look around;
      // it eases back to centre five seconds after they let go (the dragend
      // handler below). Rotation, pitch and double-click zoom stay off, so a
      // deliberate pan is the only way it ever leaves centre — and a temporary
      // one.
      dragPan: true,
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

    // Pan-and-return. Dragging is on so the operator can look around, but the
    // map belongs to the station, so a drift away is temporary: five seconds
    // after they release, it eases back to centre. A new drag cancels a pending
    // return (dragstart) and each release restarts the count (dragend), so a run
    // of pans never snaps out from under the pointer mid-look.
    let recentreTimer: ReturnType<typeof setTimeout> | undefined;
    const clearRecentre = () => {
      if (recentreTimer !== undefined) {
        clearTimeout(recentreTimer);
        recentreTimer = undefined;
      }
    };
    map.on("dragstart", clearRecentre);
    map.on("dragend", () => {
      clearRecentre();
      recentreTimer = setTimeout(() => {
        map.easeTo({ center: centre, duration: 600 });
      }, 5000);
    });

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

      // The clicked contact's track. Empty until a contact is clicked; the
      // effect below fills it with that contact's trail and keeps its leading
      // end on the aircraft as it moves. A dark halo under a bright line, the
      // same two-pass trick the rings use to stay legible over satellite
      // imagery — and rounded joins so a turning track does not show mitres.
      map.addSource("contact-trail", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "contact-trail-halo",
        type: "line",
        source: "contact-trail",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#050a0e",
          "line-width": 4,
          "line-opacity": 0.5,
          "line-blur": 1,
        },
      });
      map.addLayer({
        id: "contact-trail",
        type: "line",
        source: "contact-trail",
        layout: { "line-cap": "round", "line-join": "round" },
        // Colour is set per update to match the marker — gold, or red when the
        // contact is close and low — so the track reads as the same object.
        paint: {
          "line-color": CONTACT_COLOUR,
          "line-width": 2,
          "line-opacity": 0.9,
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
          new maplibregl.Marker({ element: el, subpixelPositioning: true })
            .setLngLat([lon, lat + km / 110.574])
            .addTo(map);
        }
      }

      const dot = document.createElement("div");
      dot.className = "map-station";
      new maplibregl.Marker({ element: dot, subpixelPositioning: true })
        .setLngLat(centre)
        .addTo(map);
    });

    return () => {
      readyRef.current = false;
      clearRecentre();
      // Remember where this size was left, so the sibling that mounts on a swap
      // comes up at the same zoom rather than the default.
      zoomMemory.set(zoomKey, map.getZoom());
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
   * Registrations for the labels, but only when the operator has asked for them.
   *
   * Registration is not in the ADS-B stream — it is a per-contact lookup, and
   * putting it on the label means looking up every aircraft on screen rather
   * than only the one whose card is open. So the lookups are gated on the field
   * being selected: nobody pays for tail numbers on the map unless they turned
   * them on. The results land in the shared cache; the version bump just tells
   * the marker loop below to re-read it once a batch has resolved.
   */
  useEffect(() => {
    if (!wantRegistration) return;
    let cancelled = false;
    const pending = aircraft
      .filter((c) => cachedAircraftInfo(c.icao) === undefined)
      .map((c) => fetchAircraftInfo(c.icao).catch(() => undefined));
    if (pending.length) {
      void Promise.allSettled(pending).then(() => {
        if (!cancelled) setRegVersion((v) => v + 1);
      });
    }
    return () => {
      cancelled = true;
    };
  }, [aircraft, wantRegistration]);

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
    const trails = tracksFor(stationId);
    const now = Date.now();
    const seen = new Set<string>();

    for (const contact of aircraft) {
      if (contact.latitude === null || contact.longitude === null) continue;
      seen.add(contact.icao);

      const pos: [number, number] = [contact.longitude, contact.latitude];

      // Grow this contact's track, and stamp it heard so the store keeps it for
      // the hour after. A fix is appended only when the contact has actually
      // moved: this effect also re-runs on selection and prefs changes, when
      // the positions are the same objects as last time, and a contact holding
      // station adds nothing — so a fix identical to the last is dropped. No
      // length cap: the track is the whole path since the contact first
      // appeared, and dropping only unmoved fixes keeps even a long flight
      // bounded by how much it turned rather than by how long it was in view.
      let track = trails.get(contact.icao);
      if (!track) {
        track = { points: [], lastSeen: now };
        trails.set(contact.icao, track);
      }
      track.lastSeen = now;
      const last = track.points[track.points.length - 1];
      if (!last || last[0] !== pos[0] || last[1] !== pos[1]) {
        track.points.push(pos);
      }

      // The operator's own close-and-low threshold, not the station's alert
      // flag: this is the console's view of what is worth drawing red.
      const critical = isCritical(contact.range_km, contact.altitude_m, prefs);
      const colour = critical ? ALERT_COLOUR : CONTACT_COLOUR;
      // A resolved lookup with no tail number falls back to the callsign — the
      // operator asked the flight number to stand in when the registry has
      // nothing. `cached` is undefined only while the lookup is pending, so the
      // field stays blank until there is an answer rather than flashing the
      // callsign and then replacing it.
      const cached = wantRegistration
        ? cachedAircraftInfo(contact.icao)
        : undefined;
      const registration = cached
        ? cached.registration ?? contact.callsign?.trim() ?? null
        : null;
      const label = buildLabel(contact, prefs, registration);
      const icon = iconFor(contact.emitter_type);

      let marker = existing.get(contact.icao);
      if (!marker) {
        const el = document.createElement("div");
        el.className = "map-contact";
        // The glyph and, for a rotorcraft, a rotor disc that spins over it, in a
        // wrapper so the disc can be centred on the glyph rather than the whole
        // marker (the label sits below and would pull the centre down). The
        // rotor is inert markup until the `rotorcraft` class turns it on, so
        // every other contact carries a hidden, un-animated element and nothing
        // else. `querySelector("svg")` below still finds the glyph — it is the
        // first svg in document order. The rotor's own blades are two crossed
        // ellipses; spinning the svg blurs them into a disc, like FR24.
        el.innerHTML =
          "<div class='glyph-wrap'><svg></svg>" +
          "<svg class='rotor' viewBox='-16 -16 32 32' aria-hidden='true'>" +
          "<ellipse rx='15' ry='2.1'></ellipse>" +
          "<ellipse rx='2.1' ry='15'></ellipse></svg></div><span></span>";
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
          // Hover previews the contact — its card and its track — transiently:
          // it clears when the pointer leaves the glyph, so a card lingers only
          // once you CLICK to pin it. A centre-anchored wheel-zoom slides the
          // marker out from under a still pointer and so ends a bare hover; to
          // hold a contact across a zoom, click it — a pin survives both a zoom
          // and leaving the map.
          glyph?.addEventListener("pointerenter", () => {
            hoverRef.current(contact.icao);
          });
          glyph?.addEventListener("pointerleave", () => {
            hoverRef.current(null);
          });
          // Clicking pins rather than toggling: a second click on an already
          // open contact is far more often a missed drag than a request to
          // close, and Close is right there. A pin outlives even leaving the map.
          glyph?.addEventListener("click", (e) => {
            e.stopPropagation();
            pinRef.current(contact.icao);
          });
        }
        marker = new maplibregl.Marker({ element: el, subpixelPositioning: true }).setLngLat(pos).addTo(map);
        existing.set(contact.icao, marker);
      } else {
        marker.setLngLat(pos);
      }

      const el = marker.getElement();
      el.classList.toggle("alert", critical);
      el.classList.toggle("selected", contact.icao === selected);
      // A helicopter gets a spinning rotor; everything else keeps the hidden,
      // un-animated rotor element it was built with. Toggled every update
      // because a transponder can begin reporting a category it was not before.
      el.classList.toggle("rotorcraft", isRotorcraft(contact.emitter_type));
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
        // A critical contact's label wears the red badge, and its text must be
        // the white the stylesheet gives it — NOT this inline colour, which is
        // the same red and would paint red-on-red, the unreadable state this
        // fixes. Cleared so `.map-contact.alert span` (white) wins; a normal
        // contact keeps the gold inline, and selection still overrides via
        // `!important`. The glyph below stays `colour` either way.
        span.style.color = critical ? "" : colour;
      }
    }

    // A contact that has left the airspace loses its marker — but not its
    // track: that is kept in the store for the hour after it was last heard, so
    // the same aircraft returning continues one line rather than starting a new
    // one. The marker is DOM and goes now; the track is dropped by time below.
    for (const [icao, marker] of existing) {
      if (seen.has(icao)) continue;
      marker.remove();
      existing.delete(icao);
    }

    // Drop tracks not heard for the last hour, across every station in the
    // store — not just this one — so the persisted copy stays bounded rather
    // than carrying the airspaces of stations no longer open. Runs on every
    // frame rather than a timer: frames arrive about a second apart while a
    // station is open, which is precisely when the store is worth trimming, and
    // it saves carrying an interval per map.
    for (const [, tracks] of trailStore) {
      for (const [icao, track] of tracks) {
        if (now - track.lastSeen > TRAIL_TTL_MS) tracks.delete(icao);
      }
    }

    // Persist for the next page load, on a throttle. The store is the whole
    // history, so this is stringified at most once every couple of seconds
    // rather than every frame — a reload loses only that much of the freshest
    // tail, which is not visible against a track that immediately resumes.
    if (now - trailPersistedAt > TRACK_PERSIST_MS) {
      trailPersistedAt = now;
      saveTrailStore(trailStore);
    }

    // Draw the selected contact's track, or clear it. Follows selection —
    // hover or pin — so pointing at a contact previews where it came from and a
    // click just keeps it. A GeoJSON line in map coordinates, so it reprojects
    // itself on zoom and needs no `move` handler like the anchored panel does;
    // needs two fixes to be a line.
    const trailSource = map.getSource("contact-trail") as
      | maplibregl.GeoJSONSource
      | undefined;
    if (trailSource) {
      const chosen = selected ? aircraft.find((c) => c.icao === selected) : null;
      const path = selected ? trails.get(selected)?.points : undefined;
      if (chosen && path && path.length >= 2) {
        map.setPaintProperty(
          "contact-trail",
          "line-color",
          isCritical(chosen.range_km, chosen.altitude_m, prefs)
            ? ALERT_COLOUR
            : CONTACT_COLOUR,
        );
        trailSource.setData({
          type: "Feature",
          properties: {},
          geometry: { type: "LineString", coordinates: path },
        });
      } else {
        trailSource.setData({ type: "FeatureCollection", features: [] });
      }
    }
    // `prefs` re-labels on a units or field change; `regVersion` re-labels once
    // a batch of registration lookups has landed in the cache. `selected`
    // drives both the track and the marker's own highlight.
  }, [aircraft, compact, selected, prefs, regVersion, stationId]);

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
      {/* Clicking empty sky dismisses both the pin and the latched hover; the
          glyph handlers stop propagation, so this only fires on empty map.
          Leaving the map ends a latched hover (see the glyph handler) — a pin
          is left alone, as it should outlive the pointer leaving the map. */}
      <div
        ref={holderRef}
        className="map-canvas"
        onClick={() => {
          setPinned(null);
          setHovered(null);
        }}
        onPointerLeave={() => setHovered(null)}
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
          // The panel counts as part of what is being hovered, so moving onto
          // it keeps the selection on this contact. It does not clear on leave:
          // the latch is ended by leaving the map or clicking empty sky (see
          // the map holder), not by the pointer crossing off the panel — which
          // would otherwise drop the track as you moved between panel and map.
          onPointerEnter={() => setHovered(openContact.icao)}
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
