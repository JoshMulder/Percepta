import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AlertRail } from "./AlertRail";
import { OdinStatusBar } from "./OdinStatusBar";
import { TileWall } from "./TileWall";
import type { FleetEvent, FleetStation } from "../types";

/**
 * The rules the wall is FOR, as opposed to how it happens to look.
 *
 * An operations wall earns its screen by being trustworthy when nobody is
 * looking at it. Three properties carry that, and all three are quiet failures —
 * nothing throws, the screen still renders, and the operator is simply misled:
 *
 *   - a station in trouble is never collapsed out of sight
 *   - the queue ages upward, so the oldest unhandled thing is the most visible
 *   - the screen says how old it is, and keeps saying so while nothing arrives
 */

function station(over: Partial<FleetStation> = {}): FleetStation {
  return {
    id: Math.random().toString(36).slice(2),
    name: "Kennels Road",
    organization_id: "o1",
    organization_name: "SPS",
    latitude: -43.5,
    longitude: 172.5,
    locality: null,
    region: null,
    status: "online",
    dark: false,
    last_seen_at: new Date().toISOString(),
    is_simulated: false,
    model: null,
    config_version: 1,
    ...over,
  };
}

function event(over: Partial<FleetEvent> = {}): FleetEvent {
  return {
    id: Math.random().toString(36).slice(2),
    station_id: "s1",
    station_name: "Kennels Road",
    organization_name: "SPS",
    type: "uplink.down",
    severity: "warning",
    message: "Uplink lost",
    received_at: new Date().toISOString(),
    ...over,
  };
}

const tileNames = () =>
  Array.from(document.querySelectorAll(".odin-tile-name")).map(
    (e) => e.textContent ?? "",
  );

afterEach(cleanup);

describe("the tile wall", () => {
  it("puts trouble first, so it lands in the corner the eye starts from", () => {
    render(
      <TileWall
        stations={[
          station({ name: "Healthy" }),
          station({ name: "Dark", status: "offline", dark: true }),
          station({ name: "Offline", status: "offline" }),
          station({ name: "Never", status: "never", last_seen_at: null }),
        ]}
        selectedId={null}
        onSelect={() => {}}
      />,
    );
    expect(tileNames()).toEqual(["Dark", "Offline", "Never", "Healthy"]);
  });

  it("orders stably between polls, so tiles do not swap with their neighbours", () => {
    // Two stations of identical rank must not trade places on a refresh: on a
    // wall, movement is the alarm channel, and a tile that shuffles for no
    // reason spends it on nothing.
    const a = station({ name: "Alpha", organization_name: "A" });
    const b = station({ name: "Bravo", organization_name: "A" });
    const { rerender } = render(
      <TileWall stations={[a, b]} selectedId={null} onSelect={() => {}} />,
    );
    const first = tileNames();
    rerender(<TileWall stations={[b, a]} selectedId={null} onSelect={() => {}} />);
    expect(tileNames()).toEqual(first);
  });

  describe("collapsing the nominal remainder", () => {
    const many = (n: number, over: Partial<FleetStation> = {}) =>
      Array.from({ length: n }, (_, i) => station({ name: `S${i}`, ...over }));

    it("keeps a tile for every station on a small fleet", () => {
      render(
        <TileWall stations={many(12)} selectedId={null} onSelect={() => {}} />,
      );
      expect(tileNames()).toHaveLength(12);
      expect(document.querySelector(".odin-wall-nominal")).toBeNull();
    });

    it("collapses only the stations with nothing to say", () => {
      const stations = [
        ...many(70),
        station({ name: "Trouble", status: "offline", dark: true }),
      ];
      render(
        <TileWall stations={stations} selectedId={null} onSelect={() => {}} />,
      );
      expect(tileNames()).toContain("Trouble");
      expect(document.querySelector(".odin-wall-nominal")).not.toBeNull();
    });

    it("never collapses a station whose device has failed", () => {
      // The defect this test exists for. A station whose camera has died but
      // whose link and battery are fine is otherwise indistinguishable from a
      // healthy one — so it was classed nominal and collapsed off the wall,
      // which is the single outcome exception-pagination exists to prevent.
      const stations = [
        ...many(70),
        station({ name: "Camera dead", slots: { camera: "failed", radio: "present" } }),
      ];
      render(
        <TileWall stations={stations} selectedId={null} onSelect={() => {}} />,
      );
      expect(tileNames()).toContain("Camera dead");
    });

    it("treats a device that was never fitted as fine", () => {
      // "absent" is not a fault. A camera nobody installed has not failed, and
      // an operator does something completely different about each.
      const stations = [
        ...many(70),
        station({ name: "No camera", slots: { camera: "absent", radio: "present" } }),
      ];
      render(
        <TileWall stations={stations} selectedId={null} onSelect={() => {}} />,
      );
      expect(tileNames()).not.toContain("No camera");
    });
  });
});

describe("the alert rail", () => {
  it("is a queue, not a feed: critical first, then oldest-first", () => {
    const t = (mins: number) =>
      new Date(Date.now() - mins * 60_000).toISOString();
    render(
      <AlertRail
        events={[
          event({ message: "recent warning", received_at: t(1) }),
          event({ message: "old warning", received_at: t(90) }),
          event({ message: "recent critical", severity: "critical", received_at: t(2) }),
        ]}
        onSelectStation={() => {}}
      />,
    );
    const rows = Array.from(document.querySelectorAll(".odin-rail-row")).map(
      (e) => e.textContent ?? "",
    );
    expect(rows[0]).toContain("recent critical");
    // Oldest-first within severity: a queue ages upward. Newest-first would bury
    // the thing that has been waiting longest, which is the thing most likely to
    // have been forgotten.
    expect(rows[1]).toContain("old warning");
    expect(rows[2]).toContain("recent warning");
  });

  it("says nothing is waiting, calmly", () => {
    render(<AlertRail events={[]} onSelectStation={() => {}} />);
    expect(document.querySelector(".odin-rail-empty")).not.toBeNull();
  });
});

describe("the liveness pip", () => {
  it("keeps counting up while no new data arrives", async () => {
    // The whole reason it exists. The view this replaces caught its poll errors
    // and went on rendering a stale fleet with total confidence — and the
    // failure case is precisely the one where no new props arrive, so a pip that
    // only re-rendered on new data could never report that none was coming.
    vi.useFakeTimers();
    const stale = Date.now() - 120_000;
    render(
      <OdinStatusBar
        stations={[station()]}
        unacked={0}
        lastPollAt={stale}
        polling={false}
        error="Could not reach the platform."
      />,
    );
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    const pip = document.querySelector(".odin-pip");
    // "warn", the wall's standard tone modifier — not a bespoke "stale" class.
    expect(pip?.className).toContain("warn");
    // The staleness reads on the label BESIDE the pip, not inside it: the pip
    // is a bare dot carrying the animation, and the text is deliberately not in
    // an aria-live region — a screen reader counting seconds aloud is not a win.
    const bar = document.querySelector(".odin-statusbar");
    expect(bar?.textContent ?? "").toMatch(/\d+\s*s/);
    vi.useRealTimers();
  });

  it("does not colour a zero", () => {
    // "0 dark" in red is a false alarm that never goes away, and an operator
    // learns within a shift to ignore a light that is always on.
    render(
      <OdinStatusBar
        stations={[station(), station()]}
        unacked={0}
        lastPollAt={Date.now()}
        polling
        error={null}
      />,
    );
    const bad = Array.from(document.querySelectorAll(".odin-statusbar-figure.bad"));
    for (const el of bad) {
      expect(Number(el.textContent)).toBeGreaterThan(0);
    }
  });

  it("counts what is actually wrong", () => {
    render(
      <OdinStatusBar
        stations={[
          station(),
          station({ status: "offline" }),
          station({ status: "offline", dark: true }),
        ]}
        unacked={2}
        lastPollAt={Date.now()}
        polling
        error={null}
      />,
    );
    expect(screen.getByText("3")).toBeTruthy();
  });
});
