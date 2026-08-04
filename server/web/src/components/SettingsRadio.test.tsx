import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { RadioPayload } from "../types";
import { SettingsRadio } from "./SettingsRadio";

/**
 * The two squelch controls, and the one rule they share: **the control has to
 * answer the operator now, and the station has to win in the end.**
 *
 * Both of these were driven straight from telemetry, and both of the ways that
 * failed were reported as "auto squelch isn't working properly":
 *
 *   The AUTO button's label came only from the station, so a click did nothing
 *   visible for up to a second. Clicking again — which is what anybody does
 *   when a button appears dead — undid the first click, and AUTO ended up
 *   exactly where it started.
 *
 *   The threshold slider's value was `threshold_db`, which is the number AUTO
 *   moves: it rides the noise floor and changes on every frame. The handle was
 *   pulled out from under the pointer once a second.
 */

afterEach(cleanup);

function payload(overrides: Partial<RadioPayload> = {}): RadioPayload {
  return {
    kind: "radio",
    freq_hz: 118_700_000,
    rssi_db: -77,
    noise_floor_db: -85,
    threshold_db: -77,
    squelch_open: false,
    auto_squelch: true,
    monitor: false,
    gain: 37.2,
    gains: [0, 37.2],
    ppm: 0,
    tx_capable: false,
    ...overrides,
  };
}

function show(radio: RadioPayload) {
  return render(
    <SettingsRadio
      radio={radio}
      caps={["radio.listen", "radio.control"]}
      stationId="s1"
      stationName="Bench"
    />,
  );
}

const autoButton = () => screen.getByRole("button", { name: /Auto squelch/ });
const slider = () => screen.getByLabelText("Squelch threshold") as HTMLInputElement;

function showConfigurable(radio: RadioPayload) {
  return render(
    <SettingsRadio
      radio={radio}
      caps={["radio.listen", "radio.control", "config.write"]}
      stationId="s1"
      stationName="Bench"
    />,
  );
}

describe("RF gain", () => {
  it("offers Managed and shows the step it has settled on", () => {
    showConfigurable(payload({ gain: "managed", managed_gain_db: 37.2 }));
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("managed");
    expect(screen.getByText(/Currently/).textContent).toContain("37.2 dB");
  });

  it("sends managed when it is chosen", () => {
    const setGain = vi.spyOn(api, "setGain").mockResolvedValue({ accepted: true });
    showConfigurable(payload({ gain: 37.2 }));
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "managed" },
    });
    expect(setGain).toHaveBeenCalledWith("s1", "managed");
  });
});

describe("the AUTO button", () => {
  it("says what the operator just did, without waiting for the station", () => {
    vi.spyOn(api, "autoSquelch").mockResolvedValue({ accepted: true });
    show(payload({ auto_squelch: true }));

    expect(autoButton().textContent).toContain("on");
    fireEvent.click(autoButton());

    // No telemetry has arrived yet. Before this, the label sat on "on" for up
    // to a second, and a second click on an apparently dead button turned it
    // straight back on.
    expect(autoButton().textContent).toContain("off");
    expect(api.autoSquelch).toHaveBeenCalledWith("s1", false);
  });

  it("holds the click through the frames before the command lands", () => {
    vi.spyOn(api, "autoSquelch").mockResolvedValue({ accepted: true });
    const view = show(payload({ auto_squelch: true }));
    fireEvent.click(autoButton());

    // A command takes a moment to reach the station, so the next frame or two
    // still say AUTO is on. Believing them would flick the label back and
    // forth, which is the flicker the optimistic value exists to prevent.
    view.rerender(
      <SettingsRadio
        radio={payload({ auto_squelch: true })}
        caps={["radio.listen", "radio.control"]}
        stationId="s1"
        stationName="Bench"
      />,
    );
    expect(autoButton().textContent).toContain("off");

    // And once the station agrees, the label is telemetry's again.
    view.rerender(
      <SettingsRadio
        radio={payload({ auto_squelch: false })}
        caps={["radio.listen", "radio.control"]}
        stationId="s1"
        stationName="Bench"
      />,
    );
    expect(autoButton().textContent).toContain("off");
  });

  it("gives up on a command that never lands, and shows the station again", () => {
    vi.useFakeTimers();
    try {
      vi.spyOn(api, "autoSquelch").mockResolvedValue({ accepted: true });
      show(payload({ auto_squelch: true }));
      fireEvent.click(autoButton());
      expect(autoButton().textContent).toContain("off");

      // Nothing ever confirmed it. The console must not go on showing a state
      // the station is not in — a wrong reading that never corrects itself is
      // worse than a slow one.
      act(() => {
        vi.advanceTimersByTime(5_000);
      });
      expect(autoButton().textContent).toContain("on");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("the threshold slider", () => {
  it("stays where it was put while AUTO moves the reported threshold", () => {
    vi.spyOn(api, "squelch").mockResolvedValue({ accepted: true });
    const view = show(payload({ threshold_db: -77 }));

    fireEvent.change(slider(), { target: { value: "-60" } });
    expect(slider().value).toBe("-60");

    // AUTO rides the noise floor, so the next frame carries a different
    // threshold. That used to yank the handle back mid-drag.
    view.rerender(
      <SettingsRadio
        radio={payload({ threshold_db: -78 })}
        caps={["radio.listen", "radio.control"]}
        stationId="s1"
        stationName="Bench"
      />,
    );
    expect(slider().value).toBe("-60");
  });

  it("sends one command for a drag, not one per pointer move", () => {
    const squelch = vi.spyOn(api, "squelch").mockResolvedValue({ accepted: true });
    show(payload());

    for (const value of ["-70", "-65", "-62", "-60"]) {
      fireEvent.change(slider(), { target: { value } });
    }
    expect(squelch).not.toHaveBeenCalled();   // nothing on the wire mid-drag

    fireEvent.pointerUp(slider());
    expect(squelch).toHaveBeenCalledTimes(1);
    expect(squelch).toHaveBeenCalledWith("s1", -60);

    // A blur follows a pointer release. One adjustment is one command and one
    // audit row, however many gestures end it.
    fireEvent.blur(slider());
    expect(squelch).toHaveBeenCalledTimes(1);
  });

  it("shows AUTO dropping out, because setting a threshold by hand leaves it", () => {
    vi.spyOn(api, "squelch").mockResolvedValue({ accepted: true });
    show(payload({ auto_squelch: true }));

    fireEvent.change(slider(), { target: { value: "-60" } });
    fireEvent.pointerUp(slider());

    // The station does this on `radio.squelch`; saying so here means the button
    // does not claim AUTO is still on for the second before telemetry lands.
    expect(autoButton().textContent).toContain("off");
  });

  it("takes a later drag back to the same value", () => {
    const squelch = vi.spyOn(api, "squelch").mockResolvedValue({ accepted: true });
    const view = show(payload({ threshold_db: -77 }));

    fireEvent.change(slider(), { target: { value: "-60" } });
    fireEvent.pointerUp(slider());
    expect(squelch).toHaveBeenCalledTimes(1);

    // The station confirms, which drops the optimistic value.
    view.rerender(
      <SettingsRadio
        radio={payload({ threshold_db: -60, auto_squelch: false })}
        caps={["radio.listen", "radio.control"]}
        stationId="s1"
        stationName="Bench"
      />,
    );
    // Moved away and back. The de-duplication must not swallow this.
    fireEvent.change(slider(), { target: { value: "-70" } });
    fireEvent.pointerUp(slider());
    fireEvent.change(slider(), { target: { value: "-60" } });
    fireEvent.pointerUp(slider());
    expect(squelch).toHaveBeenCalledTimes(3);
  });
});
