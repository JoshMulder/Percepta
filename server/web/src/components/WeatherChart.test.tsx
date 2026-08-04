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

  it("shows the latest value and a y-axis of the window's range", () => {
    render(
      <WeatherHistory
        samples={[
          sample({ t: 0, temp: 12 }),
          sample({ t: 60_000, temp: 16.4 }),
        ]}
      />,
    );
    const row = document.querySelector(".weather-row");
    // The current value in the head, and the range as the axis top and bottom.
    expect(row?.textContent).toContain("16.4");
    const axis = Array.from(row?.querySelectorAll(".chart-y") ?? []).map(
      (e) => e.textContent,
    );
    expect(axis).toContain("16.4 °C");
    expect(axis).toContain("12.0 °C");
  });

  it("says so rather than drawing an empty box before there is history", () => {
    render(<WeatherHistory samples={[]} loading />);
    expect(document.querySelectorAll(".weather-line").length).toBe(0);
    expect(document.querySelector(".weather-history-empty")?.textContent).toContain(
      "loading",
    );
  });
});
