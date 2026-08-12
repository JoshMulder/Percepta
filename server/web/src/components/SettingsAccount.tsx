import { useState } from "react";
import { ApiError, api } from "../api";
import type { Me } from "../types";
import { Modal } from "./Modal";

/**
 * Your own account: an overview, with editing behind explicit buttons.
 *
 * Nothing here can widen what you may reach — that is decided by an admin, and
 * the split is deliberate. Editing is a modal rather than an always-live form so
 * the resting state is a glanceable summary, not a page of inputs inviting a
 * stray keystroke.
 */
export function SettingsAccount({
  me,
  onProfileChanged,
}: {
  me: Me;
  onProfileChanged: (displayName: string) => void;
}) {
  // The name is held here so the overview updates the moment a save lands,
  // without waiting for the parent to thread a fresh `me` back down.
  const [displayName, setDisplayName] = useState(me.display_name);
  const [editing, setEditing] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);

  return (
    <div className="settings-sections">
      <section className="settings-section">
        <h3>Account</h3>
        <dl className="settings-facts">
          <dt>Name</dt>
          <dd>{displayName}</dd>
          <dt>Email</dt>
          <dd>{me.email}</dd>
        </dl>
        <div className="settings-actions">
          <button type="button" className="btn primary" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button
            type="button"
            className="btn ghost"
            onClick={() => setChangingPassword(true)}
          >
            Change password
          </button>
        </div>
      </section>

      {editing && (
        <EditProfileModal
          me={me}
          displayName={displayName}
          onClose={() => setEditing(false)}
          onSaved={(name) => {
            setDisplayName(name);
            onProfileChanged(name);
            setEditing(false);
          }}
        />
      )}

      {changingPassword && (
        <ChangePasswordModal onClose={() => setChangingPassword(false)} />
      )}
    </div>
  );
}

/**
 * Edit the display name.
 *
 * Email is shown but not yet editable here — a self-service change goes through
 * a verification link to the new address, which is a separate build. Until then
 * it stays read-only rather than offering a field that cannot save.
 */
function EditProfileModal({
  me,
  displayName,
  onClose,
  onSaved,
}: {
  me: Me;
  displayName: string;
  onClose: () => void;
  onSaved: (displayName: string) => void;
}) {
  const [name, setName] = useState(displayName);
  const [savingName, setSavingName] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);

  // The email change is a separate flow (its own form, so it does not nest
  // inside the name form) and a separate build on the server: the request needs
  // the current password and only sends a verification link — nothing moves
  // until that link is opened.
  const [changingEmail, setChangingEmail] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [emailPassword, setEmailPassword] = useState("");
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailMessage, setEmailMessage] = useState<string | null>(null);
  const [emailError, setEmailError] = useState<string | null>(null);

  async function saveName(e: React.FormEvent) {
    e.preventDefault();
    setSavingName(true);
    setNameError(null);
    try {
      const updated = await api.updateProfile(name.trim());
      onSaved(updated.display_name);
    } catch (err) {
      setNameError(err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setSavingName(false);
    }
  }

  async function requestEmail(e: React.FormEvent) {
    e.preventDefault();
    setEmailBusy(true);
    setEmailError(null);
    setEmailMessage(null);
    try {
      const result = await api.requestEmailChange(newEmail.trim(), emailPassword);
      setEmailMessage(
        `Verification link sent to ${result.sent_to}. Open it to confirm — your ` +
          `sign-in email changes only then.`,
      );
      setNewEmail("");
      setEmailPassword("");
      setChangingEmail(false);
    } catch (err) {
      setEmailError(
        err instanceof ApiError ? err.message : "Could not start the change.",
      );
    } finally {
      setEmailBusy(false);
    }
  }

  return (
    <Modal title="Edit account" onClose={onClose}>
      <form onSubmit={saveName}>
        <label className="field">
          <span>Display name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={255}
            autoFocus
            required
          />
        </label>
        <div className="settings-actions">
          <button
            type="submit"
            className="btn primary"
            disabled={savingName || !name.trim() || name === displayName}
          >
            {savingName ? "Saving…" : "Save name"}
          </button>
          {nameError && <span className="settings-error">{nameError}</span>}
        </div>
      </form>

      <div className="field">
        <span>Email</span>
        {!changingEmail ? (
          <div className="settings-actions">
            <span>{me.email}</span>
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                setEmailMessage(null);
                setEmailError(null);
                setChangingEmail(true);
              }}
            >
              Change email
            </button>
          </div>
        ) : (
          <form onSubmit={requestEmail}>
            <label className="field">
              <span>New email</span>
              <input
                type="email"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                autoComplete="email"
                autoFocus
                required
              />
            </label>
            <label className="field">
              <span>Current password</span>
              <input
                type="password"
                value={emailPassword}
                onChange={(e) => setEmailPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
            <div className="settings-actions">
              <button
                type="submit"
                className="btn primary"
                disabled={emailBusy || !newEmail.trim() || !emailPassword}
              >
                {emailBusy ? "Sending…" : "Send verification"}
              </button>
              <button
                type="button"
                className="btn ghost"
                onClick={() => setChangingEmail(false)}
                disabled={emailBusy}
              >
                Cancel
              </button>
            </div>
          </form>
        )}
        {emailMessage && <span className="settings-ok">{emailMessage}</span>}
        {emailError && <span className="settings-error">{emailError}</span>}
        <small>
          Changing your email sends a confirmation link to the new address and
          needs your current password. Your sign-in only changes once you open
          the link.
        </small>
      </div>

      <div className="settings-actions">
        <button type="button" className="btn ghost" onClick={onClose}>
          Close
        </button>
      </div>
    </Modal>
  );
}

/**
 * Change the password, current → new.
 *
 * The confirmation is checked here as well as by the browser: the server never
 * sees the repeat field, so a mistyped repeat can only be caught client-side. A
 * successful change signs out every other session, including a console left
 * streaming; this one stays.
 */
function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (next !== confirm) {
      setError("The two new passwords do not match.");
      return;
    }
    setSaving(true);
    setMessage(null);
    setError(null);
    try {
      const result = await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setMessage(
        result.other_sessions_ended > 0
          ? `Password changed. ${result.other_sessions_ended} other ${
              result.other_sessions_ended === 1 ? "session was" : "sessions were"
            } signed out.`
          : "Password changed. You had no other sessions open.",
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change the password.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title="Change password" onClose={onClose}>
      {message ? (
        <>
          <p className="settings-ok">{message}</p>
          <div className="settings-actions">
            <button type="button" className="btn primary" onClick={onClose}>
              Done
            </button>
          </div>
        </>
      ) : (
        <form onSubmit={submit}>
          <label className="field">
            <span>Current password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              autoFocus
              required
            />
          </label>
          <label className="field">
            <span>New password</span>
            <input
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              required
            />
            <small>At least 12 characters. Length matters more than symbols.</small>
          </label>
          <label className="field">
            <span>Repeat new password</span>
            <input
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
            />
          </label>
          <p className="settings-note">
            Changing your password signs out every other session immediately,
            including any console left streaming. This one stays signed in.
          </p>
          <div className="settings-actions">
            <button
              type="submit"
              className="btn primary"
              disabled={saving || !current || !next || !confirm}
            >
              {saving ? "Changing…" : "Change password"}
            </button>
            <button type="button" className="btn ghost" onClick={onClose} disabled={saving}>
              Cancel
            </button>
            {error && <span className="settings-error">{error}</span>}
          </div>
        </form>
      )}
    </Modal>
  );
}
