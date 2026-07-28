import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { Capability, Me, Member, OrganizationDetail } from "../types";

/**
 * Members, their roles, and what each may do at each station.
 *
 * Two things this UI has to communicate rather than hide, because getting them
 * wrong is how someone ends up with access nobody intended:
 *
 * An **admin holds every capability on every station implicitly**, so their
 * grant rows are meaningless while they are an admin. Showing them an empty
 * checkbox grid would be actively misleading.
 *
 * A **viewer can never hold an actuator capability**, whatever is ticked. The
 * ceiling is applied when access is evaluated, not when it is stored, so a
 * viewer's grant may legitimately contain capabilities that currently do
 * nothing — and restoring their role restores them. The grid marks those rather
 * than silently dropping them.
 */

/** Capabilities that do something physical at the station. A viewer may never
 *  hold one; the server enforces this, and the grid says so. */
const ACTUATORS = new Set<Capability>([
  "video.ptz",
  "radio.control",
  "light.control",
  "config.write",
]);

const LABELS: Record<string, string> = {
  "station.view": "See station",
  "telemetry.view": "Telemetry",
  "video.view": "Video",
  "video.ptz": "Move camera",
  "radio.listen": "Listen",
  "radio.control": "Tune radio",
  "light.control": "Floodlight",
  "media.review": "Recordings",
  "config.write": "Configure",
};

export function SettingsOrganization({ me }: { me: Me }) {
  const [org, setOrg] = useState<OrganizationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.organization();
      setOrg(data);
      setError(null);
      setSelected((s) => s ?? data.members[0]?.user_id ?? null);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load the organisation.",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await action();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work.");
    } finally {
      setBusy(null);
    }
  }

  if (error && !org) return <p className="settings-error">{error}</p>;
  if (!org) return <p className="settings-note">Loading…</p>;

  const member = org.members.find((m) => m.user_id === selected) ?? null;

  return (
    <div className="settings-sections">
      <section className="settings-section">
        <h3>{org.name}</h3>
        <div className="member-layout">
          <ul className="member-list">
            {org.members.map((m) => (
              <li key={m.user_id}>
                <button
                  type="button"
                  className={`member-item${m.user_id === selected ? " active" : ""}`}
                  onClick={() => setSelected(m.user_id)}
                >
                  <span className="member-name">
                    {m.display_name}
                    {m.user_id === me.user_id && <em> (you)</em>}
                  </span>
                  <span className="member-roles">{m.roles.join(", ") || "no role"}</span>
                </button>
              </li>
            ))}
          </ul>

          {member && (
            <MemberDetail
              key={member.user_id}
              me={me}
              org={org}
              member={member}
              busy={busy}
              onRoles={(roles) =>
                run(`roles:${member.user_id}`, () =>
                  api.setMemberRoles(member.user_id, roles),
                )
              }
              onGrant={(stationId, capabilities) =>
                run(`grant:${member.user_id}:${stationId}`, () =>
                  api.setMemberGrant(member.user_id, stationId, capabilities),
                )
              }
            />
          )}
        </div>
        {error && <p className="settings-error">{error}</p>}
      </section>
    </div>
  );
}

function MemberDetail({
  me,
  org,
  member,
  busy,
  onRoles,
  onGrant,
}: {
  me: Me;
  org: OrganizationDetail;
  member: Member;
  busy: string | null;
  onRoles: (roles: string[]) => void;
  onGrant: (stationId: string, capabilities: string[]) => void;
}) {
  const isAdmin = member.roles.includes("admin");
  const isViewer = member.roles.includes("viewer");
  const adminCount = org.members.filter((m) => m.roles.includes("admin")).length;
  const lastAdmin = isAdmin && adminCount <= 1;

  function capsFor(stationId: string): Capability[] {
    return member.grants.find((g) => g.ground_station_id === stationId)?.capabilities ?? [];
  }

  function toggle(stationId: string, capability: Capability) {
    const current = capsFor(stationId);
    const next = current.includes(capability)
      ? current.filter((c) => c !== capability)
      : [...current, capability];
    onGrant(stationId, next);
  }

  return (
    <div className="member-detail">
      <h4>{member.display_name}</h4>
      <p className="settings-note">{member.email}</p>

      <div className="field">
        <span>Role</span>
        <div className="role-buttons">
          {org.roles.map((role) => (
            <button
              key={role}
              type="button"
              className={`btn ghost${member.roles.includes(role) ? " active" : ""}`}
              disabled={
                busy === `roles:${member.user_id}` ||
                (lastAdmin && member.roles.includes("admin") && role !== "admin")
              }
              onClick={() => onRoles([role])}
            >
              {role}
            </button>
          ))}
        </div>
        {lastAdmin && (
          <small>
            The only administrator. Promote someone else before changing this —
            an organisation with no admin cannot be recovered from the console.
          </small>
        )}
      </div>

      {isAdmin ? (
        <p className="settings-note">
          Administrators hold every capability on every station in this
          organisation implicitly. Per-station grants below do not apply while
          this person is an admin.
        </p>
      ) : (
        <>
          {isViewer && (
            <p className="settings-note">
              A viewer can never hold the capabilities marked below, whatever is
              ticked. The limit is applied when access is checked, so ticking one
              stores it without granting it — and it takes effect if their role
              changes later.
            </p>
          )}
          <div className="grant-grid-wrap">
            <table className="grant-grid">
              <thead>
                <tr>
                  <th scope="col">Station</th>
                  {org.grantable_capabilities.map((c) => (
                    <th key={c} scope="col" title={c}>
                      {LABELS[c] ?? c}
                      {ACTUATORS.has(c) && <span className="actuator-mark">*</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {org.stations.map((station) => {
                  const caps = capsFor(station.id);
                  return (
                    <tr key={station.id}>
                      <th scope="row">{station.name}</th>
                      {org.grantable_capabilities.map((c) => {
                        const ineffective = isViewer && ACTUATORS.has(c);
                        return (
                          <td key={c}>
                            <input
                              type="checkbox"
                              checked={caps.includes(c)}
                              disabled={busy !== null}
                              onChange={() => toggle(station.id, c)}
                              aria-label={`${LABELS[c] ?? c} at ${station.name}`}
                              className={ineffective ? "ineffective" : undefined}
                              title={
                                ineffective
                                  ? "Stored, but a viewer cannot use this"
                                  : undefined
                              }
                            />
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <small>
            <span className="actuator-mark">*</span> Does something physical at
            the station. Every use is audited, and a viewer may never hold one.
            Changes take effect immediately, including on anyone currently
            watching a stream.
          </small>
          {me.user_id === member.user_id && (
            <p className="settings-note">
              These are your own permissions. As an administrator you can still
              reach every station regardless of what is ticked here.
            </p>
          )}
        </>
      )}
    </div>
  );
}
