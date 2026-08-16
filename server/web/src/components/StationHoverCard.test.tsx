import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { FleetStation } from "../types";
import { StationHoverCard } from "./StationHoverCard";

// Vitest runs without globals here, so testing-library does not auto-clean.
afterEach(cleanup);

/**
 * The free half of the map's interaction.
 *
 * Everything on this card comes from data the wall is already holding, which is
 * what lets hover be free and click be the deliberate act. The tests that matter
 * are the ABSENCES: a site with no weather head, a station that has never
 * connected, a reading of zero. Each renders differently from "we have not heard
 * yet", and getting those the same way round is how a wall starts lying quietly.
 */

function station(over: Partial<FleetStation> = {}): FleetStation {
  return {
    id: "s1",
    name: "Kennels Road",
    organization_id: "o1",
    organization_name: "SPS Automation",
    latitude: -43.5,
    longitude: 172.6,
    locality: "Kennels Rd",
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
    <StationHoverCard
      station={station(over)}
      x={100}
      y={100}
      flipX={false}
      flipY={false}
    />,
  );
}

describe("StationHoverCard", () => {
  it("names the station and where it is", () => {
    show();
    expect(screen.getByText("Kennels Road")).toBeTruthy();
    expect(screen.getByText(/SPS Automation/)).toBeTruthy();
  });

  it("omits the weather row entirely when the site has no weather head", () => {
    // Four dashes would imply a sensor that is failing rather than a site that
    // has none — a distinction the console draws everywhere else.
    show();
    expect(screen.queryByText(/km vis/)).toBeNull();
  });

  it("shows a wind range only when there is a gust above the mean", () => {
    show({ wind_kt: 12, gust_kt: 12 });
    expect(screen.queryByText(/12–25/)).toBeNull();
    cleanup();
    show({ wind_kt: 12, gust_kt: 25 });
    expect(screen.getByText(/12–25/)).toBeTruthy();
  });

  it("renders a legitimate zero rather than hiding it", () => {
    // Calm and freezing are readings. A truthiness test would drop both and
    // show a card that looks like a station with no sensors at all.
    show({ wind_kt: 0, temperature_c: 0, soc_pct: 0 });
    expect(screen.getByText(/0kt/)).toBeTruthy();
    expect(screen.getByText(/0%/)).toBeTruthy();
  });

  it("says 'never connected' rather than the raw status word", () => {
    show({ status: "never" });
    expect(screen.getByText("never connected")).toBeTruthy();
  });

  it("calls a dark station dark, not offline", () => {
    // `dark` is a modifier on offline, and the wall's whole point is that the
    // two mean different things to whoever is on shift.
    show({ status: "offline", dark: true });
    expect(screen.getByText("dark")).toBeTruthy();
  });

  it("names the worst condition and counts the rest", () => {
    show({ worst_condition: "power.undervoltage", condition_count: 3 });
    expect(screen.getByText(/power\.undervoltage/)).toBeTruthy();
    expect(screen.getByText(/\+2/)).toBeTruthy();
  });

  it("is inert to the pointer and to screen readers", () => {
    // Load-bearing: if the card took the pointer, moving toward it would steal
    // the hover from the pin, close the card, and start a flicker loop.
    const { container } = render(
      <StationHoverCard
        station={station()}
        x={0}
        y={0}
        flipX={false}
        flipY={false}
      />,
    );
    expect(container.firstElementChild?.className).toContain("fleet-hover");
    expect(container.firstElementChild?.getAttribute("aria-hidden")).toBe("true");
  });
});
