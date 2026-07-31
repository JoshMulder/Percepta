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
  it("says charging when the station reports power going in", () => {
    render(<PowerFlow power={payload({ battery_w: 300 })} />);
    expect(labels()).toContain("Charging");
  });

  it("does not claim charging when it is discharging", () => {
    render(<PowerFlow power={payload({ battery_w: -300 })} />);
    expect(labels()).not.toContain("Charging");
    expect(labels()).toContain("Battery");
  });

  it("reads idle when it is neither, rather than showing a direction", () => {
    render(<PowerFlow power={payload({ battery_w: 0 })} />);
    const node = nodeFor("Battery")!;
    expect(node.querySelector(".pf-value")?.textContent).toBe("idle");
    expect(node.classList.contains("dim")).toBe(true);
  });

  it("takes the direction from the station rather than deriving it", () => {
    // Sources exceed the load here, which would suggest charging to anything
    // doing the arithmetic itself — but the station says otherwise, and the
    // station is the one that knows about conversion losses.
    render(
      <PowerFlow power={payload({ pv_w: 900, load_w: 100, battery_w: -50 })} />,
    );
    expect(labels()).not.toContain("Charging");
  });
});

describe("what animates", () => {
  it("animates only the links actually carrying power", () => {
    render(
      <PowerFlow
        power={payload({
          pv_w: 400, load_w: 120, battery_w: 280,
          mains_present: true, mains_w: 0,
          generator_running: false, generator_w: 0,
        })}
      />,
    );
    // Solar, battery and load move; mains and generator do not.
    expect(document.querySelectorAll(".pf-dash").length).toBe(3);
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
