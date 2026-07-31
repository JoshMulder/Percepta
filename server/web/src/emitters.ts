/**
 * `ADSB_EMITTER_TYPE` turned into something an operator can read at a glance.
 *
 * The station publishes `emitter_type` as the raw integer the receiver sent and
 * deliberately does not name it (`station/gsu/sensors/__init__.py`). Naming is a
 * presentation decision and belongs here, where it can change without a station
 * deploy and without the wire format acquiring an opinion.
 *
 * **0 is not "unknown category".** It is the transponder saying it was never
 * configured with one, which is common on general aviation and says nothing
 * about the aircraft. It gets the neutral glyph, the same as a code this build
 * does not recognise, but the panel words the two differently — one is a silent
 * transponder and the other is a gap in this console.
 *
 * Values 8, 13, 15, 16 and 20+ are unassigned, space vehicles, or the cluster
 * and line obstacle types the station will not see from a single receiver. They
 * fall through to the neutral glyph rather than being invented.
 */

export type EmitterKind =
  | "unknown"
  | "light"
  | "small"
  | "large"
  | "heavy"
  | "agile"
  | "rotorcraft"
  | "glider"
  | "lighter-than-air"
  | "parachute"
  | "ultralight"
  | "uav"
  | "surface"
  | "obstacle";

/** MAVLink `ADSB_EMITTER_TYPE`, as in the uAvionix ICD. */
const KIND_BY_CODE: Record<number, EmitterKind> = {
  0: "unknown",
  1: "light", // < 15 500 lb
  2: "small", // 15 500 – 75 000 lb
  3: "large", // 75 000 – 300 000 lb
  4: "large", // high-vortex large (B757). Same silhouette, different wake.
  5: "heavy", // > 300 000 lb
  6: "agile", // highly manoeuvrable, > 5g and > 400 kt
  7: "rotorcraft",
  9: "glider",
  10: "lighter-than-air",
  11: "parachute",
  12: "ultralight",
  14: "uav",
  17: "surface", // emergency vehicle
  18: "surface", // service vehicle
  19: "obstacle", // point obstacle
  20: "obstacle", // cluster obstacle
  21: "obstacle", // line obstacle
};

export function emitterKind(code: number | null | undefined): EmitterKind {
  if (code === null || code === undefined) return "unknown";
  return KIND_BY_CODE[code] ?? "unknown";
}

/** What the panel writes out. Distinguishes "the transponder did not say" from
 *  "this build does not know that code", because they call for different
 *  actions and only one of them is ours to fix. */
export function emitterLabel(code: number | null | undefined): string {
  if (code === null || code === undefined) return "not reported";
  const named: Record<number, string> = {
    0: "not set by the transponder",
    1: "Light",
    2: "Small",
    3: "Large",
    4: "Large, high vortex",
    5: "Heavy",
    6: "High performance",
    7: "Rotorcraft",
    9: "Glider",
    10: "Lighter than air",
    11: "Parachutist",
    12: "Ultralight",
    14: "Unmanned",
    17: "Surface, emergency",
    18: "Surface, service",
    19: "Point obstacle",
    20: "Cluster obstacle",
    21: "Line obstacle",
  };
  return named[code] ?? `Unrecognised (${code})`;
}

/**
 * The glyph, as SVG path data in an 18×18 box pointing north.
 *
 * Paths rather than an icon font for the same reason the map has no symbol
 * layer: a font is a fetch, and this console does not make one to a third party
 * just to draw an aeroplane. They are drawn nose-up because the marker is
 * rotated to the track, so "up" here means "the direction of travel".
 *
 * Silhouettes are distinguishable at 18px in peripheral vision, which is the
 * only size and the only attention they get. That rules out detail: a rotorcraft
 * is a cross with a disc, not a helicopter.
 */
const GLYPH: Record<EmitterKind, string> = {
  // Fixed wing, in three weights. Sweep and span carry the size class, so a
  // heavy reads as bigger than a light without a legend.
  light: "M9 2 L10.4 8 L15 10.6 L15 12 L10.4 10.8 L10.4 14 L12 15.6 L12 16.6 L9 15.6 L6 16.6 L6 15.6 L7.6 14 L7.6 10.8 L3 12 L3 10.6 L7.6 8 Z",
  small: "M9 1.4 L10.6 7.6 L16 10.4 L16 12 L10.6 10.6 L10.6 14 L12.4 15.8 L12.4 16.8 L9 15.8 L5.6 16.8 L5.6 15.8 L7.4 14 L7.4 10.6 L2 12 L2 10.4 L7.4 7.6 Z",
  large: "M9 1 L10.9 7.4 L16.6 10.2 L16.6 12.1 L10.9 10.5 L10.9 14.2 L13 16.2 L13 17.2 L9 16 L5 17.2 L5 16.2 L7.1 14.2 L7.1 10.5 L1.4 12.1 L1.4 10.2 L7.1 7.4 Z",
  heavy: "M9 0.8 L11.2 7.2 L17.2 10 L17.2 12.2 L11.2 10.4 L11.2 14.4 L13.6 16.5 L13.6 17.5 L9 16.2 L4.4 17.5 L4.4 16.5 L6.8 14.4 L6.8 10.4 L0.8 12.2 L0.8 10 L6.8 7.2 Z",
  // Delta. Fast and manoeuvrable reads as a dart, not a wing.
  agile: "M9 1 L14.5 16 L9 12.4 L3.5 16 Z",
  // Top-down helicopter: a long main rotor across a slim fuselage with a tail
  // boom. The first attempt was a filled rotor disc over a body, which at 18px
  // collapsed into a lightbulb — a disc and a stub read as one blob at that
  // size, and the thing that says "helicopter" is the rotor being wider than
  // the aircraft, which only a line can show.
  rotorcraft: "M2 6.4 H16 M9 3.4 V14.6 M6.6 14.6 H11.4 M7.4 6.9 a1.6 2.4 0 1 0 3.2 0 a1.6 2.4 0 1 0 -3.2 0",
  // Long thin wings, no engine nacelles.
  glider: "M9 3 L9.8 9.4 L17 11 L17 12 L9.8 11.2 L9.8 15 L11 16.4 L11 17.2 L9 16.4 L7 17.2 L7 16.4 L8.2 15 L8.2 11.2 L1 12 L1 11 L8.2 9.4 Z",
  // Envelope with a gondola.
  "lighter-than-air": "M9 2 C12.6 2 14.6 5.4 14.6 9 C14.6 12.6 12.6 15 9 15 C5.4 15 3.4 12.6 3.4 9 C3.4 5.4 5.4 2 9 2 Z M7.6 15.2 h2.8 v1.8 h-2.8 Z",
  // Canopy over a figure.
  parachute: "M2.6 8.4 A6.4 6.4 0 0 1 15.4 8.4 Z M2.6 8.4 L9 13.4 L15.4 8.4 M9 13.4 v3.4",
  // A hang-glider/paraglider delta seen from above. Previously a thin
  // straight-winged shape, which at 18px was the glider glyph again — two
  // categories sharing one silhouette is worse than not distinguishing them,
  // because it looks like information. A swept wing shares nothing with the
  // long straight span above it.
  ultralight: "M9 3.4 L16.4 14.2 L9 11.6 L1.6 14.2 Z M9 11.6 V16.2",
  // Quad-ish X with a body: the shape people read as a drone.
  uav: "M9 6.4 A2.6 2.6 0 1 1 8.99 6.4 Z M3.4 3.4 L6.9 6.9 M14.6 3.4 L11.1 6.9 M3.4 14.6 L6.9 11.1 M14.6 14.6 L11.1 11.1 M2.2 2.2 a1.6 1.6 0 1 0 0.01 0 Z M15.8 2.2 a1.6 1.6 0 1 0 0.01 0 Z M2.2 15.8 a1.6 1.6 0 1 0 0.01 0 Z M15.8 15.8 a1.6 1.6 0 1 0 0.01 0 Z",
  // Ground vehicles do not fly, so they get no nose and are never rotated.
  surface: "M3.4 7.6 h7.2 l2.4 2.6 h1.6 v3.4 h-11.2 Z M5.6 15.4 a1.5 1.5 0 1 0 0.01 0 Z M12.4 15.4 a1.5 1.5 0 1 0 0.01 0 Z",
  // Outlined, not filled, and that is the whole reason: filled, it was the
  // ultralight delta again at 18px. An outline also matches how a chart draws
  // an obstacle, and these are the only static things on the map.
  obstacle: "M9 2.6 L15.2 14.6 H2.8 Z M9 8.4 V12",
  // Neutral: a plain diamond. Not an aeroplane, because asserting a category
  // the receiver did not send is exactly the sort of confident guess the rest
  // of this system refuses to make.
  unknown: "M9 3.2 L14.4 9 L9 14.8 L3.6 9 Z",
};

/** Whether the glyph should be rotated to the reported track.
 *
 * A ground vehicle, an obstacle and a balloon have no meaningful heading in
 * this view, and spinning them to a track the receiver may not even have sent
 * would be motion that means nothing. */
export function rotates(kind: EmitterKind): boolean {
  return kind !== "surface" && kind !== "obstacle" && kind !== "lighter-than-air";
}

export function glyphPath(kind: EmitterKind): string {
  return GLYPH[kind];
}

/** `parachute`, `uav` and `rotorcraft` are strokes, not filled silhouettes — a
 *  filled canopy reads as a blob, a filled quad as a smear, and a filled rotor
 *  disc as a lightbulb. */
export function isStroked(kind: EmitterKind): boolean {
  return (
    kind === "parachute"
    || kind === "uav"
    || kind === "rotorcraft"
    || kind === "obstacle"
  );
}

/**
 * Rendered size in pixels, which is what actually separates the weight classes.
 *
 * Drawn at one size, `light` through `heavy` are four near-identical
 * aeroplanes: the sweep and span differences are real in the path data and
 * invisible at 18px, which is the only size this is ever seen at. Four glyphs
 * nobody can tell apart is worse than one, because it looks like information.
 *
 * Scaling the marker instead makes the distinction pre-attentive — a heavy is
 * simply a bigger aircraft than a light, which is also true. The range is
 * deliberately narrow: much beyond this and a distant heavy starts obscuring
 * the contacts around it, and size stops meaning weight and starts meaning
 * importance.
 */
/** Everything scaled together. The glyphs were drawn to be legible at a glance
 *  and were still too small against satellite imagery — half again as large
 *  keeps every ratio between the weight classes and just makes them findable. */
const SCALE = 1.5;

export function glyphSize(kind: EmitterKind): number {
  return Math.round(baseGlyphSize(kind) * SCALE);
}

function baseGlyphSize(kind: EmitterKind): number {
  switch (kind) {
    case "light":
      return 14;
    case "small":
      return 17;
    case "large":
      return 20;
    case "heavy":
      return 23;
    // Small and fast. Reading as a dart rather than as a big aircraft is the
    // point, so it stays under the airliners.
    case "agile":
      return 15;
    case "parachute":
    case "ultralight":
      return 16;
    default:
      return 18;
  }
}
