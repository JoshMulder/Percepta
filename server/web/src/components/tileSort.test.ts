import { describe, expect, it } from "vitest";

import { alertIndex } from "./TileWall";

/**
 * The wall's sort, after it learned about alerts.
 *
 * Before this, `rank()` read reachability only — dark, offline, never, else —
 * so an online station with five open criticals sorted identically to a nominal
 * one and landed wherever its organisation's name fell alphabetically. "Trouble
 * is top-left" was true only of stations that had stopped talking. The request
 * for "stations with alerts move to the top" was a new feature, not a
 * description of what the wall did.
 */

function alert(over: Partial<{ ground_station_id: string; severity: string; state: string }> = {}) {
  return {
    ground_station_id: "a",
    severity: "critical",
    state: "open",
    ...over,
  };
}

describe("alertIndex", () => {
  it("counts criticals and warnings separately per station", () => {
    const index = alertIndex([
      alert({ ground_station_id: "a", severity: "critical" }),
      alert({ ground_station_id: "a", severity: "warning" }),
      alert({ ground_station_id: "b", severity: "warning" }),
    ]);
    expect(index.a).toEqual({
      critical: 1,
      unackedCritical: 1,
      warning: 1,
      unackedWarning: 1,
    });
    expect(index.b.critical).toBe(0);
    expect(index.b.warning).toBe(1);
  });

  it("separates acked from unacked", () => {
    // The distinction the whole promotion order rests on: acking says somebody
    // has it, so the tile steps aside for one nobody has picked up — without
    // leaving the trouble tiers, because acking is not fixing.
    const index = alertIndex([
      alert({ ground_station_id: "a", state: "acked" }),
      alert({ ground_station_id: "a", state: "open" }),
    ]);
    expect(index.a.critical).toBe(2);
    expect(index.a.unackedCritical).toBe(1);
  });

  it("ignores severities it does not rank", () => {
    // info alerts exist and must not promote a tile — the wall's colour and
    // ordering are rationed for things that need somebody.
    const index = alertIndex([alert({ severity: "info" })]);
    expect(index.a).toEqual({
      critical: 0,
      unackedCritical: 0,
      warning: 0,
      unackedWarning: 0,
    });
  });

  it("is empty for a quiet fleet", () => {
    expect(alertIndex([])).toEqual({});
  });
});
