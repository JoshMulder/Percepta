import { useEffect, useState } from "react";
import type { Me } from "../types";
import { SettingsAccount } from "./SettingsAccount";
import { SettingsDisplay } from "./SettingsDisplay";
import { SettingsOrganization } from "./SettingsOrganization";
import { SettingsPlatform } from "./SettingsPlatform";

/**
 * The settings overlay.
 *
 * Deliberately rendered outside the fit-scaled console stack. `useFitScale`
 * sizes the whole console by measuring the sidebar's natural height and setting
 * the root font size to make it fill the viewport — so anything that
 * participates in that stack changes the scale of everything else by existing.
 * This is `position: fixed` and sits outside it.
 *
 * Its type is sized in px rather than rem for the same reason, and it is the one
 * place in the app that does. The console is a dense glanceable display that
 * must fit exactly, and on a small viewport its root can end up at 11px; a
 * settings dialog is a reading-and-editing surface where that would be
 * unpleasant to use. Different job, different rules.
 */

type Tab = "account" | "display" | "organization" | "platform";

export function Settings({
  me,
  stationId,
  onClose,
  onProfileChanged,
  onStationsChanged,
  onSignOut,
}: {
  me: Me;
  /** Only used as the initial selection in Organisation > Stations. */
  stationId: string | null;
  onClose: () => void;
  onProfileChanged: (displayName: string) => void;
  onStationsChanged: () => void;
  onSignOut: () => void;
}) {
  // Tabs are hidden rather than disabled when they are not available. A
  // disabled tab advertises a capability the user does not have and invites
  // them to ask why; an absent one says nothing.
  const isAdmin = me.roles.includes("admin");

  const tabs: { id: Tab; label: string }[] = [
    { id: "account", label: "My account" },
    // Available to everyone: these are display choices for the person looking,
    // gated by no capability.
    { id: "display", label: "Map" },
    ...(isAdmin ? [{ id: "organization" as Tab, label: "Organisation" }] : []),
    // Only while the active org IS the platform org. A platform admin working
    // inside a customer's organisation does not get this.
    ...(me.is_platform_admin ? [{ id: "platform" as Tab, label: "Platform" }] : []),
  ];

  const [tab, setTab] = useState<Tab>("account");

  // Escape closes, as it does for the alerts drawer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="settings-scrim" onClick={onClose}>
      <div
        className="settings"
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="settings-head">
          <h2>Settings</h2>
          <button
            type="button"
            className="settings-close"
            onClick={onClose}
            aria-label="Close settings"
          >
            ×
          </button>
        </header>

        <div className="settings-body">
          <nav className="settings-tabs" aria-label="Settings sections">
            {tabs.map((t) => (
              <button
                key={t.id}
                type="button"
                className={`settings-tab${tab === t.id ? " active" : ""}`}
                onClick={() => setTab(t.id)}
                aria-current={tab === t.id}
              >
                {t.label}
              </button>
            ))}
            {/* Not a tab - it selects nothing and leaves instead. It sits with
                them because this is where a user now looks for anything about
                their own session, and it is pushed to the bottom and coloured
                so it is never the thing clicked on the way to something else. */}
            <button
              type="button"
              className="settings-tab sign-out"
              onClick={onSignOut}
            >
              Sign out
            </button>
          </nav>

          {/* The Organisation pane fills the dialog instead of scrolling as a
              whole, so its rosters run full height with the add button pinned.
              Every other pane keeps the normal scrolling behaviour. */}
          <div className={`settings-pane${tab === "organization" ? " pane-fill" : ""}`}>
            {tab === "account" && (
              <SettingsAccount me={me} onProfileChanged={onProfileChanged} />
            )}
            {tab === "display" && <SettingsDisplay />}
            {tab === "organization" && (
              <SettingsOrganization
                me={me}
                stationId={stationId}
                onStationsChanged={onStationsChanged}
              />
            )}
            {tab === "platform" && <SettingsPlatform />}
          </div>
        </div>
      </div>
    </div>
  );
}
