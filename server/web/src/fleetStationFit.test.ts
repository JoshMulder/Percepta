import { describe, expect, it } from "vitest";

import type { FleetStation } from "./types";
import { fitKey, located } from "./fleetStationFit";

/**
 * The map's fit, tested where it can actually be tested.
 *
 * jsdom has no WebGL and the suite's maplibre stub has no `fitBounds` — so an
 * assertion written against the map component passes against nothing at all.
 * These two functions are pure precisely so the behaviour that was broken for
 * the life of the component is reachable from a test runner.
 */

function station(over: Partial<FleetStation> = {}): FleetStation {
  return {
    id: "s1",
    name: "Kennels Road",
    organization_id: "o1",
    organization_name: "SPS",
    latitude: -43.5,
    longitude: 172.6,
    locality: null,
    region: null,
    status: "online",
    dark: false,
    last_seen_at: null,
    is_simulated: false,
    model: null,
    config_version: 1,
    ...over,
  } as FleetStation;
}

describe("located", () => {
  it("drops a station with no usable position", () => {
    // The crash this guard exists for: the digest shipped stations without
    // coordinates and MapLibre threw "Invalid LngLat object: (NaN, NaN)",
    // taking the whole wall down.
    expect(located([station({ latitude: undefined })])).toHaveLength(0);
    expect(located([station({ longitude: null as unknown as number })])).toHaveLength(0);
    expect(located([station({ latitude: NaN })])).toHaveLength(0);
  });

  it("keeps a station at a legitimate zero", () => {
    // isFinite, not truthiness — the Gulf of Guinea is a real place.
    expect(located([station({ latitude: 0, longitude: 0 })])).toHaveLength(1);
  });
});

describe("fitKey", () => {
  it("changes when a station joins the map", () => {
    const before = fitKey([station({ id: "a" })]);
    const after = fitKey([station({ id: "a" }), station({ id: "b" })]);
    expect(after).not.toBe(before);
  });

  it("changes when a station gains a position for the first time", () => {
    // The interesting case: the station was in the fleet all along, so a key
    // based on the fleet's length would not have moved.
    const before = fitKey([station({ id: "a", latitude: undefined })]);
    const after = fitKey([station({ id: "a" })]);
    expect(before).toBe("");
    expect(after).toBe("a");
  });

  it("does not change when the same stations arrive in a different order", () => {
    // The poll does not promise an order. If it did, the view would re-fit on
    // every frame and an operator could never pan anywhere.
    const one = fitKey([station({ id: "a" }), station({ id: "b" })]);
    const two = fitKey([station({ id: "b" }), station({ id: "a" })]);
    expect(one).toBe(two);
  });

  it("does not change when a position is merely corrected", () => {
    // Deliberate: a station whose coordinates are refined by a metre must not
    // yank the operator's view back mid-shift.
    const before = fitKey([station({ id: "a" })]);
    const after = fitKey([station({ id: "a", latitude: -43.5001 })]);
    expect(after).toBe(before);
  });

  it("is empty when nothing can be placed", () => {
    // An empty key means "do not fit" — fitting to an empty bounds is what
    // produced the whole-of-country default view in the first place.
    expect(fitKey([station({ latitude: undefined })])).toBe("");
    expect(fitKey([])).toBe("");
  });
});
