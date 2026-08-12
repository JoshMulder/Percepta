import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";

/**
 * Confirming a new email address from an emailed link.
 *
 * Rendered before the session check, because the link may be opened in a browser
 * where the person is not signed in — the token is the whole authorisation. It
 * redeems on load rather than behind a button: following the link is the intent,
 * and there is nothing to fill in. Redemption does not revoke sessions, so a
 * signed-in tab stays signed in and this just confirms and hands them onward.
 *
 * The token is dropped from the address bar on success — single use by then, but
 * a live-looking link surviving in history is worth nothing and costs nothing to
 * remove.
 */
export function VerifyEmail({ token, onDone }: { token: string; onDone: () => void }) {
  const [state, setState] = useState<"working" | "done" | "error">("working");
  const [email, setEmail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Redeem exactly once, even under StrictMode's double-invoked effects: a second
  // call would find the token already spent and wrongly report failure.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    api
      .redeemEmailChange(token)
      .then((r) => {
        window.history.replaceState(null, "", window.location.pathname);
        setEmail(r.email);
        setState("done");
      })
      .catch((err) => {
        setError(
          err instanceof ApiError
            ? err.message
            : "That did not work. Ask for a new link.",
        );
        setState("error");
      });
  }, [token]);

  return (
    <div className="login">
      <div>
        {state === "working" && <h1>Confirming…</h1>}
        {state === "done" && (
          <>
            <h1>Email confirmed</h1>
            <p className="settings-note">
              Your sign-in email is now <b>{email}</b>. Use it next time you sign in.
            </p>
            <button type="button" className="btn primary" onClick={onDone}>
              Continue
            </button>
          </>
        )}
        {state === "error" && (
          <>
            <h1>Link not valid</h1>
            <p className="settings-error">{error}</p>
            <button type="button" className="btn primary" onClick={onDone}>
              Continue
            </button>
          </>
        )}
      </div>
    </div>
  );
}
