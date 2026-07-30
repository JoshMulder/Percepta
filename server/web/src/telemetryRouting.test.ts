import { describe, expect, it } from "vitest";
import { isForSelectedStation } from "./telemetryRouting";

/**
 * The station-scoping check.
 *
 * Written against a fault seen on real hardware: a Raspberry Pi with no weather
 * head displaying wind, temperature and pressure. The station was publishing no
 * weather stream at all — it said so in its own health frame — and the console
 * clears every reading on a station switch, so neither end was obviously wrong.
 *
 * What was wrong was the gap between them. `select_station` scopes the socket
 * server-side, but not instantaneously: a frame for the station being left was
 * already in flight, arrived just after the switch had emptied the panels, and
 * populated them. For a stream the new station also publishes that is invisible
 * — the next frame corrects it within a second. For one it does not publish,
 * nothing ever corrects it, and a demo station's weather sat under a real
 * station's name for the rest of the session.
 */

describe("admitting a frame", () => {
  it("accepts one addressed to the selected station", () => {
    expect(isForSelectedStation({ station_id: "a" }, "a")).toBe(true);
  });

  it("rejects one addressed to any other station", () => {
    // The whole fault in one line.
    expect(isForSelectedStation({ station_id: "b" }, "a")).toBe(false);
  });

  it("rejects everything before a station has been chosen", () => {
    // There is no panel for it to belong to yet.
    expect(isForSelectedStation({ station_id: "a" }, null)).toBe(false);
  });

  it("does not match loosely", () => {
    // Station ids are uuids and compared whole. A prefix match would let a
    // truncated or malformed id through, which is the same bug with extra
    // steps.
    expect(isForSelectedStation({ station_id: "a" }, "ab")).toBe(false);
    expect(isForSelectedStation({ station_id: "ab" }, "a")).toBe(false);
  });
});

describe("messages that are about the connection, not a station", () => {
  it("lets them through", () => {
    // `hello`, `station_selected` and `station_revoked` carry no station_id.
    // Dropping them would break selection itself — including, absurdly, the
    // reply that tells the console its selection succeeded.
    expect(isForSelectedStation({}, "a")).toBe(true);
    expect(isForSelectedStation({}, null)).toBe(true);
  });

  it("lets them through before anything is selected, which is when they arrive", () => {
    // `hello` is the first message on the socket and necessarily precedes any
    // selection. A check that required a selection would deadlock the console.
    expect(isForSelectedStation({}, null)).toBe(true);
  });
});
