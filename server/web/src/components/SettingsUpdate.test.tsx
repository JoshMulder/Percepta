import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { Capability, HealthPayload } from "../types";
import { SettingsUpdate } from "./SettingsUpdate";

/**
 * The update pane's two jobs: show what the station is running (from telemetry,
 * never from the 202) and take a push (two-step, capability-gated).
 */

afterEach(cleanup);

const DIGEST = "sha256:" + "a".repeat(64);

function health(software?: HealthPayload["software"], agentVersion?: string): HealthPayload {
  return { kind: "health", agent_version: agentVersion, software };
}

function show(
  h: HealthPayload | null,
  caps: Capability[] = ["station.update"],
  stationId: string | null = "s1",
) {
  return render(
    <SettingsUpdate health={h} caps={caps} stationId={stationId} stationName="Bench" />,
  );
}

const pushButton = () => screen.getByRole("button", { name: /Push update/ }) as HTMLButtonElement;

describe("what it shows", () => {
  it("reads the running version and last result from health.software", () => {
    show(
      health({
        running_version: "v0.1.0",
        update_last_result: "rolled_back",
        update_last_version: "bad-a6",
        update_at: "2026-08-11T11:50:02Z",
      }),
    );
    expect(screen.getByText("v0.1.0")).toBeTruthy();
    expect(screen.getByText(/rolled back to the previous image/)).toBeTruthy();
    expect(screen.getByText(/bad-a6/)).toBeTruthy();
  });

  it("shows an in-flight desired version while an update is landing", () => {
    show(health({ running_version: "v0.1.0", desired_version: "v0.2.0" }));
    expect(screen.getByText("v0.2.0")).toBeTruthy();
  });

  it("says unknown when no telemetry has arrived", () => {
    show(null);
    expect(screen.getByText("unknown")).toBeTruthy();
  });
});

describe("the push control", () => {
  it("is hidden entirely without station.update", () => {
    show(health({ running_version: "v0.1.0" }), ["telemetry.view"]);
    // Status still renders; the trigger does not.
    expect(screen.getByText("v0.1.0")).toBeTruthy();
    expect(screen.queryByText("Push an update")).toBeNull();
    expect(screen.queryByRole("button", { name: /Push update/ })).toBeNull();
  });

  it("stays disabled until an image and a well-formed digest are present", () => {
    show(null);
    expect(pushButton().disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Image"), { target: { value: "reg/percepta-gsu" } });
    expect(pushButton().disabled).toBe(true); // no digest yet
    fireEvent.change(screen.getByLabelText("Digest"), { target: { value: "sha256:nope" } });
    expect(pushButton().disabled).toBe(true); // malformed digest
    fireEvent.change(screen.getByLabelText("Digest"), { target: { value: DIGEST } });
    expect(pushButton().disabled).toBe(false);
  });

  it("confirms, then sends exactly what was typed", async () => {
    const spy = vi.spyOn(api, "updateStation").mockResolvedValue({ accepted: true });
    show(null);
    fireEvent.change(screen.getByLabelText("Image"), { target: { value: "reg/percepta-gsu" } });
    fireEvent.change(screen.getByLabelText("Digest"), { target: { value: DIGEST } });
    fireEvent.change(screen.getByLabelText("Tag (optional)"), { target: { value: "v0.2.0" } });

    // One click arms; nothing is sent yet.
    fireEvent.click(pushButton());
    expect(spy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Confirm: push v0.2.0/ }));
    expect(spy).toHaveBeenCalledWith("s1", {
      image: "reg/percepta-gsu",
      digest: DIGEST,
      tag: "v0.2.0",
      force: undefined,
    });
    // After it lands, it says so and clears the digest so it cannot be re-fired.
    expect(await screen.findByText(/Requested/)).toBeTruthy();
    expect((screen.getByLabelText("Digest") as HTMLInputElement).value).toBe("");
  });

  it("re-arms if the target is edited after the confirm appears", () => {
    vi.spyOn(api, "updateStation").mockResolvedValue({ accepted: true });
    show(null);
    fireEvent.change(screen.getByLabelText("Image"), { target: { value: "reg/percepta-gsu" } });
    fireEvent.change(screen.getByLabelText("Digest"), { target: { value: DIGEST } });
    fireEvent.click(pushButton());
    expect(screen.getByRole("button", { name: /Confirm: push/ })).toBeTruthy();

    // Change the digest — the confirm must not still send the old target.
    fireEvent.change(screen.getByLabelText("Digest"), { target: { value: "sha256:" + "b".repeat(64) } });
    expect(screen.queryByRole("button", { name: /Confirm: push/ })).toBeNull();
    expect(pushButton()).toBeTruthy();
  });
});
