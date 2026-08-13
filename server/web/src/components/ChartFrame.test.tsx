import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ChartFrame, gridLines } from "./ChartFrame";
import { fixedScale, niceScale } from "../chartScale";

/**
 * The frame's one real claim: a gradation's label and its gridline are the same
 * statement, so they must land in the same place. The label is HTML positioned
 * as a percentage from the bottom and the line is an SVG coordinate from the
 * top, computed in different files — which is exactly the sort of pair that
 * drifts silently and leaves an axis a pixel wrong forever.
 */

const H = 34;

afterEach(cleanup);

function frame(scale = fixedScale(0, 100), unit = "%") {
  render(
    <ChartFrame scale={scale} unit={unit}>
      <svg viewBox={`0 0 100 ${H}`} preserveAspectRatio="none">
        {gridLines(scale, H)}
        <path d="M0 0 L100 34" />
      </svg>
    </ChartFrame>,
  );
}

describe("ChartFrame", () => {
  it("puts every label at the height of its own gridline", () => {
    frame();
    const labels = Array.from(document.querySelectorAll(".chart-gutter-tick"));
    const lines = Array.from(document.querySelectorAll("line.chart-grid"));
    expect(labels).toHaveLength(lines.length);

    labels.forEach((label, i) => {
      // The label's distance up from the bottom, as a fraction.
      const bottom = parseFloat((label as HTMLElement).style.bottom) / 100;
      // The line's, derived back out of an SVG y measured from the top.
      const y = parseFloat(lines[i].getAttribute("y1") ?? "NaN");
      expect(1 - y / H, `tick ${i} (${label.textContent})`).toBeCloseTo(bottom, 6);
    });
  });

  it("spans the full width of the plot with each gridline", () => {
    frame();
    for (const line of document.querySelectorAll("line.chart-grid")) {
      expect(line.getAttribute("x1")).toBe("0");
      expect(line.getAttribute("x2")).toBe("100");
    }
  });

  it("names the unit once, at the top, rather than on every gradation", () => {
    frame();
    const labels = Array.from(document.querySelectorAll(".chart-gutter-tick")).map(
      (e) => e.textContent,
    );
    expect(labels.filter((l) => l?.includes("%"))).toHaveLength(1);
    expect(labels[labels.length - 1]).toContain("%");
  });

  it("keeps the labels out of the plot", () => {
    // The whole point of the gutter: the trace no longer runs under the numbers.
    frame();
    const plot = document.querySelector(".chart-plot");
    expect(plot?.querySelector(".chart-gutter-tick")).toBeNull();
    expect(document.querySelector(".chart-gutter .chart-gutter-tick")).not.toBeNull();
  });

  it("draws no time axis when the chart's x is not time", () => {
    frame();
    expect(document.querySelector(".chart-x")).toBeNull();
  });

  it("insets the time axis to sit under the plot, not the gutter", () => {
    render(
      <ChartFrame scale={niceScale(0, 10)} from={0} to={3600_000}>
        <svg viewBox="0 0 100 34" />
      </ChartFrame>,
    );
    // It is wrapped rather than bare, and the wrapper is what carries the inset
    // — a bare axis would start under the labels and misalign with the data.
    expect(document.querySelector(".chart-x-wrap .chart-x")).not.toBeNull();
  });

  it("survives a flat series without a divide by zero", () => {
    // A battery sitting at 100% all window, a barometer that has not moved.
    frame(niceScale(100, 100), "%");
    for (const line of document.querySelectorAll("line.chart-grid")) {
      expect(Number.isFinite(parseFloat(line.getAttribute("y1") ?? ""))).toBe(true);
    }
    for (const label of document.querySelectorAll(".chart-gutter-tick")) {
      expect((label as HTMLElement).style.bottom).not.toContain("NaN");
    }
  });
});
