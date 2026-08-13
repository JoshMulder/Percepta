import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { WeatherHistory, type WeatherSample } from "./WeatherChart";

afterEach(cleanup);

function sample(over: Partial<WeatherSample>): WeatherSample {
  return { t: 0, temp: 14, humidity: 70, pressure: 1013, wind: 8, ...over };
}

function rows(): string[] {
  return Array.from(document.querySelectorAll(".weather-row-label")).map(
    (e) => e.textContent ?? "",
  );
}

describe("WeatherHistory", () => {
  it("draws a sparkline per fitted sensor", () => {
    render(
      <WeatherHistory
        samples={[
          sample({ t: 0 }),
          sample({ t: 60_000, temp: 15, humidity: 72, pressure: 1012, wind: 10 }),
        ]}
      />,
    );
    expect(rows()).toEqual(["Temperature", "Humidity", "Pressure", "Wind"]);
    expect(document.querySelectorAll(".weather-line").length).toBe(4);
  });

  it("leaves an unfitted sensor out entirely", () => {
    render(
      <WeatherHistory
        samples={[
          sample({ t: 0, humidity: null, pressure: null }),
          sample({ t: 60_000, temp: 15, humidity: null, pressure: null, wind: 10 }),
        ]}
      />,
    );
    expect(rows()).toEqual(["Temperature", "Wind"]);
  });

  it("shows the latest value, and gradates the axis on round numbers", () => {
    render(
      <WeatherHistory
        samples={[
          sample({ t: 0, temp: 12 }),
          sample({ t: 60_000, temp: 16.4 }),
        ]}
      />,
    );
    const row = document.querySelector(".weather-row");
    // The current reading stays in the head.
    expect(row?.textContent).toContain("16.4");

    // The axis is no longer "whatever the data did" - 12.0 and 16.4 were the
    // old top and bottom labels, and a reader could place nothing between them.
    const axis = Array.from(row?.querySelectorAll(".chart-gutter-tick") ?? []).map(
      (e) => (e.textContent ?? "").trim(),
    );
    // 2-degree steps over 12..18: four gradations, which is what a row this
    // short can carry. The point is that they are round numbers a reader can
    // place a trace against, not that they are any particular count.
    expect(axis).toEqual(["12", "14", "16", "18 °C"]);
  });

  it("draws a gridline for every gradation, behind the trace", () => {
    render(
      <WeatherHistory
        samples={[sample({ t: 0, temp: 12 }), sample({ t: 60_000, temp: 16.4 })]}
      />,
    );
    const row = document.querySelector(".weather-row");
    const ticks = row?.querySelectorAll(".chart-gutter-tick").length ?? 0;
    const lines = row?.querySelectorAll("line.chart-grid").length ?? 0;
    expect(lines).toBe(ticks);

    // Behind: the grid is emitted before the paths, so it cannot mask the data.
    const svg = row?.querySelector("svg");
    const kinds = Array.from(svg?.children ?? []).map((e) => e.tagName);
    expect(kinds.indexOf("line")).toBeLessThan(kinds.indexOf("path"));
  });

  it("gives every row its own scale, because the units do not share one", () => {
    render(
      <WeatherHistory
        samples={[
          sample({ t: 0, temp: 12, pressure: 1003 }),
          sample({ t: 60_000, temp: 16.4, pressure: 1009 }),
        ]}
      />,
    );
    // By label, not by position: the fixture fills every series, so all four
    // rows render and the second one is humidity.
    const rowFor = (label: string) =>
      Array.from(document.querySelectorAll(".weather-row")).find(
        (r) => r.querySelector(".weather-row-label")?.textContent === label,
      )!;
    const top = (row: Element) => {
      const ticks = row.querySelectorAll(".chart-gutter-tick");
      return (ticks[ticks.length - 1]?.textContent ?? "").trim();
    };
    expect(top(rowFor("Temperature"))).toContain("°C");
    expect(top(rowFor("Pressure"))).toContain("hPa");
    expect(top(rowFor("Humidity"))).toContain("%");
  });

  it("puts one time axis under the stack, not one per row", () => {
    render(
      <WeatherHistory
        samples={[
          sample({ t: 0, temp: 12, humidity: 80 }),
          sample({ t: 60_000, temp: 16.4, humidity: 71 }),
        ]}
      />,
    );
    expect(document.querySelectorAll(".weather-row").length).toBe(4);
    // Same samples, same window: four copies of it would be four times the ink.
    expect(document.querySelectorAll(".chart-x").length).toBe(1);
  });

  it("says so rather than drawing an empty box before there is history", () => {
    render(<WeatherHistory samples={[]} loading />);
    expect(document.querySelectorAll(".weather-line").length).toBe(0);
    expect(document.querySelector(".weather-history-empty")?.textContent).toContain(
      "loading",
    );
  });
});
