import { useState } from "react";
import { api, ApiError } from "../api";
import { isChallenge, type LoginChallenge, type Me } from "../types";
import { Logo } from "./Logo";

/**
 * Sign in, with the second factor when an organisation requires one.
 *
 * Three states rather than three screens: credentials, then a code, and for
 * somebody who has never set MFA up, a QR to scan first. The email and password
 * are kept while the code is entered because the server re-checks all three
 * together - a challenge is not a session and grants nothing on its own.
 */
export function Login({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState<LoginChallenge | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.login(email, password, challenge ? code : undefined);
      if (isChallenge(result)) {
        setChallenge(result);
        setCode("");
      } else {
        onSignedIn(result);
      }
    } catch (err) {
      const unauthorised = err instanceof ApiError && err.status === 401;
      setError(
        !unauthorised
          ? "Could not sign in. Check the connection and try again."
          : challenge
            ? "That code was not accepted. Codes last about 30 seconds."
            : "Invalid email or password",
      );
    } finally {
      setBusy(false);
    }
  };

  const enrolling = challenge?.status === "mfa_enrollment_required";

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <Logo size="large" />

        {!challenge ? (
          <>
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
          </>
        ) : (
          <>
            {enrolling && (
              <div className="mfa-enrol">
                <p>
                  Scan this with an authenticator app, then enter the code it
                  shows.
                </p>
                {challenge.qr_svg && (
                  <img
                    className="mfa-qr"
                    src={challenge.qr_svg}
                    alt="Authenticator setup code"
                  />
                )}
                {challenge.secret && (
                  <p className="mfa-secret">
                    Or type this key in: <code>{challenge.secret}</code>
                  </p>
                )}
              </div>
            )}

            <label htmlFor="code">Authentication code</label>
            <input
              id="code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              // Six digits and nothing else. Pasting from an authenticator
              // routinely brings a space along with it.
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
              required
              autoFocus
            />
          </>
        )}

        {error && <div className="login-error">{error}</div>}

        <button type="submit" className="btn primary" disabled={busy}>
          {busy ? "Signing in…" : enrolling ? "Confirm and sign in" : "Sign in"}
        </button>

        {challenge && (
          <button
            type="button"
            className="btn ghost"
            onClick={() => {
              setChallenge(null);
              setCode("");
              setError(null);
            }}
          >
            Start again
          </button>
        )}
      </form>
      {/* Plain links, not router state: they are real addresses somebody can be
          sent, and they work before anyone has signed in. */}
      <p className="login-legal">
        <a href="/privacy">Privacy Policy</a>
        <span aria-hidden="true"> · </span>
        <a href="/terms">Terms of Use</a>
      </p>
    </div>
  );
}
