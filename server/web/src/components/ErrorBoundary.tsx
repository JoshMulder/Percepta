import { Component, type CSSProperties, type ErrorInfo, type ReactNode } from "react";

/**
 * The console's last line of defence against a blank page.
 *
 * React unmounts any subtree that throws during render, and with nothing
 * catching that the whole app becomes a white screen — the worst failure mode
 * there is. It looks exactly like an outage, gives the operator nothing to act
 * on, and when the cause is a stale value in *this browser's* storage it
 * survives a sign-out and a reload, so the only escape is an incognito window.
 * That is a real incident report, not a hypothetical.
 *
 * So the crash is caught here and turned into the two actions that actually
 * recover:
 *   Reload            — a transient error, or a stale cached bundle after a deploy.
 *   Reset & reload    — clears this site's localStorage first, for the case a
 *                       persisted preference or radio preset is what crashes a
 *                       render. This is the self-serve form of DevTools' "clear
 *                       site data", for an operator who does not have DevTools open.
 *
 * The HttpOnly session cookie is deliberately NOT cleared: it is not a render
 * input, and dropping it would sign the operator out for a fault that has
 * nothing to do with their session.
 */
interface State {
  error: Error | null;
  /** The React component stack, shown in the panel as well as logged.
   *
   *  A production build minifies the message to "Minified React error #300",
   *  which names no component — and the console line below is easy to miss, or
   *  gone entirely when the crash is followed by a reload (the org switcher
   *  reloads deliberately, which was wiping the evidence before anyone could
   *  read it). Putting the top frames on screen means a photograph of the panel
   *  is enough to say WHICH component failed. */
  componentStack: string | null;
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error): State {
    return { error, componentStack: null };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The panel below is for the operator; this line is for whoever they send
    // the screenshot to — it keeps the real stack in the browser console.
    console.error("Console crashed:", error, info.componentStack);
    this.setState({ componentStack: info.componentStack ?? null });
  }

  /** The innermost few frames — the component that actually threw and its
   *  nearest parents. The whole stack reaches the app root and is noise. */
  private topFrames(): string {
    const frames = (this.state.componentStack ?? "")
      .split("\n")
      .map((f) => f.trim())
      .filter(Boolean)
      .slice(0, 5);
    return frames.join("\n");
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div style={WRAP}>
        <div style={CARD}>
          <h1 style={TITLE}>Something went wrong</h1>
          <p style={BODY}>
            The console hit an error it could not recover from. Reloading usually
            fixes it. If it keeps happening, resetting this browser's saved
            settings clears a stored value that may be the cause — it does not
            sign you out and it changes nothing on the server.
          </p>
          {error.message && <p style={DETAIL}>{error.message}</p>}
          {this.state.componentStack && (
            <pre style={STACK}>{this.topFrames()}</pre>
          )}
          <div style={ROW}>
            <button style={PRIMARY} onClick={() => window.location.reload()}>
              Reload
            </button>
            <button style={SECONDARY} onClick={resetAndReload}>
              Reset settings &amp; reload
            </button>
          </div>
        </div>
      </div>
    );
  }
}

function resetAndReload(): void {
  try {
    localStorage.clear();
  } catch {
    /* private mode or no storage; the reload on its own is the fallback */
  }
  window.location.reload();
}

// Inline styles on purpose: this renders when the app is broken, so it depends
// on nothing but the stylesheet's colour variables (loaded in main.tsx before
// anything can throw), with literal fallbacks in case even that is the problem.
const WRAP: CSSProperties = {
  minHeight: "100vh",
  display: "grid",
  placeItems: "center",
  padding: "1.5rem",
  background: "var(--bg, #0b0f13)",
  color: "var(--text, #e6edf3)",
};
const CARD: CSSProperties = {
  maxWidth: "32rem",
  background: "var(--panel, #11171d)",
  border: "1px solid var(--line, #223)",
  borderRadius: "0.5rem",
  padding: "1.5rem",
};
const TITLE: CSSProperties = { margin: "0 0 0.75rem", fontSize: "1.1rem" };
const BODY: CSSProperties = { margin: "0 0 0.75rem", lineHeight: 1.5, color: "var(--dim, #9fb0c0)" };
const DETAIL: CSSProperties = {
  margin: "0 0 1rem",
  padding: "0.5rem 0.625rem",
  fontFamily: "var(--mono, monospace)",
  fontSize: "0.8rem",
  color: "var(--dim, #9fb0c0)",
  background: "var(--panel-2, #0d141a)",
  borderRadius: "0.375rem",
  overflowWrap: "anywhere",
};
const STACK: CSSProperties = {
  ...DETAIL,
  whiteSpace: "pre-wrap",
  fontSize: "0.72rem",
  lineHeight: 1.4,
  maxHeight: "8rem",
  overflow: "auto",
};
const ROW: CSSProperties = { display: "flex", gap: "0.5rem", flexWrap: "wrap" };
const BUTTON: CSSProperties = {
  padding: "0.5rem 0.875rem",
  borderRadius: "0.375rem",
  border: "1px solid var(--line, #223)",
  cursor: "pointer",
  font: "inherit",
};
const PRIMARY: CSSProperties = {
  ...BUTTON,
  background: "var(--accent, #00a0dc)",
  border: "1px solid var(--accent, #00a0dc)",
  color: "#001019",
};
const SECONDARY: CSSProperties = {
  ...BUTTON,
  background: "transparent",
  color: "var(--text, #e6edf3)",
};
