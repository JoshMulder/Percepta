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

export function SettingsOrganization({
  me,
  onStationCreated,
}: {
  me: Me;
  /** A station was just created here. Naming it is all this page does; the
   *  operator is then taken to it on the Stations tab to finish setup. */
  onStationCreated: (stationId: string) => void;
}) {
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

        <label className="field checkbox-field">
          <input
            type="checkbox"
            checked={org.mfa_required ?? false}
            disabled={busy === "mfa"}
            onChange={(e) =>
              run("mfa", () => api.setOrgMfaRequired(e.target.checked))
            }
          />
          <span>Require two-factor authentication</span>
        </label>

        <InviteMember roles={org.roles} onInvited={load} />
        <AddStation onCreated={onStationCreated} />

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

      <PasswordReset member={member} />

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

/**
 * Send this member a password reset link.
 *
 * The admin never sees or chooses the password. A password an administrator
 * picked is known to two people from the moment it exists, and is the one
 * nobody changes.
 *
 * Confirmed before sending, because it invalidates any link already outstanding
 * and, once redeemed, signs the person out everywhere.
 */
function PasswordReset({ member }: { member: Member }) {
  const [state, setState] = useState<"idle" | "confirming" | "sending" | "sent">(
    "idle",
  );
  const [error, setError] = useState<string | null>(null);

  async function send() {
    setState("sending");
    setError(null);
    try {
      await api.sendPasswordReset(member.user_id);
      setState("sent");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "The reset could not be sent.",
      );
      setState("idle");
    }
  }

  if (state === "sent") {
    return (
      <p className="settings-ok">
        Reset link sent to {member.email}. It works once and expires.
      </p>
    );
  }

  return (
    <div className="settings-actions">
      {state === "confirming" ? (
        <>
          <button
            type="button"
            className="btn primary"
            disabled={state !== "confirming"}
            onClick={() => void send()}
          >
            Send the link
          </button>
          <button
            type="button"
            className="btn ghost"
            onClick={() => setState("idle")}
          >
            Cancel
          </button>
          <span className="settings-note">
            Any link already sent stops working. Once used, this signs them out
            everywhere.
          </span>
        </>
      ) : (
        <button
          type="button"
          className="btn ghost"
          disabled={state === "sending"}
          onClick={() => setState("confirming")}
        >
          {state === "sending" ? "Sending…" : "Send password reset"}
        </button>
      )}
      {error && <span className="settings-error">{error}</span>}
    </div>
  );
}

/**
 * Add someone to this organisation.
 *
 * No password field, deliberately. They receive a link and choose their own —
 * a password an admin typed here is one two people know before it has been used
 * once.
 */
function InviteMember({
  roles,
  onInvited,
}: {
  roles: string[];
  onInvited: () => void | Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState(roles.includes("viewer") ? "viewer" : roles[0]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.inviteMember(email.trim(), name.trim(), [role]);
      // One message either way. The other branch said "already had an
      // account", which told an org admin whether an address belongs to a
      // user in somebody else's tenancy — the question the invite endpoint's
      // own docstring says it must not answer. Both cases are now emailed, so
      // "sent" is true whichever it was.
      setMessage(`Invitation sent to ${result.email}.`);
      setEmail("");
      setName("");
      setOpen(false);
      await onInvited();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add them.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="settings-actions">
        <button
          type="button"
          className="btn primary"
          onClick={() => {
            setOpen(true);
            setMessage(null);
          }}
        >
          Add someone
        </button>
        {message && <span className="settings-ok">{message}</span>}
      </div>
    );
  }

  return (
    <form onSubmit={submit}>
      <div className="field-row">
        <label className="field">
          <span>Email</span>
          <input
            type="text"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
            required
          />
        </label>
        <label className="field">
          <span>Name</span>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </label>
        <label className="field">
          <span>Role</span>
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            {roles.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="settings-actions">
        <button
          type="submit"
          className="btn primary"
          disabled={busy || !email.trim() || !name.trim()}
        >
          {busy ? "Sending…" : "Send invitation"}
        </button>
        <button type="button" className="btn ghost" onClick={() => setOpen(false)}>
          Cancel
        </button>
        {error && <span className="settings-error">{error}</span>}
      </div>
    </form>
  );
}

/**
 * Create a station — name only.
 *
 * Naming is the whole job here. Everything else about a station (its position,
 * its enrolment code) belongs on that station's own page, so on create the
 * operator is taken straight there rather than filling a longer form in a modal.
 * The timezone is taken from the browser, which is right far more often than UTC
 * and is editable on the station's configuration form afterwards.
 */
function AddStation({
  onCreated,
}: {
  onCreated: (stationId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Escape closes only this modal, not the settings dialog behind it. The
  // capture phase runs before the settings' own window-level Escape handler, and
  // stopping the event there keeps one press from closing both.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const station = await api.createStation({
        name: name.trim(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        latitude: null,
        longitude: null,
      });
      setOpen(false);
      setName("");
      onCreated(station.id);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not create the station.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="settings-actions">
      <button
        type="button"
        className="btn primary"
        onClick={() => {
          setName("");
          setError(null);
          setOpen(true);
        }}
      >
        Add station
      </button>

      {open && (
        <div className="modal-scrim" onClick={() => setOpen(false)}>
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-label="Add station"
            onClick={(e) => e.stopPropagation()}
          >
            <h3>New station</h3>
            <form onSubmit={submit}>
              <label className="field">
                <span>Name</span>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Kaikoura Ridge"
                  maxLength={255}
                  autoFocus
                  required
                />
              </label>
              <p className="settings-note">
                The record belongs to your organisation — the hardware does not
                have to exist yet. You will be taken to it to set its position and
                issue an enrolment code.
              </p>
              <div className="settings-actions">
                <button
                  type="submit"
                  className="btn primary"
                  disabled={saving || !name.trim()}
                >
                  {saving ? "Creating…" : "Create station"}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setOpen(false)}
                >
                  Cancel
                </button>
                {error && <span className="settings-error">{error}</span>}
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
