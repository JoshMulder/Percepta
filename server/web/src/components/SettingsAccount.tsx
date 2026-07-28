import { useState } from "react";
import { ApiError, api } from "../api";
import type { Me } from "../types";

/** Your own name and password. Nothing here can widen what you may reach —
 *  that is decided by an admin, and the split is deliberate. */
export function SettingsAccount({
  me,
  onProfileChanged,
}: {
  me: Me;
  onProfileChanged: (displayName: string) => void;
}) {
  const [displayName, setDisplayName] = useState(me.display_name);
  const [savingName, setSavingName] = useState(false);
  const [nameMessage, setNameMessage] = useState<string | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [savingPassword, setSavingPassword] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  async function saveName(e: React.FormEvent) {
    e.preventDefault();
    setSavingName(true);
    setNameMessage(null);
    setNameError(null);
    try {
      const updated = await api.updateProfile(displayName.trim());
      onProfileChanged(updated.display_name);
      setNameMessage("Saved.");
    } catch (err) {
      setNameError(err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setSavingName(false);
    }
  }

  async function savePassword(e: React.FormEvent) {
    e.preventDefault();
    // Checked here as well as on the server. The server cannot check this one
    // at all — it never sees the confirmation field — so this is the only place
    // a mistyped repeat is caught.
    if (next !== confirm) {
      setPasswordError("The two new passwords do not match.");
      return;
    }
    setSavingPassword(true);
    setPasswordMessage(null);
    setPasswordError(null);
    try {
      const result = await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setPasswordMessage(
        result.other_sessions_ended > 0
          ? `Password changed. ${result.other_sessions_ended} other ${
              result.other_sessions_ended === 1 ? "session was" : "sessions were"
            } signed out.`
          : "Password changed. You had no other sessions open.",
      );
    } catch (err) {
      setPasswordError(
        err instanceof ApiError ? err.message : "Could not change the password.",
      );
    } finally {
      setSavingPassword(false);
    }
  }

  return (
    <div className="settings-sections">
      <section className="settings-section">
        <h3>Profile</h3>
        <form onSubmit={saveName}>
          <label className="field">
            <span>Display name</span>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              maxLength={255}
              required
            />
          </label>
          <label className="field">
            <span>Email</span>
            {/* Not editable. It is the login identifier and appears in audit
                rows as free text so they survive the account being deleted —
                changing it would rewrite who an old entry appears to be about. */}
            <input value={me.email} readOnly disabled />
            <small>
              Your email is your sign-in and is recorded against everything you
              do. Ask an administrator if it needs to change.
            </small>
          </label>
          <div className="settings-actions">
            <button
              type="submit"
              className="btn primary"
              disabled={savingName || !displayName.trim() || displayName === me.display_name}
            >
              {savingName ? "Saving…" : "Save"}
            </button>
            {nameMessage && <span className="settings-ok">{nameMessage}</span>}
            {nameError && <span className="settings-error">{nameError}</span>}
          </div>
        </form>
      </section>

      <section className="settings-section">
        <h3>Password</h3>
        <form onSubmit={savePassword}>
          <label className="field">
            <span>Current password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
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
              disabled={savingPassword || !current || !next || !confirm}
            >
              {savingPassword ? "Changing…" : "Change password"}
            </button>
            {passwordMessage && <span className="settings-ok">{passwordMessage}</span>}
            {passwordError && <span className="settings-error">{passwordError}</span>}
          </div>
        </form>
      </section>
    </div>
  );
}
