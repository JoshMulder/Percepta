import { useCallback, useEffect, useRef, useState } from "react";

import type { EventPayload } from "./types";

/**
 * Live telemetry for the ONE station an operator has deliberately opened.
 *
 * The wall's digest is a 3-second snapshot of vitals the ingest already had —
 * cheap, fleet-wide, and the right thing for a screen showing two hundred sites.
 * This is the opposite: a real subscription to one station's stream, taken
 * because somebody decided to look at it, and given up the moment they stop.
 *
 * ATTACHED ON OPEN, DETACHED ON CLOSE, NEVER ACCUMULATED. The telemetry stream
 * is undifferentiated — adsb, power, radio, light, weather and health all arrive
 * on it — so an attach carries that site's whole ADS-B feed at 1 Hz whether or
 * not anybody wanted aircraft. Fair for the station somebody is looking at,
 * absurd multiplied by a shift's worth of stations they merely glanced at. The
 * server enforces one at a time; this hook makes the client agree rather than
 * relying on being told off.
 *
 * A SECOND SOCKET, sharing the watch's route. `/api/odin/watch` is already the
 * authenticated cross-tenant surface, so attaching over it needs no new door and
 * no ticket. It is deliberately NOT the console's `/ws`: that socket pins one
 * station for its whole life, and `subscribe` there builds the group name from
 * the CALLER'S org — which for an ODIN operator is the platform org, producing a
 * group nobody publishes to. The failure would be silence, not an error.
 *
 * STATE IS CLEARED ON EVERY STATION CHANGE. Showing the previous station's
 * readings under a new station's name for the second before the first frame
 * arrives is worse than showing nothing: an operator cannot tell a stale figure
 * from a current one, and the whole point of attaching is to trust what is on
 * the screen.
 */

const CLOSE_UNAUTHENTICATED = 4401;
const CLOSE_FORBIDDEN = 4403;

export type AttachLink = "connecting" | "open" | "closed" | "denied";

export interface AttachedTelemetry {
  /** Latest payload per `kind`: health, power, weather, radio, light, adsb. */
  live: Record<string, EventPayload>;
  /** What the SERVER says is attached. Null until it confirms. */
  attached: string | null;
  link: AttachLink;
  /** When the last frame arrived, so the drawer can say how fresh it is. */
  lastFrameAt: number | null;
}

export function useOdinAttach(
  stationId: string | null,
  enabled: boolean,
): AttachedTelemetry {
  const [live, setLive] = useState<Record<string, EventPayload>>({});
  const [attached, setAttached] = useState<string | null>(null);
  const [link, setLink] = useState<AttachLink>("closed");
  const [lastFrameAt, setLastFrameAt] = useState<number | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  /** What the operator has open, replayed on reconnect. */
  const wantRef = useRef<string | null>(null);
  wantRef.current = stationId;

  const send = useCallback((id: string | null) => {
    const socket = socketRef.current;
    // `!socket` explicitly, not just the optional chain. `socket?.readyState`
    // on a null socket is `undefined`, and if `WebSocket.OPEN` is also
    // undefined — which it is under a stubbed WebSocket, and would be in any
    // environment without the global — the comparison is FALSE and execution
    // falls straight through to `null.send`. The chain reads like a guard and
    // is not one.
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(
      JSON.stringify({ type: "attach_station", ground_station_id: id }),
    );
  }, []);

  const connect = useCallback(() => {
    if (!enabled) return;
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(
      `${protocol}://${window.location.host}/api/odin/watch`,
    );
    socketRef.current = socket;
    setLink("connecting");

    socket.onopen = () => {
      retryRef.current = 0;
      setLink("open");
      // Whatever is open right now, not whatever was open when the socket
      // dropped — a reconnect after the operator moved on must not re-attach
      // the station they left.
      send(wantRef.current);
    };

    socket.onmessage = (raw) => {
      let message: { type?: string; [k: string]: unknown };
      try {
        message = JSON.parse(raw.data as string);
      } catch {
        return;
      }
      if (message.type === "attached") {
        setAttached((message.ground_station_id as string | null) ?? null);
        return;
      }
      if (message.type === "event" && message.stream === "telemetry") {
        const payload = message.payload as EventPayload & {
          kind?: string;
          available?: boolean;
        };
        // `available === false` means the station is declaring a sensor it does
        // not currently have — no readings, just the declaration. The console
        // learned this the expensive way on first hardware: rendering it as a
        // reading crashed the panel. Kept, not dropped, so the drawer can say
        // "fitted, not reporting" rather than showing nothing at all.
        if (!payload?.kind) return;
        setLive((previous) => ({ ...previous, [payload.kind as string]: payload }));
        setLastFrameAt(Date.now());
        return;
      }
    };

    socket.onclose = (ev) => {
      socketRef.current = null;
      if (ev.code === CLOSE_UNAUTHENTICATED || ev.code === CLOSE_FORBIDDEN) {
        // Retrying a door that will not open spins for ever. The drawer falls
        // back to the digest snapshot, which is what it showed before this
        // hook existed — degraded, but honest and still useful.
        setLink("denied");
        return;
      }
      setLink("closed");
      const delay = Math.min(30_000, 1000 * 2 ** retryRef.current);
      retryRef.current += 1;
      timerRef.current = window.setTimeout(connect, delay);
    };
  }, [enabled, send]);

  useEffect(() => {
    if (!enabled) return;
    connect();
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      socket?.close();
    };
  }, [enabled, connect]);

  // The station changed: clear first, then ask. Clearing on the way in rather
  // than when the first new frame lands is what stops the previous station's
  // readings appearing under this one's name.
  useEffect(() => {
    setLive({});
    setLastFrameAt(null);
    setAttached(null);
    send(stationId);
  }, [stationId, send]);

  // Detach when the drawer closes or the wall unmounts. Without this the server
  // holds the subscription until the socket dies — which on a wall display that
  // is never closed is "for ever", for a station nobody is looking at.
  useEffect(() => {
    return () => {
      if (socketRef.current?.readyState === WebSocket.OPEN) send(null);
    };
  }, [send]);

  return { live, attached, link, lastFrameAt };
}
