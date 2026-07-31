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
    expect(labels().some((l) => l.startsWith("Mains"))).toBe(false);
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
    const node = nodeFor("Mains down");
    expect(node).toBeTruthy();
    expect(node!.classList.contains("dim")).toBe(true);
  });

  it("says so when a fitted mains input has lost power", () => {
    render(<PowerFlow power={payload({ mains_present: false, mains_w: 0 })} />);
    expect(labels()).toContain("Mains down");
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

  it("runs faster for a bigger flow", () => {
    render(<PowerFlow power={payload({ pv_w: 100 })} />);
    const slow = (document.querySelector(".pf-solar .pf-dash") as HTMLElement)
      .style.animationDuration;
    cleanup();
    render(<PowerFlow power={payload({ pv_w: 900 })} />);
    const fast = (document.querySelector(".pf-solar .pf-dash") as HTMLElement)
      .style.animationDuration;
    expect(parseFloat(fast)).toBeLessThan(parseFloat(slow));
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

  it("bends a source into the side taking most of what it makes", () => {
    // Solar sends 120 W left to the load and 280 W right to the battery, so it
    // leans right; the leftward run keeps its full length to the tap, which is
    // where the split actually happens.
    render(
      <PowerFlow power={payload({ pv_w: 400, load_w: 120, battery_w: 280 })} />,
    );
    const stub = pathPoints(".pf-link.pf-solar");
    const [nodeX] = stub[0];
    expect(stub[stub.length - 1][0]).toBeGreaterThan(nodeX);
    // And the run it bends into starts short of the tap, so the curve and the
    // rail do not lie on top of each other.
    const bendEnd = stub[stub.length - 1][0];
    const rail = pathPoints(".pf-link.pf-battery");
    expect(Math.min(...rail.map(([x]) => x))).toBeCloseTo(bendEnd, 5);
  });

  it("leans the other way when the load is taking most of it", () => {
    render(
      <PowerFlow power={payload({ pv_w: 400, load_w: 380, battery_w: 20 })} />,
    );
    const stub = pathPoints(".pf-link.pf-solar");
    expect(stub[stub.length - 1][0]).toBeLessThan(stub[0][0]);
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
