import { describe, expect, it } from "vitest";
import { SHAPES, iconFor } from "./adsbIcons";

/**
 * The generated icon set, checked for the things that broke while wiring it.
 *
 * These are not tests of tar1090's artwork — that is vendored and not ours to
 * assert about. They cover the seams: the emitter-category bridge, and three
 * properties of the generator's output that each rendered something wrong on
 * screen before they were fixed.
 */

/** Every MAVLink ADSB_EMITTER_TYPE the enum defines, including the gaps. */
const ALL_CODES = Array.from({ length: 20 }, (_, i) => i);

describe("picking a shape for what the transponder said", () => {
  it("gives every emitter type a shape rather than nothing", () => {
    for (const code of ALL_CODES) {
      const icon = iconFor(code);
      expect(icon.name, `emitter ${code}`).toBeTruthy();
      expect(icon.body.length, `emitter ${code}`).toBeGreaterThan(0);
    }
  });

  it("falls back to unknown for the enum's own gaps", () => {
    // 8, 13 and 16 are UNASSIGNED in the MAVLink enum, and 15 is spacecraft,
    // which tar1090 has no shape for. Guessing an aeroplane for any of them
    // would be asserting a category nobody sent.
    for (const code of [8, 13, 15, 16]) {
      expect(iconFor(code).name, `emitter ${code}`).toBe("unknown");
    }
  });

  it("treats 'not told' as unknown rather than as a light aircraft", () => {
    // 0 is NO_INFO and null is a receiver that sent no field at all. Neither
    // is a category, and index 0 of a list is not an answer to either.
    expect(iconFor(0).name).toBe("unknown");
    expect(iconFor(null).name).toBe("unknown");
    expect(iconFor(undefined).name).toBe("unknown");
  });

  it("separates the weight classes the way tar1090 does", () => {
    expect(iconFor(1).name).toBe("cessna");      // A1 light
    expect(iconFor(3).name).toBe("airliner");    // A3 large
    expect(iconFor(5).name).toBe("heavy_2e");    // A5 heavy
    expect(iconFor(7).name).toBe("helicopter");  // A7 rotorcraft
  });

  it("draws a UAV as a quadcopter, not as a small aeroplane", () => {
    // tar1090's own `uav` shape is a fixed-wing silhouette. This platform
    // exists to watch drones, so B6 is pointed at the quadcopter instead.
    expect(iconFor(14).name).toBe("quadcopter");
  });

  it("draws a light aircraft smaller than a heavy", () => {
    // Size is the shape's own width times tar1090's per-mapping scale, not the
    // scale alone — most of those are within a few percent of 1 and exist to
    // even out shapes that were drawn at slightly different weights.
    const drawn = (code: number) => iconFor(code).w * iconFor(code).scale;
    expect(drawn(1)).toBeLessThan(drawn(5));
  });
});

describe("what the generator emitted", () => {
  it("leaves no shape with a null viewBox", () => {
    // Several shapes carry no viewBox of their own. Emitting the missing value
    // produced viewBox="null" and a marker that drew nothing at all.
    for (const [name, shape] of Object.entries(SHAPES)) {
      expect(shape.viewBox, name).toMatch(/^-?[\d.]+ -?[\d.]+ [\d.]+ [\d.]+$/);
    }
  });

  it("scopes nothing to a global class name", () => {
    // Two ground shapes shipped `<defs><style>.cls-1{…}</style></defs>`.
    // Inlined into a marker that is not scoped to anything: `.cls-1` would
    // have applied to every other marker on the page.
    for (const [name, shape] of Object.entries(SHAPES)) {
      expect(shape.body, name).not.toContain("<style");
      expect(shape.body, name).not.toContain('class="');
    }
  });

  it("lets one property tint every shape", () => {
    // The set's placeholder fills — blue for aircraft, grey for ground
    // vehicles — all become currentColor. A shape that kept its own fill would
    // sit there in tar1090's blue while everything else went gold.
    for (const [name, shape] of Object.entries(SHAPES)) {
      expect(shape.body, name).toContain("currentColor");
      expect(shape.body, name).not.toContain("#3b82f6");
      expect(shape.body, name).not.toContain("#5a5a5a");
    }
  });

  it("keeps the dark outline that makes these readable over a city", () => {
    expect(SHAPES.airliner.body).toContain("#0b1220");
  });

  it("does not rotate what has no heading", () => {
    // A balloon has no meaningful heading and a mast certainly does not.
    expect(SHAPES.balloon.noRotate).toBe(true);
    expect(SHAPES.ground_tower.noRotate).toBe(true);
    expect(SHAPES.airliner.noRotate).toBe(false);
  });

  it("keeps ground vehicles taller than they are wide", () => {
    // 7.2 by 18 is a van seen from above. Squaring the two off — which the
    // first wiring did — turned both into unreadable slivers.
    for (const name of ["ground_service", "ground_emergency"]) {
      expect(SHAPES[name].h, name).toBeGreaterThan(SHAPES[name].w * 2);
    }
  });
});
