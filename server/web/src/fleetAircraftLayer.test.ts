import { describe, expect, it } from "vitest";

import type { FleetAircraft } from "./types";
import { aircraftFeatures, aircraftTrails } from "./fleetAircraftLayer";

/**
 * The pure half of the DOM-markers-to-symbol-layer conversion.
 *
 * It is pure precisely so that it can be tested: jsdom has no WebGL, and the
 * suite's maplibre stub has no `addSource`, so nothing downstream of these
 * features is reachable from a test runner. Putting the transform inside the
 * effect would have made the whole change untestable — which matters most for
 * the coordinate guard below, since that one has already taken the wall down
 * once in production.
 */

function contact(over: Partial<FleetAircraft> = {}): FleetAircraft {
  return {
    icao: "C81234",
    callsign: "ANZ123",
    latitude: -43.5,
    longitude: 172.6,
    track_deg: 90,
    heard_by: 2,
    ...over,
  } as FleetAircraft;
}

describe("aircraftFeatures", () => {
  it("carries position, track and label onto the feature", () => {
    const { features } = aircraftFeatures([contact()]);
    expect(features).toHaveLength(1);
    expect(features[0].geometry.coordinates).toEqual([172.6, -43.5]);
    expect(features[0].properties.track).toBe(90);
    expect(features[0].properties.label).toBe("ANZ123");
  });

  it("drops a contact with no usable position", () => {
    // The bug this guard exists for: a digest that omitted a coordinate reached
    // MapLibre as NaN and threw "Invalid LngLat object: (NaN, NaN)", which took
    // the entire wall down — from one aircraft, in one frame.
    expect(aircraftFeatures([contact({ latitude: undefined })]).features).toHaveLength(0);
    // Cast, because the TYPE says these are numbers and the digest has shipped
    // them absent anyway — which is the entire reason for a runtime guard on a
    // field TypeScript believes cannot be missing.
    expect(
      aircraftFeatures([contact({ longitude: null as unknown as number })]).features,
    ).toHaveLength(0);
    expect(aircraftFeatures([contact({ latitude: NaN })]).features).toHaveLength(0);
  });

  it("keeps a contact whose position is a legitimate zero", () => {
    // `Number.isFinite`, not truthiness. The Gulf of Guinea is a real place and
    // a falsy-check would silently delete anything flying over it.
    const { features } = aircraftFeatures([contact({ latitude: 0, longitude: 0 })]);
    expect(features).toHaveLength(1);
  });

  it("points a trackless contact north rather than dropping it", () => {
    // "We do not know which way this is going" is worth rendering; an aircraft
    // that vanishes because it reported no heading is not.
    const { features } = aircraftFeatures([contact({ track_deg: undefined })]);
    expect(features).toHaveLength(1);
    expect(features[0].properties.track).toBe(0);
  });

  it("falls back to the ICAO when there is no callsign", () => {
    const { features } = aircraftFeatures([
      contact({ callsign: "   " }),
      contact({ icao: "C89999", callsign: undefined }),
    ]);
    expect(features[0].properties.label).toBe("C81234");
    expect(features[1].properties.label).toBe("C89999");
  });

  it("produces a valid empty collection for an empty fleet", () => {
    // A quiet sky is the normal overnight state and must not be a special case.
    expect(aircraftFeatures([])).toEqual({
      type: "FeatureCollection",
      features: [],
    });
  });
});

describe("aircraftTrails", () => {
  it("builds a LineString per contact with a track", () => {
    const { features } = aircraftTrails([
      contact({ trail: [[172.6, -43.5], [172.7, -43.5]] }),
    ]);
    expect(features).toHaveLength(1);
    expect((features[0] as any).geometry).toEqual({
      type: "LineString",
      coordinates: [[172.6, -43.5], [172.7, -43.5]],
    });
  });

  it("drops a contact with fewer than two points", () => {
    // A one-point "line" renders as nothing, correctly — but still costs a
    // feature and a buffer upload per frame, and on a busy circuit most
    // contacts are newly heard.
    expect(aircraftTrails([contact({ trail: [[172.6, -43.5]] })]).features).toHaveLength(0);
    expect(aircraftTrails([contact({ trail: [] })]).features).toHaveLength(0);
    expect(aircraftTrails([contact()]).features).toHaveLength(0);
  });

  it("strips a hole in the trail rather than passing NaN to the map", () => {
    // Same guard as the positions and for the same reason: one NaN throws
    // "Invalid LngLat object" and takes the whole map down.
    const { features } = aircraftTrails([
      contact({
        trail: [[172.6, -43.5], [NaN, -43.5], [172.8, -43.5]],
      }),
    ]);
    expect((features[0] as any).geometry.coordinates).toEqual([
      [172.6, -43.5],
      [172.8, -43.5],
    ]);
  });

  it("drops a trail that is all holes", () => {
    expect(
      aircraftTrails([contact({ trail: [[NaN, NaN], [NaN, NaN]] })]).features,
    ).toHaveLength(0);
  });
});
