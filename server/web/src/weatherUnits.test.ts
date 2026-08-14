import { describe, expect, it } from "vitest";
import { weatherDisplay } from "./format";

/**
 * Weather units are display-only, and the risk in them is not arithmetic.
 *
 * The conversions are one multiplication each and hard to get wrong. What is
 * easy to get wrong is the pairing: a value converted while its suffix is not,
 * or a suffix changed while the decimals are not. Both produce a number that
 * looks entirely plausible and is wrong — an inHg pressure printed to the
 * nearest whole number reads "30" every day of the year, which is not a
 * rounding error but a barometer that appears to have stopped.
 */

const C = { temperatureUnit: "c", pressureUnit: "hpa", windUnit: "kt" } as const;

describe("weatherDisplay", () => {
  it("leaves the station's own units alone", () => {
    // The contract's units, which are also what is stored. The identity case
    // has to be exact: a rounding drift here would show up as the history and
    // the live reading disagreeing about the same moment.
    expect(weatherDisplay("temp", C).convert(12.4)).toBe(12.4);
    expect(weatherDisplay("pressure", C).convert(1013)).toBe(1013);
    expect(weatherDisplay("wind", C).convert(17)).toBe(17);
  });

  it("converts temperature at the freezing and boiling points", () => {
    const f = weatherDisplay("temp", { ...C, temperatureUnit: "f" });
    expect(f.convert(0)).toBeCloseTo(32, 6);
    expect(f.convert(100)).toBeCloseTo(212, 6);
    expect(f.convert(-40)).toBeCloseTo(-40, 6);
    expect(f.suffix).toContain("F");
  });

  it("converts pressure to inches of mercury", () => {
    // Standard atmosphere: 1013.25 hPa is 29.92 inHg, the number every altimeter
    // setting in aviation is quoted against.
    const inhg = weatherDisplay("pressure", { ...C, pressureUnit: "inhg" });
    expect(inhg.convert(1013.25)).toBeCloseTo(29.92, 2);
    expect(inhg.suffix).toBe("inHg");
  });

  it("gives inHg the decimals it needs to move at all", () => {
    // A whole weather system moves inHg by about one unit. At zero decimals the
    // reading would be "30" in fair weather and "30" in a gale.
    const inhg = weatherDisplay("pressure", { ...C, pressureUnit: "inhg" });
    expect(inhg.digits).toBeGreaterThanOrEqual(2);
    const fair = inhg.convert(1025).toFixed(inhg.digits);
    const storm = inhg.convert(985).toFixed(inhg.digits);
    expect(fair).not.toBe(storm);
  });

  it("treats millibars as hectopascals, because they are the same unit", () => {
    const mb = weatherDisplay("pressure", { ...C, pressureUnit: "mb" });
    expect(mb.convert(1013)).toBe(1013);
    expect(mb.suffix).toBe("mb");
  });

  it("converts wind to each offered unit", () => {
    const at = (windUnit: "kmh" | "mph" | "ms") =>
      weatherDisplay("wind", { ...C, windUnit });
    expect(at("kmh").convert(10)).toBeCloseTo(18.52, 2);
    expect(at("mph").convert(10)).toBeCloseTo(11.5078, 3);
    expect(at("ms").convert(10)).toBeCloseTo(5.14444, 4);
  });

  it("gives metres per second a decimal, because a calm day is 0 to 3", () => {
    const ms = weatherDisplay("wind", { ...C, windUnit: "ms" });
    expect(ms.digits).toBeGreaterThanOrEqual(1);
    // 4 kt and 6 kt are meaningfully different breezes and must not both be "3".
    expect(ms.convert(4).toFixed(ms.digits)).not.toBe(
      ms.convert(6).toFixed(ms.digits),
    );
  });

  it("never converts a value without also changing its suffix", () => {
    // The pairing failure. Every non-default unit must relabel, or the screen
    // states a converted number under the station's original unit.
    const cases: [string, object, string][] = [
      ["temp", { ...C, temperatureUnit: "f" }, "°C"],
      ["pressure", { ...C, pressureUnit: "inhg" }, "hPa"],
      ["wind", { ...C, windUnit: "kmh" }, "kt"],
      ["wind", { ...C, windUnit: "mph" }, "kt"],
      ["wind", { ...C, windUnit: "ms" }, "kt"],
    ];
    for (const [series, prefs, original] of cases) {
      const d = weatherDisplay(series, prefs as never);
      expect(d.convert(10), `${series} did not convert`).not.toBe(10);
      expect(d.suffix, `${series} kept its original suffix`).not.toBe(original);
    }
  });

  it("passes humidity through whatever the other units say", () => {
    // Per cent is per cent. It has no setting, and must not acquire one by
    // accident when a neighbouring unit changes.
    const d = weatherDisplay("humidity", {
      temperatureUnit: "f",
      pressureUnit: "inhg",
      windUnit: "ms",
    });
    expect(d.convert(71)).toBe(71);
    expect(d.suffix).toBe("%");
  });
});
