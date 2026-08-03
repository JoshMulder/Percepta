import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { PowerFlowHistory, type SocSample } from "./BatteryChart";

/**
 * The flows drawn beneath the state of charge in the battery popout.
 *
 * The one rule that matters, and the same one the live diagram lives by: a
 * source that is not fitted is null across the whole window and must not be
 * drawn. Drawing it would put a flat zero line on the chart and a source in the
 * legend that this site has never had.
 */

afterEach(cleanup);

function series(over: Partial<SocSample>): SocSample {
  return { t: 0, soc: 80, pv: 0, load: 0, mains: null, gen: null, ...over };
}

function legend(): string[] {
  return Array.from(document.querySelectorAll(".series-key")).map(
    (e) => e.textContent ?? "",
  );
}

describe("PowerFlowHistory", () => {
  it("draws a line and a legend entry for each fitted source", () => {
    render(
      <PowerFlowHistory
        samples={[
          series({ t: 0, load: 100, pv: 300, mains: 0, gen: 50 }),
          series({ t: 60_000, load: 120, pv: 250, mains: 0, gen: 60 }),
        ]}
      />,
    );
    const keys = legend();
    expect(keys.some((k) => k.startsWith("Load"))).toBe(true);
    expect(keys.some((k) => k.startsWith("Solar"))).toBe(true);
    expect(keys.some((k) => k.startsWith("AC In"))).toBe(true);
    expect(keys.some((k) => k.startsWith("Generator"))).toBe(true);
    // One path per drawn series.
    expect(document.querySelectorAll(".series-line").length).toBe(4);
  });

  it("leaves an unfitted source off the chart and the legend entirely", () => {
    render(
      <PowerFlowHistory
        samples={[
          series({ t: 0, load: 100, pv: 300, mains: null, gen: null }),
          series({ t: 60_000, load: 120, pv: 250, mains: null, gen: null }),
        ]}
      />,
    );
    const keys = legend();
    expect(keys.some((k) => k.startsWith("AC In"))).toBe(false);
    expect(keys.some((k) => k.startsWith("Generator"))).toBe(false);
    // Only the two fitted sources are drawn.
    expect(document.querySelectorAll(".series-line").length).toBe(2);
  });

  it("shows the latest watt figure of each source", () => {
    render(
      <PowerFlowHistory
        samples={[
          series({ t: 0, load: 100, pv: 300 }),
          series({ t: 60_000, load: 137, pv: 250 }),
        ]}
      />,
    );
    const load = legend().find((k) => k.startsWith("Load")) ?? "";
    expect(load).toContain("137 W");
  });

  it("says so rather than drawing an empty box before there is history", () => {
    render(<PowerFlowHistory samples={[]} loading />);
    expect(document.querySelectorAll(".series-line").length).toBe(0);
    expect(document.querySelector(".series-legend")?.textContent).toContain(
      "loading",
    );
  });

  it("breaks the line across a gap instead of leaping it", () => {
    // The generator ran, stopped being reported, then ran again. Its line
    // should be two strokes, not one drawn straight through the gap — so the
    // path picks the pen back up with an M, giving two subpaths.
    render(
      <PowerFlowHistory
        samples={[
          series({ t: 0, gen: 50, load: 10 }),
          series({ t: 60_000, gen: null, load: 10 }),
          series({ t: 120_000, gen: 70, load: 10 }),
        ]}
      />,
    );
    const genPath = Array.from(
      document.querySelectorAll<SVGPathElement>(".series-line"),
    ).find((p) => (p.getAttribute("d") ?? "").split("M").length === 3);
    expect(genPath, "the generator line should be two subpaths").toBeTruthy();
  });
});
