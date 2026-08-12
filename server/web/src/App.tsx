import { useEffect, useState } from "react";
import { api, setUnauthorizedHandler } from "./api";
import { Console } from "./components/Console";
import { Login } from "./components/Login";
import { PlatformDashboard } from "./components/PlatformDashboard";
import { ResetPassword } from "./components/ResetPassword";
import { VerifyEmail } from "./components/VerifyEmail";
import type { Me } from "./types";

/** The reset link's token, if this load is one. Read before the session check:
 *  somebody following a reset link is by definition unable to sign in.
 *
 *  From the fragment, which the browser never sends to the server — so the
 *  token does not appear in the reverse proxy's access log. The console is
 *  served by the API itself with an html fallback, so a token in the query
 *  string was a real request and a real log line every time somebody opened
 *  the link.
 *
 *  The query string is still accepted, for links already in somebody's inbox
 *  when this changed. Those keep working and keep being logged; there is
 *  nothing to be done about a link that has already been sent. */
function resetToken(): string | null {
  if (window.location.pathname !== "/reset-password") return null;
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  return fragment.get("token")
    ?? new URLSearchParams(window.location.search).get("token");
}

/** The email-verification link's token, if this load is one. Same fragment
 *  reasoning as the reset link — the token stays out of the proxy access log. */
function verifyEmailToken(): string | null {
  if (window.location.pathname !== "/verify-email") return null;
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  return fragment.get("token")
    ?? new URLSearchParams(window.location.search).get("token");
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);
  const [token, setToken] = useState<string | null>(resetToken);
  const [verifyToken, setVerifyToken] = useState<string | null>(verifyEmailToken);

  // The session lives in an HttpOnly cookie, so the only way to know whether we
  // are signed in is to ask.
  useEffect(() => {
    api
      .me()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setChecked(true));
  }, []);

  // Any 401 from anywhere — a session that expired mid-use or was revoked — sends
  // the operator back to the login screen, once, rather than letting the failing
  // call degrade the console into a blank or empty page they cannot escape.
  useEffect(() => {
    setUnauthorizedHandler(() => setMe(null));
    return () => setUnauthorizedHandler(null);
  }, []);

  if (token) {
    return (
      <ResetPassword
        token={token}
        onDone={() => {
          // Redeeming revoked every session this user had, including one that
          // may be open in this tab, so whatever `me` holds is stale.
          setMe(null);
          setToken(null);
          window.history.replaceState(null, "", "/");
        }}
      />
    );
  }

  if (verifyToken) {
    return (
      <VerifyEmail
        token={verifyToken}
        onDone={() => {
          // An email change keeps every session, so if this tab was signed in it
          // still is; clear the route and land on the app, and the session check
          // below shows login only if it was not signed in.
          setVerifyToken(null);
          window.history.replaceState(null, "", "/");
        }}
      />
    );
  }

  if (!checked) return <div className="booting">Loading…</div>;
  if (!me) return <Login onSignedIn={setMe} />;
  // Re-read the identity on demand — a roster nudge fires this so a renamed
  // organisation's name updates in place rather than on the next reload. A
  // failure is ignored: the 401 handler already covers a lost session.
  const refreshMe = () => {
    void api.me().then(setMe).catch(() => {});
  };
  // A platform admin gets the whole-estate dashboard, not the station console.
  // The predicate is session-scoped: descending into a customer org mints a
  // session with is_platform_admin off, so that view falls through to Console.
  if (me.is_platform_admin) {
    return (
      <PlatformDashboard me={me} onSignedOut={() => setMe(null)} refreshMe={refreshMe} />
    );
  }
  return <Console me={me} onSignedOut={() => setMe(null)} refreshMe={refreshMe} />;
}
