import { describe, expect, it } from "vitest";

import type { FleetStation } from "./types";
import {
  BUCKET_MS,
  LEAD_LIMIT,
  STORM_AT,
  geometryFor,
  groupStorm,
  split,
  withPosters,
} from "./quiet";

/**
 * The split, the storm grouping, and who ends up on camera duty.
 *
 * These are the parts of NOTHING TO REPORT that can be wrong in a way a person
 * would not notice: a healthy fleet quietly putting cameras on duty, a mass
 * outage rendered as N separate problems, or the wall picking a lead when there
 * is no such thing as "the worst".
 */

function station(over: Partial<FleetStation> = {}): FleetStation {
  return {
    id: Math.random().toString(36).slice(2),
    name: "Site",
    organization_id: "o1",
    organization_name: "SPS Automation",
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
    condition_count: 0,
    ...over,
  } as FleetStation;
}

const fine = (n: number) => Array.from({ length: n }, () => station());
const down = (over: Partial<FleetStation> = {}) =>
  station({ status: "offline", ...over });

describe("the three geometries", () => {
  it("is quiet with nothing wrong, whatever the fleet size", () => {
    // The property the whole concept rests on: occupied area tracks TROUBLE,
    // not fleet size. Three healthy stations and three hundred render the same.
    expect(split(fine(3)).geometry).toBe("quiet");
    expect(split(fine(300)).geometry).toBe("quiet");
  });

  it("gives one case the lead", () => {
    const s = split([down(), ...fine(20)]);
    expect(s.geometry).toBe("cases");
    expect(s.lead).not.toBeNull();
    expect(s.files).toHaveLength(0);
    expect(s.muster).toHaveLength(20);
  });

  it("stops picking a lead once everything is equally on fire", () => {
    // Past the limit there is no "the worst thing", and spending the best real
    // estate on an arbitrary pick among equals is worse than not picking.
    const s = split([...Array.from({ length: STORM_AT }, () => down()), ...fine(5)]);
    expect(s.geometry).toBe("storm");
    expect(s.lead).toBeNull();
    expect(s.files).toHaveLength(STORM_AT);
  });

  it("switches shape exactly at the stated thresholds", () => {
    expect(geometryFor(0)).toBe("quiet");
    expect(geometryFor(1)).toBe("cases");
    expect(geometryFor(LEAD_LIMIT)).toBe("cases");
    expect(geometryFor(STORM_AT)).toBe("storm");
  });
});

describe("who is on camera duty", () => {
  it("puts nobody on duty when the fleet is healthy", () => {
    // The reason this concept matters on this hardware. Every station asked for
    // a picture opens its camera once a minute, on a board whose supply already
    // cannot hold its own peak. The grid asks every drawn tile; this asks none.
    expect(withPosters(split(fine(50)))).toEqual([]);
  });

  it("asks only the cases", () => {
    const bad = down();
    const asked = withPosters(split([bad, ...fine(30)]));
    expect(asked).toEqual([bad.id]);
  });

  it("asks nobody during a storm", () => {
    // Twenty stations are down and none of them is going to answer a camera
    // request anyway; the ones that would are the ones whose supply is already
    // the suspect.
    const asked = withPosters(split(Array.from({ length: 20 }, () => down())));
    expect(asked).toEqual([]);
  });
});

describe("the open station", () => {
  it("becomes a case while its drawer is open", () => {
    // A wall that empties the instant you click something is arguing with the
    // person using it.
    const open = station({ id: "open" });
    const s = split([...fine(10), open], { promote: "open" });
    expect(s.geometry).toBe("cases");
    expect(s.lead?.id).toBe("open");
    expect(withPosters(s)).toEqual(["open"]);
  });
});

describe("grouping a storm by its cause", () => {
  const at = (iso: string) => ({ last_seen_at: iso });

  it("reads one provider outage as one case, not fourteen", () => {
    // The single most valuable fact during a mass outage, and nothing on the
    // wall today can say it. Fourteen identical tiles say "fourteen problems"
    // when the truth is one, and an operator who reads fourteen starts
    // fourteen investigations.
    const fleet = Array.from({ length: 14 }, (_, i) =>
      down({
        organization_name: "Meridian Air",
        ...at(new Date(Date.parse("2026-08-18T03:11:00Z") + i * 4000).toISOString()),
      }),
    );
    const groups = groupStorm(fleet);
    expect(groups).toHaveLength(1);
    expect(groups[0].stations).toHaveLength(14);
    expect(groups[0].cause).toBe("offline");
    expect(groups[0].organization).toBe("Meridian Air");
  });

  it("keeps two unrelated outages apart", () => {
    const groups = groupStorm([
      down({ organization_name: "Meridian Air", ...at("2026-08-18T03:11:00Z") }),
      down({ organization_name: "SPS Automation", ...at("2026-08-18T03:11:00Z") }),
    ]);
    expect(groups).toHaveLength(2);
  });

  it("does not merge failures an hour apart at the same customer", () => {
    const groups = groupStorm([
      down({ organization_name: "Meridian Air", ...at("2026-08-18T03:11:00Z") }),
      down({ organization_name: "Meridian Air", ...at("2026-08-18T04:11:00Z") }),
    ]);
    expect(groups).toHaveLength(2);
  });

  it("separates a dark station from a merely offline one", () => {
    // Two different questions. Offline is "we have not heard from it lately",
    // which a satellite uplink does on its own; dark is "it is gone", which
    // somebody has to drive to.
    const groups = groupStorm([
      down({ ...at("2026-08-18T03:11:00Z") }),
      down({ dark: true, ...at("2026-08-18T03:11:30Z") }),
    ]);
    expect(groups.map((g) => g.cause).sort()).toEqual(["dark", "offline"]);
  });

  it("reports the oldest onset in a group", () => {
    const first = Date.parse("2026-08-18T03:11:00Z");
    const groups = groupStorm([
      down({ ...at(new Date(first + 60_000).toISOString()) }),
      down({ ...at(new Date(first).toISOString()) }),
    ]);
    expect(groups[0].since).toBe(first);
  });

  it("survives a station that has never reported", () => {
    const groups = groupStorm([down(), down()]);
    expect(groups).toHaveLength(1);
    expect(groups[0].since).toBeNull();
  });

  it("orders the biggest common cause first", () => {
    const base = Date.parse("2026-08-18T03:11:00Z");
    const many = Array.from({ length: 5 }, () =>
      down({ organization_name: "Meridian Air", ...at(new Date(base).toISOString()) }),
    );
    const one = down({
      organization_name: "Zulu Ltd",
      ...at(new Date(base + BUCKET_MS * 3).toISOString()),
    });
    expect(groupStorm([one, ...many])[0].stations).toHaveLength(5);
  });
});
