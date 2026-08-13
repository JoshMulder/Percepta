import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ChartTimeAxis, axisFormatter } from "./ChartTimeAxis";

/**
 * The axis under the trend charts.
 *
 * The rules worth holding: it describes the window the samples actually cover
 * rather than the one that was asked for, it refuses to draw an axis for a
 * window that has no width, and it stops using bare clock times once the span
 * is long enough for two ticks to read the same.
 */

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
/** A fixed instant, so the assertions do not move with the clock. */
const T0 = Date.parse("2026-08-13T06:00:00Z");

function ticks(): string[] {
  return Array.from(document.querySelectorAll(".chart-x-tick")).map(
    (e) => e.textContent ?? "",
  );
}

afterEach(cleanup);

describe("ChartTimeAxis", () => {
  it("marks both ends and the middle", () => {
    render(<ChartTimeAxis from={T0} to={T0 + 12 * HOUR} />);
    expect(ticks()).toHaveLength(3);
    expect(document.querySelectorAll(".chart-x-tick.first")).toHaveLength(1);
    expect(document.querySelectorAll(".chart-x-tick.last")).toHaveLength(1);
  });

  it("spaces the ticks evenly across the window", () => {
    // The middle tick of a 12h window is the 6h mark, whatever the locale
    // renders it as - so compare it against the formatter's own output.
    const fmt = axisFormatter(12 * HOUR);
    render(<ChartTimeAxis from={T0} to={T0 + 12 * HOUR} />);
    expect(ticks()).toEqual([
      fmt(T0),
      fmt(T0 + 6 * HOUR),
      fmt(T0 + 12 * HOUR),
    ]);
  });

  it("takes more ticks when asked", () => {
    render(<ChartTimeAxis from={T0} to={T0 + DAY} ticks={5} />);
    expect(ticks()).toHaveLength(5);
  });

  it("carries the full timestamp in a title", () => {
    render(<ChartTimeAxis from={T0} to={T0 + DAY} />);
    const first = document.querySelector(".chart-x-tick");
    expect(first?.getAttribute("title")).toBe(new Date(T0).toLocaleString());
  });

  describe("the format", () => {
    it("is a clock time over a short window", () => {
      // 12h and 1d are read as "when today", so a time is what is wanted.
      const short = axisFormatter(12 * HOUR)(T0);
      expect(short).toMatch(/\d/);
      expect(short).toBe(
        new Date(T0).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
      );
    });

    it("becomes a date once a clock time would be ambiguous", () => {
      // The 7d and 30d windows. Three ticks reading "06:00" is not an axis.
      for (const span of [7 * DAY, 30 * DAY]) {
        expect(axisFormatter(span)(T0)).toBe(
          new Date(T0).toLocaleDateString([], {
            day: "numeric",
            month: "short",
          }),
        );
      }
    });

    it("switches at a day and a half, not at a day", () => {
      // A 24h window still wants clock times: it is one night, and the two ends
      // are the same date read from either side of midnight.
      const asTime = new Date(T0).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      expect(axisFormatter(DAY)(T0)).toBe(asTime);
      expect(axisFormatter(35 * HOUR)(T0)).toBe(asTime);
      expect(axisFormatter(37 * HOUR)(T0)).not.toBe(asTime);
    });
  });

  describe("windows with no width", () => {
    // A station that has reported once, or not since the window opened. Drawing
    // an axis across a zero-width span would put three identical labels under a
    // chart with nothing on it.
    it.each([
      ["a single instant", T0, T0],
      ["time running backwards", T0 + DAY, T0],
      ["a missing endpoint", T0, NaN],
    ])("draws nothing for %s", (_label, from, to) => {
      const { container } = render(<ChartTimeAxis from={from} to={to} />);
      expect(container.querySelector(".chart-x")).toBeNull();
    });
  });
});
