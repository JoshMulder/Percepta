import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FleetStation } from "../types";
import type { WatchApi } from "../useWatchAudio";
import { WatchStrip } from "./WatchStrip";

// Vitest runs without globals here, so testing-library does not auto-clean.
afterEach(cleanup);

/**
 * The strip, without an AudioContext.
 *
 * jsdom has no Web Audio at all, so what is tested here is the part that would
 * still be wrong if the audio were perfect: which channels are shown, what the
 * controls do to the guard set, and whether the failure states say anything. The
 * sound itself is unreachable from a test runner and is not pretended at.
 */

function station(id: string, name: string): FleetStation {
  return {
    id,
    name,
    organization_id: "o1",
    organization_name: "Wildlife Trust",
    latitude: null,
    longitude: null,
    locality: null,
    region: null,
    status: "online",
    dark: false,
    last_seen_at: null,
    is_simulated: false,
    model: null,
    config_version: 1,
  } as FleetStation;
}

function watchApi(overrides: Partial<WatchApi> = {}): WatchApi {
  return {
    guarded: [],
    setGuarded: vi.fn(),
    setPosters: vi.fn(),
    talking: {},
    link: "open",
    audioState: "playing",
    volume: 0,
    setVolume: vi.fn(),
    priority: null,
    setPriority: vi.fn(),
    replay: vi.fn(() => true),
    ...overrides,
  };
}

describe("WatchStrip", () => {
  it("names each guarded channel", () => {
    const stations = [station("a", "Kennels Road"), station("b", "Ridge Top")];
    render(
      <WatchStrip
        stations={stations}
        watch={watchApi({ guarded: ["a", "b"] })}
      />,
    );
    expect(screen.getByText("Kennels Road")).toBeTruthy();
    expect(screen.getByText("Ridge Top")).toBeTruthy();
  });

  it("releases a channel by removing it from the set", () => {
    const setGuarded = vi.fn();
    render(
      <WatchStrip
        stations={[station("a", "Kennels Road"), station("b", "Ridge Top")]}
        watch={watchApi({ guarded: ["a", "b"], setGuarded })}
      />,
    );
    fireEvent.click(screen.getByLabelText("Release Kennels Road"));
    // The WHOLE set, not a remove message — the server replaces rather than
    // accumulating, which is what makes reconnect and toggling the same thing.
    expect(setGuarded).toHaveBeenCalledWith(["b"]);
  });

  it("clears priority when the priority channel is released", () => {
    // Otherwise every other channel stays ducked under a channel that is no
    // longer there, and the strip gets quieter for no visible reason.
    const setPriority = vi.fn();
    render(
      <WatchStrip
        stations={[station("a", "Kennels Road"), station("b", "Ridge Top")]}
        watch={watchApi({
          guarded: ["a", "b"],
          priority: "a",
          setPriority,
        })}
      />,
    );
    fireEvent.click(screen.getByLabelText("Release Kennels Road"));
    expect(setPriority).toHaveBeenCalledWith(null);
  });

  it("refuses a ninth channel", () => {
    const stations = Array.from({ length: 10 }, (_, i) =>
      station(`s${i}`, `Station ${i}`),
    );
    render(
      <WatchStrip
        stations={stations}
        watch={watchApi({ guarded: stations.slice(0, 8).map((s) => s.id) })}
      />,
    );
    const add = screen.getByTitle("At most 8 channels") as HTMLButtonElement;
    expect(add.disabled).toBe(true);
  });

  it("says so when the browser cannot decode Opus", () => {
    // Failing into silence is the one thing this must not do: an operator who
    // hears nothing needs to know it is their browser and not the site.
    render(
      <WatchStrip
        stations={[station("a", "Kennels Road")]}
        watch={watchApi({ guarded: ["a"], audioState: "unsupported" })}
      />,
    );
    expect(screen.getByText(/no Opus decoder/i)).toBeTruthy();
  });

  it("says so when watch access is refused", () => {
    render(
      <WatchStrip
        stations={[station("a", "Kennels Road")]}
        watch={watchApi({ link: "denied" })}
      />,
    );
    expect(screen.getByText(/watch access refused/i)).toBeTruthy();
  });

  it("shows nothing guarded without looking broken", () => {
    render(<WatchStrip stations={[station("a", "K")]} watch={watchApi()} />);
    expect(screen.getByText("No channels guarded")).toBeTruthy();
  });
});
