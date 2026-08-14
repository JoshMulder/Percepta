/**
 * Reproduction harness for the crash seen on logout and on moving to the
 * platform dashboard: "Minified React error #300 — Rendered fewer hooks than
 * expected."
 *
 * The production bundle names no component and the org switcher reloads the page
 * a moment later, so the panel is unreadable in the browser. Driving the same
 * transition here runs the DEV build, where React names the component in the
 * error itself.
 *
 * The transition under test is the one both reports share: `me` goes from a
 * signed-in identity to null, and App swaps which top-level view renders.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Me } from "./types";

const platformAdmin: Me = {
  user_id: "u1",
  email: "admin@example.test",
  display_name: "Admin",
  organization_id: "o1",
  organization_name: "Platform",
  roles: ["admin"],
  demo_mode: false,
  is_platform_admin: true,
  is_guest: false,
};

/** Every API call the dashboard makes on mount, stubbed to something shaped
 *  right and empty, so the render path is exercised without a network. */
const apiStub = {
  me: vi.fn(async () => platformAdmin),
  logout: vi.fn(async () => undefined),
  platform: vi.fn(async () => ({ organizations: [], users: [], roles: [] })),
  platformFleet: vi.fn(async () => ({
    stats: {
      stations_total: 0, stations_online: 0, stations_offline: 0, stations_dark: 0,
      stations_never: 0, stations_no_location: 0, stations_simulated: 0,
      organizations_total: 0, organizations_active: 0,
      faults_critical_24h: 0, faults_warning_24h: 0,
    },
    stations: [],
    recent_events: [],
  })),
  platformAdsb: vi.fn(async () => ({
    aircraft: [], contributing_stations: 0, total_contacts: 0,
  })),
  platformMap: vi.fn(async () => ({
    min_zoom: 3, max_zoom: 17, default_basemap: "osm", basemaps: [], live_fetch: false,
  })),
  organizations: vi.fn(async () => []),
  stations: vi.fn(async () => [{
    id: "s1", name: "Bench", timezone: "UTC", latitude: -43.5, longitude: 172.5,
    last_seen_at: new Date().toISOString(), online: true, is_simulated: false,
    running_version: "v0.3.0", desired_version: null,
  }]),
  station: vi.fn(async () => ({
    id: "s1", name: "Bench", timezone: "UTC", latitude: -43.5, longitude: 172.5,
    last_seen_at: new Date().toISOString(), online: true, is_simulated: false,
    capabilities: ["station.view","telemetry.view","video.view","radio.listen","radio.control","config.write","station.update"],
    devices: [],
  })),
  mapConfig: vi.fn(async () => ({
    latitude: -43.5, longitude: 172.5, min_zoom: 3, max_zoom: 17, radius_km: 50,
    cached_at: null, default_basemap: "osm", basemaps: [], live_fetch: false,
  })),
  powerHistory: vi.fn(async () => []),
  weatherHistory: vi.fn(async () => []),
  radioPresets: vi.fn(async () => [null, null, null, null]),
  radioTranscripts: vi.fn(async () => []),
  // Odin's own reads. Missing, these threw straight out of an effect as an
  // unhandled rejection: the assertions still passed, because the crash
  // happened after the render they were checking, and the suite reported green
  // with a live TypeError in it.
  odinAlerts: vi.fn(async () => []),
  odinTranscripts: vi.fn(async () => []),
  releases: vi.fn(async () => []),
  latestRelease: vi.fn(async () => ({
    tag: null, image: null, notes: null, published_at: null,
  })),
};

vi.mock("./api", () => ({
  api: apiStub,
  ApiError: class ApiError extends Error {
    constructor(public status: number, message: string) {
      super(message);
    }
  },
  // App registers a handler here; the test captures it to force the 401 path.
  setUnauthorizedHandler: (h: (() => void) | null) => {
    unauthorized = h;
  },
}));

let unauthorized: (() => void) | null = null;

// jsdom implements neither of these; Console asks for both on mount. Stubbed so
// the render proceeds far enough to exercise the transition under test.
window.matchMedia = ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia;
(globalThis as unknown as { WebSocket: unknown }).WebSocket = class {
  close() {}
  send() {}
  addEventListener() {}
  removeEventListener() {}
};

// maplibre touches canvas APIs jsdom does not implement; the crash is not in the
// map, so it is stubbed out entirely rather than shimmed.
vi.mock("maplibre-gl", () => ({
  default: {
    Map: class {
      on() {} off() {} remove() {} addControl() {} setCenter() {}
      getCanvas() { return { style: {} }; }
      isStyleLoaded() { return false; }
    },
    Marker: class {
      setLngLat() { return this; } addTo() { return this; }
      remove() {} getElement() { return document.createElement("div"); }
    },
  },
}));

afterEach(() => {
  unauthorized = null;
  vi.clearAllMocks();
});

describe("the sign-out transition", () => {
  it("does not crash when the identity goes away", async () => {
    const { App } = await import("./App");
    const errors: unknown[] = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...args) => {
      errors.push(args[0]);
    });

    render(<App />);
    // Wait for the dashboard to settle — the crash reports are all AFTER the
    // signed-in view is up.
    await waitFor(() => expect(apiStub.me).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });

    // The logout path: whatever clears the session drops `me` to null and App
    // swaps to the login view.
    await act(async () => {
      unauthorized?.();
      await Promise.resolve();
    });

    const hookErrors = errors
      .map((e) => (e instanceof Error ? e.message : String(e)))
      .filter((m) => /fewer hooks|more hooks|Rendered/i.test(m));
    spy.mockRestore();
    expect(hookErrors, `React hook-order error on sign-out:\n${hookErrors.join("\n")}`)
      .toHaveLength(0);
    expect(screen.queryByText(/Something went wrong/i)).toBeNull();
  });

  it("does not crash when a station operator signs out of the console", async () => {
    // The heavier path: Console, not the platform dashboard — sockets, panels,
    // video and audio hooks all mounted, then the identity goes away.
    apiStub.me.mockResolvedValue({ ...platformAdmin, is_platform_admin: false });
    const { App } = await import("./App");
    const errors: unknown[] = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...args) => {
      errors.push(args[0]);
    });

    render(<App />);
    await waitFor(() => expect(apiStub.stations).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      unauthorized?.();
      await Promise.resolve();
    });

    const hookErrors = errors
      .map((e) => (e instanceof Error ? e.message : String(e)))
      .filter((m) => /fewer hooks|more hooks/i.test(m));
    spy.mockRestore();
    expect(
      hookErrors,
      `React hook-order error on console sign-out: ${hookErrors.join(" | ")}`,
    ).toHaveLength(0);
  });
});
