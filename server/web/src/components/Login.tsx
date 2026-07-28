import { useState } from "react";
import { api, ApiError } from "../api";
import type { Me } from "../types";
import { Logo } from "./Logo";

export function Login({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await api.login(email, password));
    } catch (err) {
      // The server answers every failure the same way on purpose, so there is
      // nothing more specific to show even if we wanted to.
      setError(
        err instanceof ApiError && err.status === 401
          ? "Invalid email or password"
          : "Could not sign in. Check the connection and try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <Logo size="large" />

        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="text"
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          autoFocus
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />

        {error && <div className="login-error">{error}</div>}

        <button type="submit" className="btn primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
