import { useEffect, useState } from "react";
import { api } from "../api";
import type { Me, OrganizationOption } from "../types";

/**
 * Switches which organisation this login is working in.
 *
 * Renders nothing unless there is more than one to choose from, which is the
 * common case — most people belong to one organisation and should not be shown
 * a control that cannot do anything.
 *
 * Switching **reloads the page** rather than re-fetching in place. That is a
 * deliberate choice, not laziness. The organisation reaches into almost
 * everything the console holds: the station list, the selected station, every
 * telemetry buffer, the map configuration, the WebSocket's authorised groups
 * and the audio pipeline. The server revokes the old session as part of the
 * switch, so the socket is closed underneath us anyway. Re-bootstrapping from
 * scratch is the only version of this with no possibility of one tenant's data
 * surviving into another's view, and that is worth more than a smooth
 * transition.
 */
export function OrgSwitcher({ me }: { me: Me }) {
  const [options, setOptions] = useState<OrganizationOption[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .organizations()
      .then(setOptions)
      .catch(() => setOptions([]));
  }, [me.organization_id]);

  if (options.length < 2) return null;

  const current = options.find((o) => o.id === me.organization_id);

  async function switchTo(id: string) {
    if (id === me.organization_id || busy) return;
    setBusy(true);
    try {
      await api.switchOrganization(id);
      window.location.reload();
    } catch {
      setBusy(false);
    }
  }

  return (
    <div className="org-switch">
      <select
        value={me.organization_id}
        onChange={(e) => void switchTo(e.target.value)}
        disabled={busy}
        aria-label="Organisation"
        title={
          current && !current.is_member
            ? "You are working inside this organisation as a platform administrator"
            : "Organisation"
        }
      >
        {options.map((o) => (
          <option key={o.id} value={o.id}>
            {o.name}
            {!o.is_member ? " ·" : ""}
          </option>
        ))}
      </select>
      {/* A tenant reached through platform access rather than membership is
          someone else's, and an operator acting in it should be able to see
          that at a glance rather than infer it from the name. */}
      {current && !current.is_member && !current.is_platform && (
        <span className="org-guest" title="Reached through platform access, not membership">
          GUEST
        </span>
      )}
    </div>
  );
}
