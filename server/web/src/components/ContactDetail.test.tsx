import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Aircraft } from "../types";
import { ContactDetail } from "./ContactDetail";

/**
 * The contact panel.
 *
 * Almost everything here is one property in different clothes: **a field the
 * aircraft did not send must read as not sent, never as zero and never as a
 * blank that could be either.** The receiver attaches a validity flag to each
 * of altitude, heading, velocity, vertical velocity, callsign and squawk, and
 * that flag is the only copy of the difference between "the value is zero" and
 * "there is no value". The station carries it to the wire; these tests are what
 * stop it being thrown away in the last fifty pixels by a stray `?? 0` or a
 * falsy check.
 *
 * That is also why so many cases below pass a real zero. A test suite that only
 * ever used non-zero values would pass just as happily against `speed || "not
 * reported"`, which is the exact bug.
 *
 * Run with the rest: see `src/emitters.test.ts` for the container command.
 */

afterEach(cleanup);

/** A contact with everything present, so each test can remove exactly the one
 *  thing it is about and nothing else varies. */
function contact(overrides: Partial<Aircraft> = {}): Aircraft {
  return {
    icao: "C827F1",
    callsign: "ANZ759M",
    latitude: -44.53,
    longitude: 171.56,
    altitude: 3500,
    track: 213,
    speed: 262,
    range_km: 34.39,
    bearing: 130.5,
    alert: false,
    altitude_type: "pressure",
    altitude_corrected_m: null,
    vertical_speed: 1.6,
    emitter_type: 2,
    squawk: 5235,
    seconds_since_contact: 1,
    on_ground: null,
    simulated: false,
    source: "adsb",
    ...overrides,
  };
}

function show(overrides: Partial<Aircraft> = {}, onClose = () => {}) {
  render(<ContactDetail contact={contact(overrides)} onClose={onClose} />);
}

/** Panel text with digit grouping removed.
 *
 * `toLocaleString` groups thousands, and which separator it picks depends on
 * the runtime's locale. Asserting "3,500" would make these tests pass or fail
 * on the container's ICU data rather than on the component, so the numbers are
 * checked without the separator — the grouping itself is not the behaviour
 * under test. */
function text(): string {
  const panel = document.querySelector(".contact-detail");
  return (panel?.textContent ?? "").replace(/[,  ]/g, "");
}

/** The labels of the rows in the list, which is a different question from "does
 *  this phrase appear anywhere in the panel". The stale banner reads "Last
 *  heard 47 s ago — this position is a memory", so a panel-wide string match
 *  cannot tell that banner from the row it is meant to have replaced. */
function rowLabels(): string[] {
  return Array.from(document.querySelectorAll(".contact-row .contact-k"))
    .map((el) => el.textContent ?? "");
}

describe("altitude, which is what the panel is for", () => {
  it("leads with it, in metres and feet", () => {
    show({ altitude: 3500 });
    // Both units, neither treated as the real one: this console is metric and
    // the reader is likely to think in feet.
    expect(text()).toContain("3500 m");
    expect(text()).toContain("11483 ft");
  });

  it("says which datum a pressure altitude is", () => {
    // 1013.25 hPa is not the local datum, so this is not height above the sea
    // today — and the number looks equally exact either way.
    show({ altitude_type: "pressure" });
    expect(text()).toContain("1013.25 hPa");
  });

  it("distinguishes a geometric altitude from a pressure one", () => {
    show({ altitude_type: "geometric" });
    expect(text()).toContain("GNSS");
    expect(text()).not.toContain("1013.25");
  });

  it("claims no datum when the receiver did not say", () => {
    show({ altitude_type: null });
    expect(text()).not.toContain("1013.25");
    expect(text()).not.toContain("GNSS");
  });

  it("says so rather than showing nothing when there is no altitude", () => {
    show({ altitude: null });
    expect(text()).toMatch(/Altitude not reported/i);
    expect(text()).not.toContain("NaN");
    // And emphatically not a zero, which would read as ground level.
    expect(text()).not.toMatch(/\b0 m\b/);
  });

  it("renders a genuine zero as an altitude, not as missing", () => {
    // An aircraft at the pressure datum. Rare, real, and exactly the case a
    // falsy check gets wrong.
    show({ altitude: 0 });
    expect(text()).toContain("0 m");
    expect(text()).not.toMatch(/Altitude not reported/i);
  });
});

describe("the corrected altitude", () => {
  it("appears beside the reported one, never instead of it", () => {
    // What the receiver said and what it means against this station's
    // barometer are two facts. A panel showing only the second cannot show its
    // working, and the second is the one derived from another sensor.
    show({ altitude: 3500, altitude_corrected_m: 3472 });
    expect(text()).toContain("3500 m");
    expect(text()).toContain("3472 m");
    expect(text()).toMatch(/barometer/i);
  });

  it("is absent entirely when the correction is off", () => {
    // Not an empty row: an empty row invites the reading that it was tried and
    // failed, when in fact it was never switched on.
    show({ altitude_corrected_m: null });
    expect(rowLabels()).not.toContain("Corrected");
    expect(text()).not.toMatch(/barometer/i);
  });
});

describe("fields the aircraft did not send", () => {
  it("reads as not reported, one wording for all of them", () => {
    show({
      speed: null, track: null, squawk: null, vertical_speed: null,
    });
    // Four absent fields, four identical statements — the component has a
    // single place that turns a null into words, and this is what keeps it
    // that way.
    expect(text().match(/not reported/g)?.length).toBe(4);
  });

  it("never turns an absent field into a zero", () => {
    show({ speed: null, track: null, squawk: null, vertical_speed: null });
    const panel = text();
    expect(panel).not.toMatch(/\b0 kt\b/);
    expect(panel).not.toContain("000°");
    expect(panel).not.toContain("0000");
    expect(panel).not.toContain("NaN");
  });
});

describe("zeros that mean something", () => {
  it("shows squawk 0000 as a code", () => {
    // 0000 is a real Mode A code. Rendering it as "0" makes it look like
    // missing data, and treating it as falsy makes it disappear.
    show({ squawk: 0 });
    expect(text()).toContain("0000");
    expect(text()).not.toMatch(/Squawk\s*not reported/i);
  });

  it("keeps a squawk's leading zeros", () => {
    show({ squawk: 21 });
    expect(text()).toContain("0021");
  });

  it("shows a stopped aircraft as 0 kt", () => {
    show({ speed: 0 });
    expect(text()).toContain("0 kt");
  });

  it("shows a due-north track as 000, not as missing", () => {
    show({ track: 0 });
    expect(text()).toContain("000°");
  });

  it("calls a zero vertical rate level rather than absent", () => {
    // Level flight is a fact the receiver reported. "not reported" would be a
    // different, wrong statement.
    show({ vertical_speed: 0 });
    expect(text()).toMatch(/level/i);
    expect(text()).not.toMatch(/Vertical\s*not reported/i);
  });
});

describe("vertical rate", () => {
  it("shows a climb in feet per minute", () => {
    show({ vertical_speed: 1.6 });
    expect(text()).toContain("315 ft/min");
    expect(text()).toContain("▲");
  });

  it("shows a descent as a descent, not a negative climb", () => {
    show({ vertical_speed: -3.5 });
    expect(text()).toContain("▼");
    // The arrow carries the sign, so the number must not also be negative.
    expect(text()).toContain("689 ft/min");
    expect(text()).not.toContain("-689");
  });
});

describe("flags that change how the rest should be read", () => {
  it("says when a contact is a memory rather than an aircraft", () => {
    // tslc past 30 s: the position on the map is where it was, not where it is.
    show({ seconds_since_contact: 47 });
    expect(text()).toMatch(/memory/i);
    expect(text()).toContain("47");
  });

  it("does not also print a Last heard row once it has said that", () => {
    show({ seconds_since_contact: 47 });
    expect(rowLabels()).not.toContain("Last heard");
  });

  it("prints Last heard while the contact is current", () => {
    show({ seconds_since_contact: 3 });
    expect(rowLabels()).toContain("Last heard");
    expect(text()).not.toMatch(/memory/i);
  });

  it("marks an injected target so it cannot be read as traffic", () => {
    show({ simulated: true });
    expect(text()).toMatch(/test target/i);
  });

  it("does not mark real traffic", () => {
    show({ simulated: false });
    expect(text()).not.toMatch(/test target/i);
  });

  it("suppresses the proximity flag on a stale contact", () => {
    // "Close and low" about a position from a minute ago is a claim the data
    // no longer supports; the staleness is the more important thing to say.
    show({ alert: true, seconds_since_contact: 45 });
    expect(text()).toMatch(/memory/i);
    expect(text()).not.toMatch(/Close and low/i);
  });

  it("shows the proximity flag on a current one", () => {
    show({ alert: true, seconds_since_contact: 2 });
    expect(text()).toMatch(/Close and low/i);
  });
});

describe("rows that only exist when there is something to say", () => {
  it("omits the ground state when the receiver cannot know it", () => {
    // ADSB_VEHICLE has no airborne/surface bit, so this is null for every
    // airborne category and always will be (CONTRACT-QUESTIONS 19). A row
    // reading "not reported" on every single contact is noise.
    show({ on_ground: null });
    expect(rowLabels()).not.toContain("State");
  });

  it("shows it for the surface categories, which can answer", () => {
    show({ on_ground: true, emitter_type: 17 });
    expect(text()).toMatch(/on the ground/i);
  });

  it("distinguishes airborne from unknown", () => {
    show({ on_ground: false });
    expect(text()).toMatch(/airborne/i);
  });
});

describe("identity", () => {
  it("leads with the callsign and keeps the address", () => {
    show({ callsign: "ANZ759M", icao: "C827F1" });
    expect(screen.getByRole("heading").textContent).toBe("ANZ759M");
    expect(text()).toContain("C827F1");
  });

  it("falls back to the address when there is no callsign", () => {
    show({ callsign: null });
    expect(screen.getByRole("heading").textContent).toBe("C827F1");
  });

  it("treats a blank callsign as no callsign", () => {
    // Real receivers pad the field; an all-spaces callsign is how "no
    // identifier" arrives, and a heading of "   " is worse than the address.
    show({ callsign: "   " });
    expect(screen.getByRole("heading").textContent).toBe("C827F1");
  });

  it("labels the dialog by whichever name it showed", () => {
    show({ callsign: null });
    expect(screen.getByRole("dialog").getAttribute("aria-label"))
      .toContain("C827F1");
  });
});

describe("closing", () => {
  it("calls back when the close control is used", () => {
    const onClose = vi.fn();
    show({}, onClose);
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
