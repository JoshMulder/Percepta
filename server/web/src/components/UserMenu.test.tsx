import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The menu behind your own name in the header.
 *
 * What is worth pinning down here is the ordering and the guard rails, not the
 * markup: Sign out has to stay last and separated, because it is the one item
 * that ends the session and it now sits in the same list as routine navigation.
 * And the organisation you are already in must not be switchable — that call
 * revokes the session server-side and reloads the page, which is a rough way to
 * find out you clicked the row you were already on.
 */

const orgs = vi.fn();
const switchOrganization = vi.fn();

vi.mock("../api", () => ({
  api: {
    organizations: () => orgs(),
    switchOrganization: (id: string) => switchOrganization(id),
  },
}));

import { UserMenu } from "./UserMenu";
import type { Me } from "../types";

const me: Me = {
  user_id: "u1",
  email: "pilot@example.test",
  display_name: "Pilot",
  organization_id: "o1",
  organization_name: "Kennels Road",
  roles: ["operator"],
  demo_mode: false,
  is_platform_admin: false,
  is_guest: false,
};

function open(over: Partial<Parameters<typeof UserMenu>[0]> = {}) {
  const onSettings = vi.fn();
  const onSignOut = vi.fn();
  render(
    <UserMenu
      me={me}
      displayName="Pilot"
      onSettings={onSettings}
      onSignOut={onSignOut}
      {...over}
    />,
  );
  return { onSettings, onSignOut };
}

/** The organisation list lands a tick after the menu opens; flush it. */
async function click(el: Element) {
  await act(async () => {
    fireEvent.click(el);
    await Promise.resolve();
  });
}

function items(): string[] {
  return Array.from(document.querySelectorAll(".user-menu-item")).map((e) =>
    (e.textContent ?? "").trim(),
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("UserMenu", () => {
  it("stays shut until you ask for it", async () => {
    orgs.mockResolvedValue([]);
    open();

    expect(document.querySelector(".user-menu-panel")).toBeNull();
    await click(screen.getByRole("button", { name: /Pilot/ }));
    expect(document.querySelector(".user-menu-panel")).not.toBeNull();
  });

  it("puts sign out last, below settings", async () => {
    orgs.mockResolvedValue([
      { id: "o1", name: "Kennels Road", is_member: true },
      { id: "o2", name: "Second Site", is_member: true },
    ]);
    open();
    await click(screen.getByRole("button", { name: /Pilot/ }));
    await waitFor(() => expect(items().length).toBeGreaterThan(2));

    expect(items()).toEqual([
      "Kennels Road",
      "Second Site",
      "Settings",
      "Sign out",
    ]);
  });

  it("does not offer a switch to the organisation you are already in", async () => {
    orgs.mockResolvedValue([
      { id: "o1", name: "Kennels Road", is_member: true },
      { id: "o2", name: "Second Site", is_member: true },
    ]);
    open();
    await click(screen.getByRole("button", { name: /Pilot/ }));
    await waitFor(() => expect(items().length).toBe(4));

    await click(screen.getByRole("menuitem", { name: /Kennels Road/ }));
    expect(switchOrganization).not.toHaveBeenCalled();

    // The one you are in is marked rather than dropped, so the list still says
    // where you are.
    expect(
      document.querySelector(".user-menu-item.current")?.textContent,
    ).toContain("Kennels Road");
  });

  it("switches to another organisation", async () => {
    orgs.mockResolvedValue([
      { id: "o1", name: "Kennels Road", is_member: true },
      { id: "o2", name: "Second Site", is_member: true },
    ]);
    switchOrganization.mockResolvedValue(undefined);
    open();
    await click(screen.getByRole("button", { name: /Pilot/ }));
    await waitFor(() => expect(items().length).toBe(4));

    await click(screen.getByRole("menuitem", { name: /Second Site/ }));
    await waitFor(() => expect(switchOrganization).toHaveBeenCalledWith("o2"));
  });

  it("says which organisations are somebody else's", async () => {
    orgs.mockResolvedValue([
      { id: "o1", name: "Kennels Road", is_member: true },
      { id: "o2", name: "Another Tenant", is_member: false },
    ]);
    open();
    await click(screen.getByRole("button", { name: /Pilot/ }));
    await waitFor(() => expect(items().length).toBe(4));

    const notes = Array.from(document.querySelectorAll(".user-menu-note")).map(
      (e) => e.textContent,
    );
    expect(notes).toEqual(["platform"]);
  });

  it("lists no organisations when there is no choice to make", async () => {
    orgs.mockResolvedValue([{ id: "o1", name: "Kennels Road", is_member: true }]);
    open();
    await click(screen.getByRole("button", { name: /Pilot/ }));
    await waitFor(() => expect(items().length).toBe(2));

    expect(items()).toEqual(["Settings", "Sign out"]);
  });

  it("closes on Escape and on a click elsewhere", async () => {
    orgs.mockResolvedValue([]);
    open();
    const trigger = screen.getByRole("button", { name: /Pilot/ });

    await click(trigger);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(document.querySelector(".user-menu-panel")).toBeNull();

    await click(trigger);
    fireEvent.mouseDown(document.body);
    expect(document.querySelector(".user-menu-panel")).toBeNull();
  });

  it("hands settings and sign out back to the layout, and shuts", async () => {
    orgs.mockResolvedValue([]);
    const { onSettings, onSignOut } = open();
    const trigger = screen.getByRole("button", { name: /Pilot/ });

    await click(trigger);
    await click(screen.getByRole("menuitem", { name: "Settings" }));
    expect(onSettings).toHaveBeenCalledOnce();
    expect(document.querySelector(".user-menu-panel")).toBeNull();

    await click(trigger);
    await click(screen.getByRole("menuitem", { name: "Sign out" }));
    expect(onSignOut).toHaveBeenCalledOnce();
  });

  it("survives an organisation list that cannot be fetched", async () => {
    orgs.mockRejectedValue(new Error("offline"));
    open();
    await click(screen.getByRole("button", { name: /Pilot/ }));

    // Losing the list must not cost you the way out.
    expect(items()).toEqual(["Settings", "Sign out"]);
  });
});
