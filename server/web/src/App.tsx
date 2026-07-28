import { useEffect, useState } from "react";
import { api } from "./api";
import { Console } from "./components/Console";
import { Login } from "./components/Login";
import type { Me } from "./types";

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);

  // The session lives in an HttpOnly cookie, so the only way to know whether we
  // are signed in is to ask.
  useEffect(() => {
    api
      .me()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setChecked(true));
  }, []);

  if (!checked) return <div className="booting">Loading…</div>;
  if (!me) return <Login onSignedIn={setMe} />;
  return <Console me={me} onSignedOut={() => setMe(null)} />;
}
