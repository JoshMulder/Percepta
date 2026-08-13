import { describe, expect, it } from "vitest";
import { fixedScale, niceScale, tickFormat } from "./chartScale";

/**
 * The gradations. What matters is that a person would have chosen the same
 * numbers, that the ends of the axis contain the data, and that a label's
 * position is derived from the same fraction as its gridline — because the two
 * drifting apart is the failure nobody notices until a gridline sits a pixel
 * off its own number.
 */

const values = (s: { ticks: { value: number }[] }) => s.ticks.map((t) => t.value);

describe("niceScale", () => {
  it("chooses round steps a person would have chosen", () => {
    // 8.2 to 14.7 degrees overnight: 2-degree steps, not 6.5/4 = 1.625.
    expect(values(niceScale(8.2, 14.7))).toEqual([8, 10, 12, 14, 16]);
  });

  it("always contains the data", () => {
    for (const [lo, hi] of [
      [8.2, 14.7],
      [0, 128],
      [-3.5, 21.2],
      [1003, 1021],
      [0.02, 0.09],
    ]) {
      const s = niceScale(lo, hi);
      expect(s.min, `min for ${lo}..${hi}`).toBeLessThanOrEqual(lo);
      expect(s.max, `max for ${lo}..${hi}`).toBeGreaterThanOrEqual(hi);
    }
  });

  it("puts a tick at each end of its own range", () => {
    const s = niceScale(0, 128);
    expect(s.ticks[0].value).toBe(s.min);
    expect(s.ticks[s.ticks.length - 1].value).toBe(s.max);
  });

  it("derives frac from the same range the gridline uses", () => {
    const s = niceScale(0, 100);
    expect(s.ticks[0].frac).toBe(0);
    expect(s.ticks[s.ticks.length - 1].frac).toBe(1);
    // Every tick's frac is exactly where its value sits in the range.
    for (const t of s.ticks) {
      expect(t.frac).toBeCloseTo((t.value - s.min) / (s.max - s.min), 10);
    }
  });

  it("does not produce floating-point debris in the labels", () => {
    // 0.1 added seven times is 0.7000000000000001, and that reaches the label.
    for (const v of values(niceScale(0, 0.7))) {
      expect(String(v).length, `ugly tick ${v}`).toBeLessThan(6);
    }
  });

  it("aims near the requested number of gradations", () => {
    // Rounding outward can add one, so this is a band rather than a promise.
    for (const target of [3, 4, 5]) {
      const n = niceScale(0, 128, target).ticks.length;
      expect(n, `target ${target} gave ${n}`).toBeGreaterThanOrEqual(target - 1);
      expect(n, `target ${target} gave ${n}`).toBeLessThanOrEqual(target + 2);
    }
  });

  describe("degenerate ranges", () => {
    it("centres a flat series instead of dividing by zero", () => {
      // Every sample identical - a battery sitting at 100%, a barometer that
      // has not moved. The old code divided by a zero range.
      const s = niceScale(50, 50);
      expect(s.min).toBeLessThan(50);
      expect(s.max).toBeGreaterThan(50);
      expect(s.frac(50)).toBeCloseTo(0.5, 6);
      expect(Number.isFinite(s.frac(50))).toBe(true);
    });

    it("handles a flat series at zero", () => {
      const s = niceScale(0, 0);
      expect(s.max).toBeGreaterThan(s.min);
      expect(Number.isFinite(s.frac(0))).toBe(true);
    });

    it("survives a reversed or non-finite range", () => {
      expect(values(niceScale(10, 2)).length).toBeGreaterThan(1);
      expect(Number.isFinite(niceScale(NaN, 5).frac(1))).toBe(true);
    });
  });
});

describe("fixedScale", () => {
  it("gradates a percentage the way a person reads one", () => {
    expect(values(fixedScale(0, 100))).toEqual([0, 25, 50, 75, 100]);
  });

  it("always labels the top of a fixed range", () => {
    // The top is the reason the range was fixed; a rounded step must not eat it.
    for (const hi of [100, 90, 42, 7]) {
      const v = values(fixedScale(0, hi));
      expect(v[v.length - 1], `top of 0..${hi}`).toBe(hi);
    }
  });
});

describe("tickFormat", () => {
  it("shows decimals only when the step needs them", () => {
    expect(tickFormat(niceScale(0, 100).ticks)(50)).toBe("50");
    expect(tickFormat(niceScale(8.2, 8.9).ticks)(8.4)).toBe("8.4");
  });

  it("does not print a whole number as 1013.00", () => {
    expect(tickFormat(niceScale(1003, 1021).ticks)(1010)).toBe("1010");
  });
});
