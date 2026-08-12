import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api } from "../api";
import type { FleetStation } from "../types";

/**
 * A terminal on a station's HOST, in an xterm.js panel. The single most
 * consequential thing a platform admin can do from here — a root-capable shell
 * on a box in the field — so it is deliberately its own affordance, and every
 * open and close is audited server-side.
 *
 * The socket is a raw byte bridge (`realtime/host.py`): PTY output arrives as
 * binary and is written straight to the terminal; keystrokes go back as binary;
 * a resize is a small JSON control frame. Requesting the ticket also asks the
 * station to open its host session (over the audited, platform-admin-only
 * endpoint) — which only happens if the box has opted in twice (the compose
 * profile and the agent flag), so a station that has not simply never answers
 * and the terminal says so.
 */
export function StationHostShell({
  station,
  onClose,
}: {
  station: FleetStation;
  onClose: () => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  // A one-line status over the terminal until the socket is live, then again if
  // it closes — so an empty black rectangle is never mistaken for a dead shell.
  const [status, setStatus] = useState<string>("Opening a host session…");

  useEffect(() => {
    let disposed = false;
    let term: Terminal | undefined;
    let ws: WebSocket | undefined;
    let onWinResize: (() => void) | undefined;

    void (async () => {
      let ticket: string;
      try {
        ({ ticket } = await api.hostShellTicket(station.id));
      } catch {
        if (!disposed) setStatus("Could not open a host session.");
        return;
      }
      if (disposed || !holder.current) return;

      term = new Terminal({
        fontFamily: "var(--mono), ui-monospace, monospace",
        fontSize: 13,
        cursorBlink: true,
        // Matches the console palette so it reads as one surface.
        theme: { background: "#0c1219", foreground: "#dde6ed", cursor: "#00a0dc" },
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(holder.current);
      fit.fit();

      const encoder = new TextEncoder();
      const sendResize = () => {
        if (ws && ws.readyState === WebSocket.OPEN)
          ws.send(JSON.stringify({ t: "resize", cols: term!.cols, rows: term!.rows }));
      };

      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(
        `${scheme}://${window.location.host}/host/view?ticket=${ticket}`,
      );
      ws.binaryType = "arraybuffer";
      ws.onopen = () => {
        setStatus("");
        term!.focus();
        sendResize();
      };
      ws.onmessage = (event) => {
        // Binary is PTY output; a text frame is the platform's own message
        // (e.g. "the station did not open a host session"). Both are terminal
        // bytes to write.
        if (typeof event.data === "string") term!.write(event.data);
        else term!.write(new Uint8Array(event.data as ArrayBuffer));
      };
      ws.onclose = () => {
        if (!disposed) setStatus("The host session closed.");
      };
      ws.onerror = () => {
        if (!disposed) setStatus("The host session could not be reached.");
      };

      // Keystrokes back as binary, so the platform forwards them straight to the
      // PTY (it never interprets this stream).
      term.onData((data) => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
      });
      term.onResize(sendResize);

      onWinResize = () => {
        try {
          fit.fit();
        } catch {
          /* the panel is being torn down */
        }
      };
      window.addEventListener("resize", onWinResize);
    })();

    return () => {
      disposed = true;
      if (onWinResize) window.removeEventListener("resize", onWinResize);
      try {
        ws?.close();
      } catch {
        /* already gone */
      }
      try {
        term?.dispose();
      } catch {
        /* already gone */
      }
    };
  }, [station.id]);

  return createPortal(
    <div className="station-console-scrim" role="presentation" onClick={onClose}>
      <div
        className="station-console host-shell"
        role="dialog"
        aria-label={`${station.name} host shell`}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="station-console-head">
          <div>
            <strong>{station.name}</strong>
            <span className="station-console-org">host shell</span>
          </div>
          <button type="button" className="btn ghost tiny" onClick={onClose}>
            Close
          </button>
        </header>
        <div className="host-term-wrap">
          {status && <div className="host-term-status">{status}</div>}
          <div className="host-term" ref={holder} />
        </div>
      </div>
    </div>,
    document.body,
  );
}
