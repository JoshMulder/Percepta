import { describe, expect, it } from "vitest";
import {
  emitterKind,
  emitterLabel,
  glyphPath,
  glyphSize,
  isStroked,
  rotates,
  type EmitterKind,
} from "./emitters";

/**
 * The emitter-type mapping.
 *
 * There is no node on the host, so these run in a container the same way the
 * console is built:
 *
 *   docker run --rm -v "$PWD:/w" -w /w node:22-slim \
 *     sh -c 'npm install --silent && npm test'
 *
 * from `server/web`. Nothing is installed on the host and `npm install` writes
 * only into the working tree, which is gitignored.
 *
 * Two kinds of thing are worth pinning here and nothing else is. The first is
 * agreement with `ADSB_EMITTER_TYPE` in the uAvionix ICD, because a typo in the
 * table silently mislabels real aircraft and nothing downstream can catch it.
 * The second is the set of internal-consistency properties that make the table
 * safe to extend: every code must reach a kind that has a glyph, every glyph
 * must be drawable, and the weight classes must stay ordered.
 *
 * Deliberately not tested: the shape of any particular silhouette. That is a
 * visual judgement — three of them were redrawn after being looked at, not
 * after failing an assertion — and a test asserting path data would only pin
 * the drawing against being improved.
 */

/** Every kind the module can produce, so the coverage tests below cannot go
 *  stale by someone adding a kind and not adding it here. */
const ALL_KINDS: EmitterKind[] = [
  "unknown", "light", "small", "large", "heavy", "agile", "rotorcraft",
  "glider", "lighter-than-air", "parachute", "ultralight", "uav", "surface",
  "obstacle",
];

describe("the ICD mapping", () => {
  it("agrees with ADSB_EMITTER_TYPE", () => {
    // Spot-checked against the categories whose glyph differs, because those
    // are the ones a transposition would visibly break.
    expect(emitterKind(1)).toBe("light");
    expect(emitterKind(2)).toBe("small");
    expect(emitterKind(3)).toBe("large");
    expect(emitterKind(5)).toBe("heavy");
    expect(emitterKind(6)).toBe("agile");
    expect(emitterKind(7)).toBe("rotorcraft");
    expect(emitterKind(9)).toBe("glider");
    expect(emitterKind(10)).toBe("lighter-than-air");
    expect(emitterKind(11)).toBe("parachute");
    expect(emitterKind(12)).toBe("ultralight");
    expect(emitterKind(14)).toBe("uav");
  });

  it("draws high-vortex large as a large, which is a decision not an oversight", () => {
    // 4 is "high vortex large" — a B757. The wake matters to a controller
    // sequencing approaches and not at all to this console, and the silhouette
    // is a large airliner either way.
    expect(emitterKind(4)).toBe("large");
    expect(emitterKind(4)).toBe(emitterKind(3));
    // The label still keeps them apart, because the panel has room to.
    expect(emitterLabel(4)).not.toBe(emitterLabel(3));
  });

  it("puts both surface categories and all three obstacles on one glyph", () => {
    expect(emitterKind(17)).toBe("surface"); // emergency vehicle
    expect(emitterKind(18)).toBe("surface"); // service vehicle
    for (const code of [19, 20, 21]) expect(emitterKind(code)).toBe("obstacle");
  });
});

describe("codes with no aircraft behind them", () => {
  it("treats 0 as a category, not as an aircraft", () => {
    // A transponder that was never configured with a category. Common on
    // general aviation, and drawing it as an aeroplane would assert something
    // the receiver did not send.
    expect(emitterKind(0)).toBe("unknown");
  });

  it("falls back rather than inventing a silhouette", () => {
    // 8, 13 and 16 are unassigned in the ICD; 15 is space/trans-atmospheric,
    // which a ground station will not see. 99 and 255 stand for anything a
    // future revision adds.
    for (const code of [8, 13, 15, 16, 22, 99, 255]) {
      expect(emitterKind(code)).toBe("unknown");
    }
  });

  it("treats a missing field as missing", () => {
    // The station sends null when the receiver's flag was clear. That must not
    // become code 0, which means something else.
    expect(emitterKind(null)).toBe("unknown");
    expect(emitterKind(undefined)).toBe("unknown");
  });
});

describe("labels", () => {
  it("distinguishes all three ways a type can be absent", () => {
    // These call for different actions and only one of them is ours to fix,
    // so they must not collapse into one wording.
    const notSent = emitterLabel(null);
    const notConfigured = emitterLabel(0);
    const notRecognised = emitterLabel(99);
    expect(new Set([notSent, notConfigured, notRecognised]).size).toBe(3);
    expect(notSent).toMatch(/not reported/i);
    expect(notConfigured).toMatch(/transponder/i);
    // The code is carried, because it is the only thing that makes this
    // actionable — somebody has to look it up.
    expect(notRecognised).toContain("99");
  });

  it("names every code it claims to know", () => {
    for (const code of [1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 14, 17, 18, 19, 20, 21]) {
      const label = emitterLabel(code);
      expect(label.length).toBeGreaterThan(0);
      expect(label).not.toMatch(/unrecognised/i);
    }
  });
});

describe("glyphs", () => {
  it("has one for every kind", () => {
    // The regression this file most exists to prevent: adding a kind to the
    // table and forgetting to draw it, which renders an empty `d` and puts an
    // invisible contact on the map.
    for (const kind of ALL_KINDS) {
      const d = glyphPath(kind);
      expect(d, kind).toBeTruthy();
      expect(d.startsWith("M"), `${kind} should start with a moveto`).toBe(true);
      expect(d, kind).not.toMatch(/NaN|undefined/);
    }
  });

  it("has one for every code a receiver can send", () => {
    // Stronger than the above: walks the whole byte range through the real
    // mapping, so an entry pointing at a kind with no glyph cannot survive.
    for (let code = 0; code <= 255; code++) {
      expect(glyphPath(emitterKind(code)), `code ${code}`).toBeTruthy();
    }
  });

  it("is drawn at the scale of the 18-unit viewBox", () => {
    // Weaker than "inside the viewBox", and named for what it actually checks.
    // A path's numbers are not all coordinates — the rotorcraft's body is an
    // elliptical arc whose `-3.2` is a *relative* offset, and arcs also carry
    // radii and two flags — so bounding the raw numbers at 0..18 fails on
    // correct drawings. Deciding the real question needs a path walker or a
    // DOM `getBBox`, and neither is worth it here: the failure this guards
    // against is a typo of scale, `160` for `16`, which puts the glyph
    // entirely off the marker. Shape is checked by looking at it.
    for (const kind of ALL_KINDS) {
      const numbers = (glyphPath(kind).match(/-?\d+(\.\d+)?/g) ?? []).map(Number);
      expect(numbers.length, kind).toBeGreaterThan(0);
      for (const n of numbers) {
        expect(Math.abs(n), `${kind} has ${n}, which is off this scale`)
          .toBeLessThanOrEqual(20);
      }
    }
  });
});

describe("size carries the weight class", () => {
  it("is strictly ordered from light to heavy", () => {
    // The property the whole redesign rests on. Drawn at one size these four
    // are the same aeroplane four times — the sweep differences are real in
    // the path data and invisible at 18px — so size is the only thing telling
    // them apart. If this ever ties, the distinction is gone and nothing else
    // would notice.
    const light = glyphSize("light");
    const small = glyphSize("small");
    const large = glyphSize("large");
    const heavy = glyphSize("heavy");
    expect(light).toBeLessThan(small);
    expect(small).toBeLessThan(large);
    expect(large).toBeLessThan(heavy);
  });

  it("keeps the range narrow enough that size still means weight", () => {
    // Much beyond this and a heavy starts obscuring the contacts around it,
    // at which point size reads as importance rather than as weight.
    const sizes = ALL_KINDS.map(glyphSize);
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(12);
    expect(Math.max(...sizes)).toBeLessThanOrEqual(26);
    expect(Math.max(...sizes) / Math.min(...sizes)).toBeLessThan(2);
  });

  it("keeps the fast, small things under the airliners", () => {
    // A delta reading as a bigger aircraft than a 737 would be backwards.
    expect(glyphSize("agile")).toBeLessThan(glyphSize("large"));
    expect(glyphSize("ultralight")).toBeLessThan(glyphSize("large"));
  });
});

describe("rotation", () => {
  it("does not spin things that have no heading in this view", () => {
    // A ground vehicle, an obstacle and a balloon rotated to a track would be
    // motion that means nothing — and for an obstacle, a track the receiver
    // never sent at all.
    expect(rotates("surface")).toBe(false);
    expect(rotates("obstacle")).toBe(false);
    expect(rotates("lighter-than-air")).toBe(false);
  });

  it("rotates everything that flies", () => {
    for (const kind of ALL_KINDS) {
      if (kind === "surface" || kind === "obstacle" || kind === "lighter-than-air") {
        continue;
      }
      expect(rotates(kind), kind).toBe(true);
    }
  });
});

describe("stroked glyphs", () => {
  it("are the ones drawn as lines rather than silhouettes", () => {
    // Each of these was filled first and each read as a blob: a canopy, a
    // quad, a rotor disc, and an obstacle triangle indistinguishable from the
    // ultralight delta.
    for (const kind of ["parachute", "uav", "rotorcraft", "obstacle"] as const) {
      expect(isStroked(kind), kind).toBe(true);
    }
  });

  it("leaves the aircraft silhouettes filled", () => {
    for (const kind of ["light", "small", "large", "heavy", "agile", "glider",
      "ultralight", "surface", "unknown"] as const) {
      expect(isStroked(kind), kind).toBe(false);
    }
  });

  it("covers every kind either way", () => {
    // Both branches set different SVG attributes in AdsbMap; a kind that is
    // neither would inherit whatever the previous contact left behind.
    for (const kind of ALL_KINDS) {
      expect(typeof isStroked(kind), kind).toBe("boolean");
    }
  });
});
