import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { PowerPayload } from "../types";
import { PowerFlow } from "./PowerFlow";

/**
 * The power flow diagram.
 *
 * Nearly everything here is one rule: **a source that is not fitted is not
 * drawn, and a fitted source contributing nothing is drawn dim.** A site with
 * no grid connection and a site whose grid has failed are completely different
 * situations, and `mains_w: 0` describes both — so the station omits the field
 * entirely at a site with no mains, and this must not turn that absence into a
 * zero. A `?? 0` or a falsy check anywhere in the component collapses the two
 * and draws a dead generator at a site that has never had one.
 */

afterEach(cleanup);

function payload(overrides: Partial<PowerPayload> = {}): PowerPayload {
  return {
    kind: "power",
    soc_pct: 80,
    battery_v: 49.2,
    pv_w: 400,
    load_w: 120,
    runtime_h: null,
    battery_w: 280,
    ...overrides,
  };
}

function labels(): string[] {
  return Array.from(document.querySelectorAll(".pf-label")).map(
    (e) => e.textContent ?? "",
  );
}

function nodeFor(label: string): Element | undefined {
  return Array.from(document.querySelectorAll(".pf-node")).find(
    (g) => g.querySelector(".pf-label")?.textContent === label,
  );
}

/** The rail's y in the drawing's own coordinates. */
const BUS_Y = 58;

/** Every coordinate pair in an element's path, in order. */
function pointsOf(g: Element): [number, number][] {
  const d = g.querySelector("path")!.getAttribute("d")!;
  return Array.from(d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g))
    .map((m) => [Number(m[1]), Number(m[2])]);
}

function pathPoints(selector: string): [number, number][] {
  return pointsOf(document.querySelector(selector)!);
}

describe("sources that do not exist", () => {
  it("draws no mains at a site with no grid connection", () => {
    render(<PowerFlow power={payload()} />);
    expect(labels().some((l) => l.startsWith("AC In"))).toBe(false);
  });

  it("draws no generator at a site without one", () => {
    render(<PowerFlow power={payload()} />);
    expect(labels().some((l) => l.startsWith("Generator"))).toBe(false);
  });

  it("always draws solar, the battery and the load", () => {
    render(<PowerFlow power={payload()} />);
    expect(labels()).toContain("Solar");
    expect(labels()).toContain("Load");
    // Charging or not, the battery is always part of the picture.
    expect(labels().some((l) => l === "Battery" || l === "Charging")).toBe(true);
  });
});

describe("sources that exist but are giving nothing", () => {
  it("draws a fitted mains input at zero, dim rather than absent", () => {
    // The fault case. Absent would say "this site has no grid", which is the
    // opposite of what a grid failure means.
    render(<PowerFlow power={payload({ mains_present: false, mains_w: 0 })} />);
    const node = nodeFor("AC In down");
    expect(node).toBeTruthy();
    expect(node!.classList.contains("dim")).toBe(true);
  });

  it("says so when a fitted mains input has lost power", () => {
    render(<PowerFlow power={payload({ mains_present: false, mains_w: 0 })} />);
    expect(labels()).toContain("AC In down");
  });

  it("draws a stopped generator dim, and a running one bright", () => {
    render(
      <PowerFlow power={payload({ generator_running: false, generator_w: 0 })} />,
    );
    expect(nodeFor("Generator off")!.classList.contains("dim")).toBe(true);
    cleanup();
    render(
      <PowerFlow power={payload({ generator_running: true, generator_w: 1400 })} />,
    );
    expect(nodeFor("Generator")!.classList.contains("dim")).toBe(false);
  });
});

describe("the battery, which is the only reversible link", () => {
  /** Which way the battery's link runs: towards the node, or away from it.
   *  Read from the ends of the whole path, because it now turns a corner and
   *  the first two points are the run along the rail. */
  function batteryStubIsInbound() {
    const points = pathPoints(".pf-link.pf-battery");
    return points[points.length - 1][1] > points[0][1];
  }

  it("shows the state of charge, not the word charging", () => {
    // The animation already says which way it is going, and a word repeating
    // the picture competes with the two numbers only available here.
    render(<PowerFlow power={payload({ battery_w: 300, soc_pct: 82 })} />);
    expect(labels()).toContain("Battery");
    expect(labels()).not.toContain("Charging");
    expect(nodeFor("Battery")!.querySelector(".pf-value")?.textContent)
      .toBe("82%");
  });

  it("points the flow into the battery when it is charging", () => {
    render(<PowerFlow power={payload({ battery_w: 300 })} />);
    expect(batteryStubIsInbound()).toBe(true);
  });

  it("and out of it when it is discharging", () => {
    render(<PowerFlow power={payload({ battery_w: -300 })} />);
    expect(batteryStubIsInbound()).toBe(false);
  });

  it("says idle rather than showing a direction when it is neither", () => {
    render(<PowerFlow power={payload({ battery_w: 0 })} />);
    const node = nodeFor("Battery")!;
    expect(node.querySelector(".pf-sub")?.textContent).toBe("idle");
  });

  it("takes the direction from the station rather than deriving it", () => {
    // Sources exceed the load here, which would suggest charging to anything
    // doing the arithmetic itself — but the station says otherwise, and it is
    // the one that knows about conversion losses.
    render(
      <PowerFlow power={payload({ pv_w: 900, load_w: 100, battery_w: -50 })} />,
    );
    expect(batteryStubIsInbound()).toBe(false);
  });
});

describe("what animates", () => {
  it("leaves the stub of a source giving nothing completely still", () => {
    render(
      <PowerFlow
        power={payload({
          pv_w: 400, load_w: 120, battery_w: 280,
          mains_present: true, mains_w: 0,
          generator_running: false, generator_w: 0,
        })}
      />,
    );
    for (const tone of ["pf-mains", "pf-gen"]) {
      const stub = document.querySelector(`.pf-link.${tone}`);
      expect(stub!.querySelector(".pf-dash"), tone).toBeNull();
    }
    // And the ones that are carrying power do move.
    for (const tone of ["pf-solar", "pf-load"]) {
      expect(document.querySelector(`.pf-link.${tone} .pf-dash`), tone)
        .toBeTruthy();
    }
  });

  it("runs at one rate whatever the flow", () => {
    // Deliberately not proportional to watts any more. Dashes arriving at a
    // junction at one rate and leaving at another have to bunch or tear, so no
    // two connected links could stay in step and the flow appeared to stop at
    // every joint. How much is moving is on the nodes in watts.
    render(
      <PowerFlow
        power={payload({
          pv_w: 900, load_w: 20, battery_w: 880,
          mains_present: true, mains_w: 40,
        })}
      />,
    );
    const rates = Array.from(document.querySelectorAll(".pf-dash")).map(
      (el) => (el as HTMLElement).style.animationDuration,
    );
    expect(rates.length).toBeGreaterThan(2);
    expect(new Set(rates).size).toBe(1);
  });
});

describe("no telemetry yet", () => {
  it("holds its height rather than collapsing", () => {
    // The sidebar's fit measures once and re-runs only on resize, so a panel
    // that grows when the first frame lands leaves the stack overflowing.
    render(<PowerFlow power={null} />);
    expect(document.querySelector(".pf-empty")).toBeTruthy();
  });
});

describe("the bus, which is where the arithmetic lives", () => {
  /** Every path that travels along the rail: which way, and whether it moves.
   *
   *  Not just `.pf-bus`. The runs at either end are part of the load and
   *  battery paths so that the flow turns their corners in one animation, so
   *  asking the rail what it carries means asking those too. */
  /** Source stubs curve into the rail, so their last two points sit on it —
   *  but a stub is not rail traffic and must not be counted as a span. */
  const STUB = ["pf-solar", "pf-mains", "pf-gen"];

  function railRuns() {
    return Array.from(document.querySelectorAll(".pf-link")).flatMap((g) => {
      if (STUB.some((c) => g.classList.contains(c))) return [];
      const on = pointsOf(g).filter(([, y]) => Math.abs(y - BUS_Y) < 0.001);
      if (on.length < 2 || on[0][0] === on[on.length - 1][0]) return [];
      const from = on[0][0];
      const to = on[on.length - 1][0];
      return [{
        rightward: to > from,
        moving: Boolean(g.querySelector(".pf-dash")),
        at: Math.min(from, to),
      }];
    // Left to right along the rail. Document order puts the two elbows last,
    // because they are drawn after the sources they take power from.
    }).sort((a, b) => a.at - b.at);
  }

  it("turns the corner in a single path rather than two meeting at one", () => {
    // The fix this block exists for. A horizontal and a vertical meeting at a
    // right angle are two paths, so they are two animations: the flow stopped
    // dead at the corner and started again on the other side of it.
    render(
      <PowerFlow power={payload({ pv_w: 400, load_w: 120, battery_w: 280 })} />,
    );
    for (const tone of ["pf-load", "pf-battery"]) {
      const d = document.querySelector(`.pf-link.${tone} path`)!
        .getAttribute("d")!;
      expect(d, tone).toContain("Q");           // one rounded bend
      expect(d.match(/M/g)!.length, tone).toBe(1);  // and one path, not two
      const ys = pointsOf(document.querySelector(`.pf-link.${tone}`)!)
        .map(([, y]) => y);
      expect(Math.min(...ys), tone).toBe(BUS_Y);      // reaches the rail
      expect(Math.max(...ys), tone).toBeGreaterThan(BUS_Y); // and the node
    }
  });

  it("splits at every attachment rather than running end to end", () => {
    // One animation along the whole rail is what made a 0 W mains input look
    // like it was pouring power out. There is a run between each neighbouring
    // pair and each carries its own answer.
    render(
      <PowerFlow
        power={payload({
          pv_w: 400, load_w: 120, battery_w: 280,
          mains_present: true, mains_w: 0,
        })}
      />,
    );
    // load, solar, mains, battery -> three runs between four attachments.
    expect(railRuns().length).toBe(3);
  });

  it("sends solar left to the load and the surplus right to the battery", () => {
    // The arithmetic, in the one case that exercises both directions at once:
    // 400 W in, 120 W out to the left of it, 280 W to the right.
    render(
      <PowerFlow power={payload({ pv_w: 400, load_w: 120, battery_w: 280 })} />,
    );
    const runs = railRuns();
    expect(runs.length).toBe(2);
    expect(runs[0]).toMatchObject({ rightward: false, moving: true });
    expect(runs[1]).toMatchObject({ rightward: true, moving: true });
  });

  it("moves power from a source towards the load that needs it", () => {
    // Solar is to the right of the load, so the rail between them runs left.
    render(
      <PowerFlow power={payload({ pv_w: 120, load_w: 120, battery_w: 0 })} />,
    );
    const moving = railRuns().filter((s) => s.moving);
    expect(moving.length).toBeGreaterThan(0);
    expect(moving.every((s) => !s.rightward)).toBe(true);
  });

  it("runs the other way when the battery is the one supplying", () => {
    // Battery on the right discharging into the load on the left: still
    // leftward, and now it is the battery's own path carrying it.
    render(
      <PowerFlow
        power={payload({ pv_w: 0, load_w: 200, battery_w: -200 })}
      />,
    );
    expect(railRuns().filter((s) => s.moving).every((s) => !s.rightward))
      .toBe(true);
  });

  it("runs rightward when a source is charging the battery", () => {
    // Solar sits left of the battery here, so surplus travels right.
    render(
      <PowerFlow
        power={payload({
          pv_w: 900, load_w: 100, battery_w: 800,
          mains_present: true, mains_w: 0,
        })}
      />,
    );
    expect(railRuns().filter((s) => s.moving).some((s) => s.rightward))
      .toBe(true);
  });

  /** How far into the dash pattern a run starts, recovered from the negative
   *  animation delay that puts it there. In user units, same as the path. */
  function advanceOf(selector: string): number {
    const el = document.querySelector(`${selector} .pf-dash`) as HTMLElement;
    const delay = parseFloat(el.style.animationDelay);
    const seconds = parseFloat(el.style.animationDuration);
    return (-delay / seconds) * 16;
  }

  const wrap = (n: number) => ((n % 16) + 16) % 16;

  it("hands the dash pattern across a junction in step", () => {
    // The whole point. A run picks the pattern up exactly where its neighbour
    // put it down, so the dashes cross the joint without vanishing and
    // reappearing out of phase. Solar's stub is 22 long (36 down to the rail
    // at 58) and feeds the battery run, so the battery run must start 22
    // further through the pattern than the stub did.
    render(
      <PowerFlow power={payload({ pv_w: 400, load_w: 120, battery_w: 280 })} />,
    );
    expect(wrap(advanceOf(".pf-link.pf-solar") - 22))
      .toBeCloseTo(wrap(advanceOf(".pf-link.pf-battery")), 6);
  });

  it("keeps two rail runs continuous where they meet", () => {
    // Solar and mains both feeding right: the run between them ends where the
    // next begins, and the pattern must not restart there either.
    render(
      <PowerFlow
        power={payload({
          pv_w: 700, load_w: 100, battery_w: 800,
          mains_present: true, mains_w: 200,
        })}
      />,
    );
    const runs = Array.from(document.querySelectorAll(".pf-bus"));
    expect(runs.length).toBeGreaterThan(0);
    for (const g of runs) {
      const pts = pointsOf(g);
      const [x1] = pts[0];
      const [x2] = pts[pts.length - 1];
      const el = g.querySelector(".pf-dash") as HTMLElement | null;
      if (!el) continue;
      const advance = (-parseFloat(el.style.animationDelay)
        / parseFloat(el.style.animationDuration)) * 16;
      // Screen-space phase: a rightward run starts at -x, a leftward one at +x.
      const expected = wrap(x2 > x1 ? -x1 : x1);
      expect(wrap(advance)).toBeCloseTo(expected, 6);
    }
  });

  it("does not animate a run nothing crosses", () => {
    // A site running entirely off solar with a full battery: the rail beyond
    // the array carries nothing, however much the array is making.
    render(
      <PowerFlow power={payload({ pv_w: 300, load_w: 300, battery_w: 0 })} />,
    );
    expect(railRuns().some((s) => !s.moving)).toBe(true);
  });
});
