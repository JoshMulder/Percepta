import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { PlatformOverview, PlatformUser } from "../types";

/**
 * Organisations and the people in them. Platform administrators only.
 *
 * This is the one screen that reaches across tenants, and it is available only
 * while the signed-in session's active organisation *is* the platform
 * organisation. That is deliberate: a platform admin working inside a
 * customer's organisation sees exactly what that organisation's own members
 * see, and this tab is not there.
 *
 * Creating an account and giving it access are kept as two steps. They are
 * different decisions, and an account that silently lands in an organisation is
 * how someone ends up in a tenant nobody meant to put them in.
 */
export function SettingsPlatform() {
  const [data, setData] = useState<PlatformOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.platform();
      setData(next);
      setSelected((s) => s ?? next.users[0]?.user_id ?? null);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the platform.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !data) return <p className="settings-error">{error}</p>;
  if (!data) return <p className="settings-note">Loading…</p>;

  const user = data.users.find((u) => u.user_id === selected) ?? null;

  return (
    <div className="settings-sections">
      <Organizations
        data={data}
        busy={busy}
        onCreate={(name) => run(() => api.createOrganization(name))}
        onRename={(id, name) => run(() => api.renameOrganization(id, name))}
      />

      <section className="settings-section">
        <h3>People</h3>
        <NewUser busy={busy} onCreate={(body) => run(() => api.createUser(body))} />
        <div className="member-layout">
          <ul className="member-list">
            {data.users.map((u) => (
              <li key={u.user_id}>
                <button
                  type="button"
                  className={`member-item${u.user_id === selected ? " active" : ""}`}
                  onClick={() => setSelected(u.user_id)}
                >
                  <span className="member-name">
                    {u.display_name}
                    {u.is_platform_admin && <em> · platform</em>}
                    {!u.is_active && <em> · disabled</em>}
                  </span>
                  <span className="member-roles">
                    {u.memberships.length === 0
                      ? "no organisation"
                      : u.memberships.map((m) => m.organization_name).join(", ")}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {user && (
            <UserDetail
              key={user.user_id}
              user={user}
              data={data}
              busy={busy}
              onSet={(orgId, roles) =>
                run(() => api.setMembership(user.user_id, orgId, roles))
              }
              onRemove={(orgId) =>
                run(() => api.removeMembership(user.user_id, orgId))
              }
            />
          )}
        </div>
        {error && <p className="settings-error">{error}</p>}
      </section>
    </div>
  );
}

function Organizations({
  data,
  busy,
  onCreate,
  onRename,
}: {
  data: PlatformOverview;
  busy: boolean;
  onCreate: (name: string) => void;
  onRename: (id: string, name: string) => void;
}) {
  const [name, setName] = useState("");
  const [adding, setAdding] = useState(false);
  /** The org whose name is being edited, or null. Held as the id so the row
   *  re-reads the live name each render rather than freezing a stale copy. */
  const [editing, setEditing] = useState<string | null>(null);
  const [editName, setEditName] = useState("");

  function saveRename(id: string, current: string) {
    const next = editName.trim();
    // A no-op rename is not worth a round trip; the server treats it the same.
    if (next && next !== current) onRename(id, next);
    setEditing(null);
  }

  return (
    <section className="settings-section">
      <h3>Organisations</h3>
      <div className="grant-grid-wrap">
        <table className="grant-grid org-table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Members</th>
              <th scope="col">Stations</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {data.organizations.map((o) => (
              <tr key={o.id}>
                <th scope="row">
                  {editing === o.id ? (
                    <input
                      className="org-name-edit"
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      maxLength={255}
                      autoFocus
                      aria-label={`Rename ${o.name}`}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") saveRename(o.id, o.name);
                        if (e.key === "Escape") setEditing(null);
                      }}
                    />
                  ) : (
                    <>
                      {o.name}
                      {o.is_platform && <span className="actuator-mark"> ·platform</span>}
                    </>
                  )}
                </th>
                <td>{o.member_count}</td>
                <td>{o.is_platform ? "—" : o.station_count}</td>
                <td className="org-actions">
                  {editing === o.id ? (
                    <>
                      <button
                        type="button"
                        className="btn primary"
                        disabled={busy || !editName.trim() || editName.trim() === o.name}
                        onClick={() => saveRename(o.id, o.name)}
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setEditing(null)}
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="btn ghost"
                      disabled={busy}
                      onClick={() => {
                        setEditing(o.id);
                        setEditName(o.name);
                      }}
                    >
                      Rename
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {adding ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onCreate(name.trim());
            setName("");
            setAdding(false);
          }}
        >
          <label className="field">
            <span>Organisation name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={255}
              autoFocus
              required
            />
          </label>
          <div className="settings-actions">
            <button type="submit" className="btn primary" disabled={busy || !name.trim()}>
              Create
            </button>
            <button type="button" className="btn ghost" onClick={() => setAdding(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <div className="settings-actions">
          <button
            type="button"
            className="btn primary"
            disabled={busy}
            onClick={() => setAdding(true)}
          >
            Add organisation
          </button>
        </div>
      )}
      <small>
        A new organisation starts empty. Add people to it below, then its own
        administrators create stations and grant access within it.
      </small>
    </section>
  );
}

function NewUser({
  busy,
  onCreate,
}: {
  busy: boolean;
  onCreate: (body: { email: string; display_name: string; password: string | null }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");

  if (!open) {
    return (
      <div className="settings-actions">
        <button type="button" className="btn ghost" disabled={busy} onClick={() => setOpen(true)}>
          Add person
        </button>
      </div>
    );
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onCreate({
          email: email.trim(),
          display_name: displayName.trim(),
          password: password ? password : null,
        });
        setEmail("");
        setDisplayName("");
        setPassword("");
        setOpen(false);
      }}
    >
      <div className="field-row">
        <label className="field">
          <span>Name</span>
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required autoFocus />
        </label>
        <label className="field">
          <span>Email</span>
          <input value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
      </div>
      <label className="field">
        <span>Password (optional)</span>
        <input
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <small>
          Leave blank and the account exists but cannot be signed in to until a
          password is set. There is no invite email yet, so leaving it blank
          means arranging one out of band.
        </small>
      </label>
      <div className="settings-actions">
        <button type="submit" className="btn primary" disabled={busy || !email.trim() || !displayName.trim()}>
          Create
        </button>
        <button type="button" className="btn ghost" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function UserDetail({
  user,
  data,
  busy,
  onSet,
  onRemove,
}: {
  user: PlatformUser;
  data: PlatformOverview;
  busy: boolean;
  onSet: (organizationId: string, roles: string[]) => void;
  onRemove: (organizationId: string) => void;
}) {
  const notIn = data.organizations.filter(
    (o) => !user.memberships.some((m) => m.organization_id === o.id),
  );
  const [addOrg, setAddOrg] = useState("");
  const [addRole, setAddRole] = useState("operator");

  return (
    <div className="member-detail">
      <h4>{user.display_name}</h4>
      <p className="settings-note">{user.email}</p>

      <div className="grant-grid-wrap">
        <table className="grant-grid org-table">
          <thead>
            <tr>
              <th scope="col">Organisation</th>
              <th scope="col">Role</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {user.memberships.length === 0 && (
              <tr>
                <td colSpan={3}>Not a member of any organisation.</td>
              </tr>
            )}
            {user.memberships.map((m) => (
              <tr key={m.organization_id}>
                <th scope="row">{m.organization_name}</th>
                <td>
                  <select
                    value={m.roles[0] ?? "operator"}
                    disabled={busy}
                    onChange={(e) => onSet(m.organization_id, [e.target.value])}
                  >
                    {data.roles.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <button
                    type="button"
                    className="btn ghost danger-text"
                    disabled={busy}
                    onClick={() => onRemove(m.organization_id)}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {notIn.length > 0 && (
        <div className="field-row">
          <label className="field">
            <span>Add to organisation</span>
            <select value={addOrg} onChange={(e) => setAddOrg(e.target.value)}>
              <option value="">Choose…</option>
              {notIn.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Role</span>
            <select value={addRole} onChange={(e) => setAddRole(e.target.value)}>
              {data.roles.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <div className="settings-actions">
            <button
              type="button"
              className="btn primary"
              disabled={busy || !addOrg}
              onClick={() => {
                onSet(addOrg, [addRole]);
                setAddOrg("");
              }}
            >
              Add
            </button>
          </div>
        </div>
      )}

      <small>
        Removing someone from an organisation also removes their station grants
        in it — a grant in an org you are not a member of gives nothing anyway,
        and leaving the rows behind makes an access review read as though they
        still had access. Membership of the Platform organisation is what makes
        someone a platform administrator.
      </small>
    </div>
  );
}
