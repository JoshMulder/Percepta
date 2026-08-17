import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FleetStation } from "../types";
import { StationTile } from "./StationTile";
import { TileWall } from "./TileWall";

// Vitest runs without globals here, so testing-library does not auto-clean.
afterEach(cleanup);

/**
 * The picture on a wall tile, and who the wall asks for one.
 *
 * Two rules are worth a test each, and both are about NOT showing something.
 * A tile with no recent picture must show none rather than a stale one — the
 * cache expires a poster after three minutes precisely so a station that
 * stopped sending, or refused on low battery, goes back to an empty face. And
 * the wall must ask only the stations it is actually showing: the collapsed
 * nominal count has no tiles, and asking those stations anyway would put a
 * fleet of field cameras on duty for a row of text.
 *
 * The third is the cache-buster, which has no visible symptom until it is
 * missing: an <img> never re-fetches a stable src, so without `?v=` the wall
 * would hold its first picture for the whole shift and look perfectly fine
 * doing it.
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

function poster(): HTMLImageElement | null {
  return document.querySelector<HTMLImageElement>(".odin-tile-poster");
}

function show(over: Partial<FleetStation> = {}) {
  return render(
    <StationTile station={station(over)} selected={false} onSelect={() => {}} />,
  );
}

describe("the poster on a tile", () => {
  it("shows nothing when the station has no recent picture", () => {
    // Not a stale frame and not a broken-image glyph. A wall exists to be
    // believed, and a five-minute-old photograph of a place is not what that
    // place looks like now.
    show();
    expect(poster()).toBeNull();
  });

  it("renders the station's own poster endpoint when there is one", () => {
    show({ poster_at: "2026-08-17T03:00:00+00:00" });
    const img = poster();
    expect(img).not.toBeNull();
    expect(img!.getAttribute("src")).toContain("/api/odin/stations/s1/poster");
  });

  it("carries the stamp as a cache-buster", () => {
    // Without this the browser never re-requests: an <img> exposes no response
    // headers to the page, so a stable src is simply never fetched again and a
    // tile holds its first picture until somebody reloads the wall.
    show({ poster_at: "2026-08-17T03:00:00+00:00" });
    const src = poster()!.getAttribute("src") ?? "";
    expect(src).toContain("?v=");
    expect(decodeURIComponent(src.split("?v=")[1])).toBe("2026-08-17T03:00:00+00:00");
  });

  it("changes the url when a newer picture arrives", () => {
    const { rerender } = show({ poster_at: "2026-08-17T03:00:00+00:00" });
    const first = poster()!.getAttribute("src");
    rerender(
      <StationTile
        station={station({ poster_at: "2026-08-17T03:01:00+00:00" })}
        selected={false}
        onSelect={() => {}}
      />,
    );
    expect(poster()!.getAttribute("src")).not.toBe(first);
  });

  it("says nothing extra to a screen reader", () => {
    // The tile's own label already names the station and its state. An image
    // announced here is one more thing to skip past and nothing to act on.
    show({ poster_at: "2026-08-17T03:00:00+00:00" });
    expect(poster()!.getAttribute("alt")).toBe("");
    expect(poster()!.getAttribute("aria-hidden")).toBe("true");
  });
});

describe("which stations the wall asks for a picture", () => {
  it("names the stations that actually have a tile", () => {
    const onShowing = vi.fn();
    render(
      <TileWall
        stations={[station({ id: "a" }), station({ id: "b" })]}
        selectedId={null}
        onSelect={() => {}}
        onShowing={onShowing}
      />,
    );
    expect(onShowing).toHaveBeenCalledWith(["a", "b"]);
  });

  it("does not re-ask when the wall merely reorders", () => {
    // The wall re-sorts on every poll as ranks change. A reorder is not a change
    // of membership, and re-declaring the set every three seconds would be a
    // burst of commands to every station on the wall to tell them what they
    // already know.
    const onShowing = vi.fn();
    const props = {
      selectedId: null,
      onSelect: () => {},
      onShowing,
    };
    const { rerender } = render(
      <TileWall stations={[station({ id: "a" }), station({ id: "b" })]} {...props} />,
    );
    expect(onShowing).toHaveBeenCalledTimes(1);

    rerender(
      <TileWall stations={[station({ id: "b" }), station({ id: "a" })]} {...props} />,
    );
    expect(onShowing).toHaveBeenCalledTimes(1);
  });
});
