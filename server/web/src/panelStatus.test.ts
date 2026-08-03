import { describe, expect, it } from "vitest";
import { panelStatus } from "./components/PanelState";

/**
 * When a panel shows skeletons, a red X, or neither.
 *
 * The case that prompted the fourth state: a station with no floodlight
 * publishes no light stream at all, so the console's only evidence was an
 * absence. An absence took twelve seconds to become suggestive and then
 * resolved to a fault — wrong twice over. It wasted the wait, and it called a
 * complete station broken.
 */

const NOW = Date.now();
const STALE = 15_000;

describe("an empty slot", () => {
  it("is obvious immediately, with no grace period", () => {
    // The station states this in every health frame; there is nothing to wait
    // for and no timer involved.
    expect(panelStatus(null, NOW, STALE, false)).toBe("not-fitted");
  });

  it("stays not-fitted however long the link has been up", () => {
    // The old path would have called this a fault after twelve seconds.
    const longAgo = NOW - 10 * 60 * 1000;
    expect(panelStatus(null, longAgo, STALE, false)).toBe("not-fitted");
  });

  it("is not a fault", () => {
    expect(panelStatus(null, NOW - 60_000, STALE, false)).not.toBe("fault");
  });
});

describe("before the station has said anything", () => {
  it("keeps loading rather than guessing at not-fitted", () => {
    // `undefined` means no health frame yet. Guessing here would flash "Not
    // fitted" across every panel on connect and then correct itself.
    expect(panelStatus(null, NOW, STALE, undefined)).toBe("loading");
  });

  it("still becomes a fault once the grace period expires", () => {
    const longAgo = NOW - 10 * 60 * 1000;
    expect(panelStatus(null, longAgo, STALE, undefined)).toBe("fault");
  });
});

describe("a station the platform already knows is offline", () => {
  /**
   * The grace period is for a station that is talking and whose first frame is
   * merely late. It is not for one nobody has heard from: `online` is computed
   * from `last_seen_at` and arrives with the station list, before any
   * telemetry, so waiting it out meant a console opened on a station that went
   * offline hours ago spent twelve seconds running skeleton loaders — implying
   * a connection was being established when there was nothing to wait for.
   */
  it("faults at once instead of running the loaders", () => {
    expect(panelStatus(null, NOW, STALE, true, false)).toBe("fault");
  });

  it("does so even before the station has described its slots", () => {
    expect(panelStatus(null, NOW, STALE, undefined, false)).toBe("fault");
  });

  it("still says not-fitted for a slot that is genuinely empty", () => {
    // Offline does not make an unfitted slot a fault. Nothing is wrong with a
    // station that has no floodlight, whether or not it is talking.
    expect(panelStatus(null, NOW, STALE, false, false)).toBe("not-fitted");
  });

  it("goes live the moment a frame actually arrives", () => {
    // `online` is refreshed on a timer and telemetry is not. A frame in hand
    // outranks a minute-old summary saying there would not be one.
    expect(panelStatus(NOW, NOW, STALE, true, false)).toBe("live");
  });

  it("keeps loading while the station list has not arrived", () => {
    // `undefined` is not knowing, and must not read as offline — otherwise
    // every panel flashes red on connect and then corrects itself.
    expect(panelStatus(null, NOW, STALE, true, undefined)).toBe("loading");
  });
});

describe("a fitted sensor", () => {
  it("loads, then goes live", () => {
    expect(panelStatus(null, NOW, STALE, true)).toBe("loading");
    expect(panelStatus(NOW, NOW, STALE, true)).toBe("live");
  });

  it("faults when it stops reporting", () => {
    expect(panelStatus(NOW - 60_000, NOW, STALE, true)).toBe("fault");
  });
});

describe("data outranks the slot report", () => {
  // This is the responsiveness path, not a corner case. `fitted` comes from the
  // health frame, which the station only re-emits every 30 seconds and only
  // updates after its own rediscovery notices a plugged-in sensor — while the
  // telemetry stream for that sensor starts on the next tick. `statusOf` used
  // to short-circuit to "not-fitted" on a `fitted === false` health verdict
  // before ever consulting the reading, so a sensor coming online sat red for
  // up to a minute and people reloaded to clear it. Deferring to these two
  // lines is what makes it flip the instant a reading arrives.
  it("shows a reading that is arriving even while health still says empty", () => {
    expect(panelStatus(NOW, NOW, STALE, false)).toBe("live");
  });

  it("stays not-fitted while health says empty and nothing is arriving", () => {
    expect(panelStatus(null, NOW, STALE, false)).toBe("not-fitted");
  });
});

describe("synthetic streams", () => {
  /**
   * These used to be exempt from staleness, reasoning that the simulator is
   * the sensor so a red X could only mean the simulator stopped. That held
   * when demo meant the platform's own in-process simulator. It does not now:
   * `simulated` is a per-stream flag a real station stamps on its telemetry,
   * so the reading is synthetic but the box, the link and the broker carrying
   * it are not.
   *
   * The exemption was found by revoking a station's credential. It stopped
   * publishing, and the console went on showing green panels full of readings
   * from before it was cut off — which is the single failure this file exists
   * to prevent, arriving through the one door left open for it.
   *
   * There is no `demo` argument to pass any more. That is the fix: whether a
   * reading is synthetic says nothing about whether it is still arriving.
   */
  it("go stale like any other, because the link carrying them is real", () => {
    expect(panelStatus(NOW - 60_000, NOW, STALE, true)).toBe("fault");
  });

  it("are live while they are actually arriving", () => {
    expect(panelStatus(NOW, NOW, STALE, true)).toBe("live");
  });

  it("still get the grace period before the first frame", () => {
    // A synthetic sensor is no quicker to start than a real one, and crying
    // wolf during start-up trains operators to ignore the X.
    expect(panelStatus(null, NOW, STALE, true)).toBe("loading");
  });
});
