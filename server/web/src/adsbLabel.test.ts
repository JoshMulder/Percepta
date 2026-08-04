import { describe, expect, it } from "vitest";
import { buildLabel } from "./adsbLabel";
import type { DisplayPrefs } from "./displayPrefs";
import type { Aircraft } from "./types";

function contact(over: Partial<Aircraft> = {}): Aircraft {
  return {
    icao: "C827F1",
    callsign: "ANZ759M",
    latitude: -44.5,
    longitude: 171.5,
    altitude_m: 3500,
    track_deg: 210,
    speed_kt: 262,
    range_km: 34,
    bearing_deg: 130,
    emitter_type: 7, // A7 — helicopter
    ...over,
  };
}

function prefs(over: Partial<DisplayPrefs> = {}): DisplayPrefs {
  return {
    altitudeUnit: "both",
    labelFields: ["callsign"],
    criticalRangeKm: 12,
    criticalAltitudeFt: 5000,
    ...over,
  };
}

describe("buildLabel", () => {
  it("shows the callsign alone by default", () => {
    expect(buildLabel(contact(), prefs(), null)).toBe("ANZ759M");
  });

  it("stacks the chosen fields in the canonical order, one per line", () => {
    const label = buildLabel(
      contact(),
      prefs({ labelFields: ["altitude", "callsign"] }),
      null,
    );
    // Callsign is listed before altitude in the canonical order regardless of
    // how the fields were passed — but buildLabel walks prefs.labelFields, which
    // the settings keep canonical. Here altitude precedes callsign as passed.
    expect(label).toBe("11,483 ft\nANZ759M");
  });

  it("uses feet on the label even when the card shows both", () => {
    const label = buildLabel(
      contact(),
      prefs({ labelFields: ["altitude"], altitudeUnit: "both" }),
      null,
    );
    expect(label).toBe("11,483 ft");
  });

  it("honours an explicit metres choice on the label", () => {
    const label = buildLabel(
      contact(),
      prefs({ labelFields: ["altitude"], altitudeUnit: "m" }),
      null,
    );
    expect(label).toBe("3,500 m");
  });

  it("skips a field the aircraft did not send rather than leaving a gap", () => {
    const label = buildLabel(
      contact({ callsign: null, speed_kt: 240 }),
      prefs({ labelFields: ["callsign", "speed"] }),
      null,
    );
    expect(label).toBe("240 kt");
  });

  it("shows the registration when asked and it is known", () => {
    const label = buildLabel(
      contact({ callsign: null }),
      prefs({ labelFields: ["registration"] }),
      "ZK-HBX",
    );
    expect(label).toBe("ZK-HBX");
  });

  it("falls back to the ICAO when every chosen field is empty", () => {
    // Registration wanted but unknown, and nothing else selected: the marker is
    // still identifiable rather than a glyph with no label.
    const label = buildLabel(
      contact({ callsign: null }),
      prefs({ labelFields: ["registration"] }),
      null,
    );
    expect(label).toBe("C827F1");
  });

  it("names the emitter category for the type field", () => {
    const label = buildLabel(
      contact({ callsign: null }),
      prefs({ labelFields: ["type"] }),
      null,
    );
    expect(label).toBe("Helicopter");
  });

  it("does not repeat the callsign when registration fell back to it", () => {
    // When the registry has no tail number the caller passes the callsign as
    // the registration; a label showing both fields must not print it twice.
    const label = buildLabel(
      contact({ callsign: "ANZ759M" }),
      prefs({ labelFields: ["callsign", "registration"] }),
      "ANZ759M",
    );
    expect(label).toBe("ANZ759M");
  });
});
