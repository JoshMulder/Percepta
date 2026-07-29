import { useState } from "react";
import { ApiError, api } from "../api";

/**
 * Setting a new password from an emailed link.
 *
 * Rendered before the session check, because the whole point is that the person
 * cannot sign in. The token in the query string is the only authorisation, and
 * it is spent by a successful submit.
 *
 * The token is dropped from the address bar on success. It is single use by
 * then, but a live-looking reset link surviving in history, or in a screenshot
 * of the address bar, is worth nothing to anyone and costs nothing to remove.
 */
export function ResetPassword({ token, onDone }: { token: string; onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const mismatch = repeat.length > 0 && password !== repeat;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (mismatch || !password) return;
    setBusy(true);
    setError(null);
    try {
      await api.redeemPasswordReset(token, password);
      window.history.replaceState(null, "", window.location.pathname);
      setDone(true);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "That did not work. Ask for a new link.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="login">
        <form onSubmit={(e) => { e.preventDefault(); onDone(); }}>
          <h1>Password set</h1>
          <p className="settings-note">
            You are signed out everywhere. Sign in with the new password.
          </p>
          <button type="submit" className="btn primary">
            Sign in
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="login">
      <form onSubmit={submit}>
        <h1>Choose a new password</h1>
        <label className="field">
          <span>New password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            autoFocus
            required
          />
        </label>
        <label className="field">
          <span>Repeat new password</span>
          <input
            type="password"
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
            autoComplete="new-password"
            required
          />
        </label>
        {mismatch && <p className="settings-error">Those do not match.</p>}
        <button
          type="submit"
          className="btn primary"
          disabled={busy || mismatch || !password}
        >
          {busy ? "Setting…" : "Set password"}
        </button>
        {error && <p className="settings-error">{error}</p>}
      </form>
    </div>
  );
}
