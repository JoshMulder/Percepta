import { describe, expect, it } from "vitest";
import { formatAltitude } from "./format";

describe("formatAltitude", () => {
  it("shows both units, metres first", () => {
    expect(formatAltitude(3500, "both")).toBe("3,500 m · 11,483 ft");
  });

  it("shows metres alone", () => {
    expect(formatAltitude(3500, "m")).toBe("3,500 m");
  });

  it("shows feet alone", () => {
    expect(formatAltitude(3500, "ft")).toBe("11,483 ft");
  });

  it("renders a genuine zero rather than nothing", () => {
    expect(formatAltitude(0, "ft")).toBe("0 ft");
  });
});
