import { describe, expect, it } from "vitest";
import { panelStatus } from "./components/PanelState";

/**
 * When a panel shows skeletons, a red X, or neither.
 *
 * The case that prompted the fourth state: a station with no floodlight
 * publishes no light stream at all, so the console's only evidence was an
 * absence. An absence took twelve seconds to become suggestive and then
 * resolved to a fault — wrong twice over. It wasted the wait, and it called a
 * complete station broken.
 */

const NOW = Date.now();
const STALE = 15_000;

describe("an empty slot", () => {
  it("is obvious immediately, with no grace period", () => {
    // The station states this in every health frame; there is nothing to wait
    // for and no timer involved.
    expect(panelStatus(null, NOW, STALE, false, false)).toBe("not-fitted");
  });

  it("stays not-fitted however long the link has been up", () => {
    // The old path would have called this a fault after twelve seconds.
    const longAgo = NOW - 10 * 60 * 1000;
    expect(panelStatus(null, longAgo, STALE, false, false)).toBe("not-fitted");
  });

  it("is not a fault", () => {
    expect(panelStatus(null, NOW - 60_000, STALE, false, false)).not.toBe("fault");
  });
});

describe("before the station has said anything", () => {
  it("keeps loading rather than guessing at not-fitted", () => {
    // `undefined` means no health frame yet. Guessing here would flash "Not
    // fitted" across every panel on connect and then correct itself.
    expect(panelStatus(null, NOW, STALE, false, undefined)).toBe("loading");
  });

  it("still becomes a fault once the grace period expires", () => {
    const longAgo = NOW - 10 * 60 * 1000;
    expect(panelStatus(null, longAgo, STALE, false, undefined)).toBe("fault");
  });
});

describe("a fitted sensor", () => {
  it("loads, then goes live", () => {
    expect(panelStatus(null, NOW, STALE, false, true)).toBe("loading");
    expect(panelStatus(NOW, NOW, STALE, false, true)).toBe("live");
  });

  it("faults when it stops reporting", () => {
    expect(panelStatus(NOW - 60_000, NOW, STALE, false, true)).toBe("fault");
  });
});

describe("data outranks the slot report", () => {
  it("shows readings that are actually arriving", () => {
    // A slot reported empty while its stream is live is a contradiction, and
    // showing what the station is really sending is the safer half of it.
    expect(panelStatus(NOW, NOW, STALE, false, false)).toBe("live");
  });
});

describe("demo stations", () => {
  it("never show a fault, because the simulator is the sensor", () => {
    expect(panelStatus(null, NOW - 10 * 60 * 1000, STALE, true, undefined))
      .toBe("loading");
    expect(panelStatus(NOW - 60_000, NOW, STALE, true, true)).toBe("live");
  });
});
