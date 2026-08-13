import { describe, expect, it } from "vitest";
import type { Map as MapLibreMap } from "maplibre-gl";
import { collapseMapCredit } from "./mapCredit";

/**
 * A stand-in for the map: the helper only ever asks for one event and one
 * container, and MapLibre itself cannot run under jsdom (no WebGL). What is
 * being pinned here is *our* half of the contract — that the class comes off,
 * and only after idle. That the control starts expanded at all is MapLibre's
 * behaviour and is verified against the real library in a browser harness.
 */
function fakeMap(container: HTMLElement) {
  let idle: (() => void) | null = null;
  const map = {
    once: (ev: string, fn: () => void) => {
      if (ev === "idle") idle = fn;
      return map;
    },
    getContainer: () => container,
  };
  return { map: map as unknown as MapLibreMap, goIdle: () => idle?.() };
}

/** The markup MapLibre 4.7 leaves behind once the credit has attribution. */
function compactShown(): HTMLElement {
  const container = document.createElement("div");
  container.innerHTML = `
    <div class="maplibregl-ctrl-bottom-right">
      <details class="maplibregl-ctrl maplibregl-ctrl-attrib maplibregl-compact maplibregl-compact-show" open>
        <summary class="maplibregl-ctrl-attrib-button"></summary>
        <div class="maplibregl-ctrl-attrib-inner">Tiles &copy; Esri</div>
      </details>
    </div>`;
  return container;
}

describe("collapseMapCredit", () => {
  it("collapses the credit once the map settles", () => {
    const container = compactShown();
    const { map, goIdle } = fakeMap(container);
    const attrib = container.querySelector(".maplibregl-ctrl-attrib")!;

    collapseMapCredit(map);
    // Nothing yet: the class may not even exist this early, which is the whole
    // reason this waits for idle rather than load.
    expect(attrib.classList.contains("maplibregl-compact-show")).toBe(true);

    goIdle();
    expect(attrib.classList.contains("maplibregl-compact-show")).toBe(false);
    // The credit is collapsed, NOT removed - it is a licence condition, and
    // `maplibregl-compact` is also what stops MapLibre re-expanding it later.
    expect(attrib.classList.contains("maplibregl-compact")).toBe(true);
    expect(container.textContent).toContain("Esri");
  });

  it("leaves a credit that is already collapsed alone", () => {
    const container = compactShown();
    const attrib = container.querySelector(".maplibregl-ctrl-attrib")!;
    attrib.classList.remove("maplibregl-compact-show");
    const { map, goIdle } = fakeMap(container);

    collapseMapCredit(map);
    goIdle();

    expect(attrib.classList.contains("maplibregl-compact")).toBe(true);
    expect(attrib.classList.contains("maplibregl-compact-show")).toBe(false);
  });
});
