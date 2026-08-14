import { describe, expect, it } from "vitest";
import type { DisplayPrefs } from "./displayPrefs";
import { isCritical } from "./displayPrefs";

function prefs(over: Partial<DisplayPrefs> = {}): DisplayPrefs {
  return {
    altitudeUnit: "both",
    temperatureUnit: "c",
    pressureUnit: "hpa",
    windUnit: "kt",
    labelFields: ["callsign"],
    criticalRangeKm: 12,
    criticalAltitudeFt: 5000,
    ...over,
  };
}

describe("isCritical", () => {
  it("flags a contact that is both close and low", () => {
    // 5 km, 300 m (~984 ft): inside 12 km and below 5,000 ft.
    expect(isCritical(5, 300, prefs())).toBe(true);
  });

  it("does not flag one that is close but high", () => {
    // 3,500 m is ~11,483 ft — well over the threshold. Both must hold.
    expect(isCritical(5, 3500, prefs())).toBe(false);
  });

  it("does not flag one that is low but far", () => {
    expect(isCritical(40, 300, prefs())).toBe(false);
  });

  it("never flags a contact with no altitude, however close", () => {
    // The same rule the station applies: an aircraft that is not reporting its
    // altitude is not judged low, so it cannot be critical.
    expect(isCritical(1, null, prefs())).toBe(false);
    expect(isCritical(1, undefined, prefs())).toBe(false);
  });

  it("never flags a contact with no range", () => {
    expect(isCritical(null, 300, prefs())).toBe(false);
    expect(isCritical(undefined, 300, prefs())).toBe(false);
  });

  it("follows the operator's own thresholds", () => {
    // 1,700 m is ~5,577 ft: above the default 5,000 ft ceiling but below a
    // 6,000 ft one the operator raised it to. The flag is their view.
    const wide = prefs({ criticalRangeKm: 20, criticalAltitudeFt: 6000 });
    expect(isCritical(8, 1700, wide)).toBe(true);
    expect(isCritical(8, 1700, prefs())).toBe(false); // above the default ceiling
  });

  it("is a strict below/within, so a contact exactly on a bound is clear", () => {
    // 5,000 ft is 1,524 m; a contact exactly at the altitude bound is not below
    // it, and one exactly at the range bound is not within it.
    expect(isCritical(12, 300, prefs())).toBe(false); // range == bound
    expect(isCritical(5, 1524, prefs())).toBe(false); // altitude == bound (ft)
  });
});
