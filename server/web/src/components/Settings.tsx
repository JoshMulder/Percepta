import { useEffect, useState } from "react";
import type { Capability, Me, RadioPayload } from "../types";
import { SettingsAccount } from "./SettingsAccount";
import { SettingsOrganization } from "./SettingsOrganization";
import { SettingsPlatform } from "./SettingsPlatform";
import { SettingsRadio } from "./SettingsRadio";
import { SettingsStation } from "./SettingsStation";

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

type Tab = "account" | "radio" | "station" | "organization" | "platform";

export function Settings({
  me,
  stationId,
  stationName,
  radio,
  capabilities,
  onClose,
  onProfileChanged,
  onStationsChanged,
  onSignOut,
}: {
  me: Me;
  stationId: string | null;
  stationName: string | null;
  /** Live radio telemetry for the station being watched. The Radio pane shows a
   *  signal meter, which only exists for the station currently subscribed. */
  radio: RadioPayload | null;
  capabilities: Capability[];
  onClose: () => void;
  onProfileChanged: (displayName: string) => void;
  onStationsChanged: () => void;
  onSignOut: () => void;
}) {
  // Tabs are hidden rather than disabled when they are not available. A
  // disabled tab advertises a capability the user does not have and invites
  // them to ask why; an absent one says nothing.
  const isAdmin = me.roles.includes("admin");
  // An admin can configure every station in the org, so the tab is offered even
  // when the console happens to be looking at one they have not been granted
  // explicitly. For everyone else it follows the station in front of them.
  const canConfigure = isAdmin || capabilities.includes("config.write");

  // Radio needs only radio.listen: the meter and the squelch belong to whoever
  // is listening, not to whoever administers the site. Gain and correction
  // inside it are gated on config.write separately.
  const canRadio = capabilities.includes("radio.listen") && stationId !== null;

  const tabs: { id: Tab; label: string }[] = [
    { id: "account", label: "My account" },
    ...(canRadio ? [{ id: "radio" as Tab, label: "Radio" }] : []),
    ...(canConfigure ? [{ id: "station" as Tab, label: "Stations" }] : []),
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

          <div className="settings-pane">
            {tab === "account" && (
              <SettingsAccount me={me} onProfileChanged={onProfileChanged} />
            )}
            {tab === "radio" && (
              <SettingsRadio
                radio={radio}
                caps={capabilities}
                stationId={stationId}
                stationName={stationName}
              />
            )}
            {tab === "station" && (
              <SettingsStation
                initialStationId={stationId}
                canCreate={isAdmin}
                onSaved={onStationsChanged}
              />
            )}
            {tab === "organization" && <SettingsOrganization me={me} />}
            {tab === "platform" && <SettingsPlatform />}
          </div>
        </div>
      </div>
    </div>
  );
}
