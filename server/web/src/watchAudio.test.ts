import { describe, expect, it } from "vitest";

import { panFor } from "./watchAudio";

/**
 * Stereo placement is the only part of the audio engine that can be tested
 * here, and it is worth testing on its own: jsdom has no AudioContext, no
 * AudioDecoder and no WebCodecs at all, so everything downstream of these
 * numbers is unreachable from a test runner. Which is exactly why the numbers
 * are a pure function rather than three lines inside `ensureChannel`.
 */
describe("panFor", () => {
  it("puts a lone channel in the centre", () => {
    // Panning the only thing playing would be a gratuitous statement about a
    // field with nothing else in it.
    expect(panFor(0, 1)).toBe(0);
  });

  it("never pans hard left or right", () => {
    // An operator wearing one earpiece — which is how half of them work — hears
    // nothing at all from a channel panned fully to the other side.
    for (let count = 2; count <= 8; count += 1) {
      for (let i = 0; i < count; i += 1) {
        expect(Math.abs(panFor(i, count))).toBeLessThanOrEqual(0.7);
      }
    }
  });

  it("spreads channels in strip order", () => {
    // Position follows the strip so the picture and the sound agree; an operator
    // telling two simultaneous overs apart by ear is relying on that.
    const positions = [0, 1, 2, 3].map((i) => panFor(i, 4));
    const sorted = [...positions].sort((a, b) => a - b);
    expect(positions).toEqual(sorted);
    expect(new Set(positions).size).toBe(positions.length);
  });

  it("uses the full width whatever the channel count", () => {
    for (let count = 2; count <= 8; count += 1) {
      expect(panFor(0, count)).toBeCloseTo(-0.7);
      expect(panFor(count - 1, count)).toBeCloseTo(0.7);
    }
  });
});
