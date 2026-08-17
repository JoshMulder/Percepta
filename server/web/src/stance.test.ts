import { describe, expect, it } from "vitest";

import type { FleetStation } from "./types";
import { COST, DEFAULT_BUDGET, allocate, isNominal, withPosters } from "./stance";

/**
 * The allocator, which is the whole of STANCE that can be tested.
 *
 * jsdom computes no layout and no styles, so nothing about how this LOOKS is
 * reachable from a test — which is exactly why the decision was pulled out of
 * the component into a pure function. What is tested here is the thing that
 * would actually go wrong: a healthy fleet claiming the area a failing station
 * needs, a station dropping off the wall entirely, or a chip being put on
 * camera duty.
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
const broken = (over: Partial<FleetStation> = {}) =>
  station({ dark: true, status: "offline", ...over });

describe("a fleet with nothing wrong", () => {
  it("gives every station the same form", () => {
    // Uniform is the correct picture of a uniform fleet. Promoting two of thirty
    // healthy sites to full-width strips, because they happen to sort first,
    // spends the wall's loudest signal on alphabetical order.
    const forms = allocate(fine(30));
    expect(new Set(forms).size).toBe(1);
  });

  it("gives three stations the whole wall each", () => {
    // The complaint that started this: three stations and a grid sized for
    // sixty, so the picture is too small to be worth sending.
    expect(allocate(fine(3))).toEqual(["strip", "strip", "strip"]);
  });

  it("steps down as the fleet grows rather than shrinking one form", () => {
    const forms = (n: number) => allocate(fine(n))[0];
    expect(forms(3)).toBe("strip");
    expect(forms(8)).toBe("card");
    expect(forms(60)).toBe("chip");
  });

  it("never returns fewer forms than stations", () => {
    // Every station gets a form. A station with no form is a station that has
    // silently left the wall, which is the one outcome this must never produce.
    for (const n of [1, 2, 5, 17, 30, 100, 500]) {
      expect(allocate(fine(n))).toHaveLength(n);
    }
  });
});

describe("a fleet with something wrong", () => {
  it("hands the trouble the biggest form and caps the healthy at a card", () => {
    const forms = allocate([broken(), ...fine(20)]);
    expect(forms[0]).toBe("strip");
    // However much room is left, a fine station has not earned more than this.
    expect(forms.slice(1).every((f) => f === "card" || f === "chip")).toBe(true);
  });

  it("does not let a healthy fleet outrank a failing station", () => {
    // The regression that matters. If the uniform rule ran here, thirty healthy
    // stations would each take a card and the dark one would be one of thirty
    // identical cards — the wall would have no opinion at the moment it most
    // needs one.
    const forms = allocate([broken(), ...fine(29)]);
    expect(forms[0]).toBe("strip");
    expect(forms[0]).not.toBe(forms[1]);
  });

  it("never spends more area than the wall has", () => {
    // The allocation rule's whole invariant: take the largest form you can have
    // such that every station below you still gets at least a chip. `hidden`
    // costs nothing because nothing is drawn.
    for (const n of [2, 3, 10, 40, 200]) {
      const forms = allocate([broken(), broken(), ...fine(n)]);
      expect(forms).toHaveLength(n + 2);
      const spent = forms
        .filter((f) => f !== "hidden")
        .reduce((t, f) => t + COST[f], 0);
      expect(spent).toBeLessThanOrEqual(DEFAULT_BUDGET);
    }
  });

  it("still gives the worst station a picture on an enormous fleet", () => {
    // The failure this caught. The greedy rule alone degenerates once the fleet
    // is longer than the wall can hold as chips: every candidate form fails the
    // "everyone below gets a chip" test, so EVERY station falls to a chip —
    // including the one that is on fire. A wall whose premise is spending area
    // on trouble must not go blank precisely when there is trouble.
    const forms = allocate([broken(), ...fine(400)]);
    expect(forms).toHaveLength(401);
    expect(["strip", "panel"]).toContain(forms[0]);
  });

  it("collapses the calmest stations, never the cases", () => {
    const fleet = [broken(), broken(), ...fine(400)];
    const forms = allocate(fleet);
    expect(forms.filter((f) => f === "hidden").length).toBeGreaterThan(0);
    fleet.forEach((s, i) => {
      if (!isNominal(s)) expect(forms[i]).not.toBe("hidden");
    });
  });
});

describe("the open station", () => {
  it("keeps a drawn form even when it is perfectly fine", () => {
    // A tile that shrinks to a chip at the moment it is clicked — because
    // opening it is how you found out it was nominal — is a wall arguing with
    // the person using it.
    // A chip IS drawn — it is a row on the wall — so the requirement is not
    // that the open station gets a big form, it is that it never falls off the
    // wall entirely while its own drawer is open.
    const open = station({ id: "open" });
    const forms = allocate([...fine(400), open], { promote: "open" });
    expect(forms[forms.length - 1]).not.toBe("hidden");
    // And on a fleet with room, being open earns it a picture.
    const small = allocate([...fine(20), station({ id: "open" })], { promote: "open" });
    expect(small[small.length - 1]).not.toBe("chip");
  });
});

describe("who is asked for a picture", () => {
  it("never puts a chip on camera duty", () => {
    // A chip has nowhere to show a picture, and a station on camera duty opens
    // its lens once a minute on a board whose supply cannot hold its own peak.
    const fleet = [broken(), ...fine(200)];
    const forms = allocate(fleet);
    const asked = new Set(withPosters(fleet, forms));
    fleet.forEach((s, i) => {
      if (forms[i] === "chip") expect(asked.has(s.id)).toBe(false);
    });
    expect(asked.size).toBeGreaterThan(0);
  });

  it("asks nobody when the wall is empty", () => {
    expect(withPosters([], allocate([]))).toEqual([]);
  });
});
