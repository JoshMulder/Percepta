import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { FleetStation } from "../types";
import { StationTile } from "./StationTile";

// Vitest runs without globals here, so testing-library does not auto-clean.
afterEach(cleanup);

/**
 * The weather line on a wall tile.
 *
 * The tests that matter are the absences and the edges. A site with no weather
 * head, a dead-calm day, a steady wind with no gust — each renders differently
 * from "the sensor is failing", and conflating those is how a wall starts
 * telling an operator something that is not true.
 */

function station(over: Partial<FleetStation> = {}): FleetStation {
  return {
    id: "s1",
    name: "Kennels Road",
    organization_id: "o1",
    organization_name: "SPS Automation",
    latitude: -44.33,
    longitude: 171.24,
    locality: "Timaru",
    region: "Canterbury",
    status: "online",
    dark: false,
    last_seen_at: null,
    is_simulated: false,
    model: null,
    config_version: 1,
    condition_count: 0,
    ...over,
  } as FleetStation;
}

function show(over: Partial<FleetStation> = {}) {
  render(
    <StationTile station={station(over)} selected={false} onSelect={() => {}} />,
  );
}

describe("the tile's weather line", () => {
  it("shows nothing at all when the site has no weather head", () => {
    // Not dashes. A row of dashes claims a failing sensor, which is a different
    // and more alarming thing than a site that never had one.
    show();
    expect(document.querySelector(".odin-tile-wx")).toBeNull();
  });

  it("shows wind, temperature and visibility when they are there", () => {
    show({ wind_kt: 12, temperature_c: 8.4, visibility_km: 30 });
    const line = document.querySelector(".odin-tile-wx");
    expect(line).not.toBeNull();
    expect(line!.textContent).toContain("12");
    expect(line!.textContent).toContain("8.4");
    expect(line!.textContent).toContain("30");
  });

  it("shows a gust range only when it is actually gusting", () => {
    show({ wind_kt: 12, gust_kt: 12 });
    expect(document.querySelector(".odin-tile-wx")!.textContent).not.toContain("–");
    cleanup();
    show({ wind_kt: 12, gust_kt: 25 });
    expect(document.querySelector(".odin-tile-wx")!.textContent).toContain("–");
  });

  it("renders a dead calm and a freezing morning rather than hiding them", () => {
    // Zero is a reading. A truthiness test would drop both and produce a tile
    // that looks like a station with no sensors on the coldest, stillest day.
    show({ wind_kt: 0, temperature_c: 0 });
    const line = document.querySelector(".odin-tile-wx")!;
    expect(line.textContent).toMatch(/0/);
    expect(document.querySelector(".odin-tile-wx-wind")).not.toBeNull();
  });

  it("renders with only one of the three present", () => {
    // A station may have a thermometer and no anemometer. Partial is normal.
    show({ temperature_c: 11.2 });
    expect(document.querySelector(".odin-tile-wx")!.textContent).toContain("11.2");
  });

  it("puts the weather into the spoken label", () => {
    // Read aloud, the line is three bare numbers with unit glyphs.
    show({ wind_kt: 12, gust_kt: 25, temperature_c: 8.4 });
    const label = screen.getByRole("button").getAttribute("aria-label") ?? "";
    expect(label).toMatch(/wind/i);
    expect(label).toMatch(/gusting/i);
  });
});
