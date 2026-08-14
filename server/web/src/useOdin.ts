import { useEffect, useRef, useState } from "react";
import type { FleetStation } from "./types";

/**
 * The Odin wall's live feed.
 *
 * The wall used to poll: every operator asked for the whole fleet every fifteen
 * seconds and the server rebuilt it from scratch for each of them. This receives
 * one digest, computed once for the whole product, pushed every few seconds.
 *
 * THE FALLBACK IS LABELLED, ALWAYS. If the socket is down this reports that it
 * is down, and the caller keeps polling and says so on the status bar. Silently
 * degrading to a poll is how a wall becomes a screensaver: it still shows a
 * fleet, it still looks alive, and nobody learns it stopped being live until
 * they need it to have been. The whole point of the pip is that the screen
 * tells the truth about itself, and a quiet fallback would be the first lie.
 *
 * Reconnection is deliberately plain — a fixed delay with a little jitter, no
 * exponential backoff. A command centre screen is expected to be up; if the
 * platform is down the operator has larger problems than reconnect pressure, and
 * backing off to minutes means a wall that stays dark long after the thing it
 * watches has recovered.
 */

export type OdinLink = "connecting" | "live" | "down";

/** How long without a frame before the link is treated as stale rather than
 *  live. The server publishes every 3s whether or not anything changed, so
 *  silence is a fact about the connection and never about a quiet fleet. */
const STALE_AFTER_MS = 12_000;
const RETRY_MS = 3_000;

interface DigestFrame {
  type: string;
  at: string;
  stations: FleetStation[];
}

export function useOdin(enabled: boolean) {
  const [stations, setStations] = useState<FleetStation[] | null>(null);
  const [link, setLink] = useState<OdinLink>("connecting");
  const [lastFrameAt, setLastFrameAt] = useState<number | null>(null);

  // Held in a ref so the reconnect timer can be cleared from the cleanup
  // without the effect depending on it and tearing the socket down on every
  // render.
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled) {
      setLink("down");
      return;
    }
    let alive = true;

    const open = () => {
      if (!alive) return;
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      let socket: WebSocket;
      try {
        socket = new WebSocket(`${proto}//${window.location.host}/api/odin/ws`);
      } catch {
        // Constructing a socket can throw outright on some proxies. Treat it
        // exactly like a close: the caller falls back to polling either way.
        setLink("down");
        retryRef.current = window.setTimeout(open, RETRY_MS);
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => alive && setLink("live");

      socket.onmessage = (e) => {
        if (!alive) return;
        let frame: DigestFrame;
        try {
          frame = JSON.parse(e.data as string);
        } catch {
          return;
        }
        // An idle ping means the socket is up but the digest has stopped —
        // which is NOT the same as a quiet fleet, because the server publishes
        // on a timer regardless. Reported as down so the caller resumes polling
        // rather than showing a frozen wall as a current one.
        if (frame.type === "odin.idle") {
          setLink("down");
          return;
        }
        if (frame.type !== "odin.digest" || !Array.isArray(frame.stations)) return;
        setStations(frame.stations);
        setLastFrameAt(Date.now());
        setLink("live");
      };

      const reopen = () => {
        if (!alive) return;
        setLink("down");
        // 4403: access was taken away mid-shift. Reconnecting would be a tight
        // loop against a door that is now locked, so this stops and leaves the
        // caller on its polls — which will get their own 403 and say so.
        if (socket.__odinForbidden) return;
        retryRef.current = window.setTimeout(open, RETRY_MS + Math.random() * 1000);
      };

      socket.onclose = (e) => {
        if (e.code === 4403) {
          (socket as WebSocket & { __odinForbidden?: boolean }).__odinForbidden = true;
        }
        reopen();
      };
      socket.onerror = () => {
        // onerror is always followed by onclose; letting close do the work
        // avoids scheduling two reconnects for one failure.
      };
    };

    open();
    return () => {
      alive = false;
      if (retryRef.current) window.clearTimeout(retryRef.current);
      retryRef.current = null;
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    };
  }, [enabled]);

  // A socket that is open but has stopped delivering is the failure the idle
  // ping exists to catch; this is the belt to its braces, for the case where
  // even the ping does not arrive.
  useEffect(() => {
    if (link !== "live") return;
    const id = window.setInterval(() => {
      if (lastFrameAt && Date.now() - lastFrameAt > STALE_AFTER_MS) setLink("down");
    }, 2000);
    return () => window.clearInterval(id);
  }, [link, lastFrameAt]);

  return { stations, link, lastFrameAt };
}

declare global {
  interface WebSocket {
    /** Set when the server refused this session, so the reconnect loop stops
     *  hammering a door that will not open until the operator signs in again. */
    __odinForbidden?: boolean;
  }
}
