import { useEffect, useState } from "react";
import { api } from "./api";
import { Console } from "./components/Console";
import { Login } from "./components/Login";
import { ResetPassword } from "./components/ResetPassword";
import type { Me } from "./types";

/** The reset link's token, if this load is one. Read before the session check:
 *  somebody following a reset link is by definition unable to sign in. */
function resetToken(): string | null {
  if (window.location.pathname !== "/reset-password") return null;
  return new URLSearchParams(window.location.search).get("token");
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);
  const [token, setToken] = useState<string | null>(resetToken);

  // The session lives in an HttpOnly cookie, so the only way to know whether we
  // are signed in is to ask.
  useEffect(() => {
    api
      .me()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setChecked(true));
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

  if (!checked) return <div className="booting">Loading…</div>;
  if (!me) return <Login onSignedIn={setMe} />;
  return <Console me={me} onSignedOut={() => setMe(null)} />;
}
