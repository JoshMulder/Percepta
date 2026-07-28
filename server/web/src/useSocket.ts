import { useCallback, useEffect, useRef, useState } from "react";
import type {
  Capability,
  ClientMessage,
  ServerMessage,
  StreamName,
} from "./types";

export type SocketState = "connecting" | "open" | "closed" | "unauthenticated";

/** Close code the server uses for "authenticate and try again". Reconnecting on
 *  this would spin forever, so it is the one code that stops the loop. */
const CLOSE_UNAUTHENTICATED = 4401;

export interface SocketApi {
  state: SocketState;
  /** Capabilities for the currently pinned station, as the *server* resolved
   *  them. Never derived client-side - the console renders what the server says
   *  the user holds, so a stale local guess cannot show a control they lost. */
  capabilities: Capability[];
  selectStation: (id: string) => void;
  subscribe: (stream: StreamName) => void;
  unsubscribe: (stream: StreamName) => void;
  /** Set when the server ended the session. The UI should stop and say so
   *  rather than silently reconnecting into a logged-out state. */
  revoked: string | null;
}

export function useSocket(
  onMessage: (message: ServerMessage) => void,
  enabled: boolean,
): SocketApi {
  const [state, setState] = useState<SocketState>("closed");
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [revoked, setRevoked] = useState<string | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  // Latest handler without re-opening the socket every render.
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;
  // Replayed on reconnect so a dropped link restores what the user was watching.
  const desiredRef = useRef<{ station: string | null; streams: Set<StreamName> }>(
    { station: null, streams: new Set() },
  );

  const send = useCallback((message: ClientMessage) => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }, []);

  const connect = useCallback(() => {
    if (!enabled) return;
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);
    socketRef.current = socket;
    setState("connecting");

    socket.onopen = () => {
      setState("open");
      retryRef.current = 0;
      const { station, streams } = desiredRef.current;
      if (station) {
        send({ type: "select_station", ground_station_id: station });
        for (const stream of streams) send({ type: "subscribe", stream });
      }
    };

    socket.onmessage = (raw) => {
      let message: ServerMessage;
      try {
        message = JSON.parse(raw.data as string);
      } catch {
        return;
      }
      if (message.type === "station_selected") setCapabilities(message.capabilities);
      if (message.type === "station_revoked") {
        setCapabilities([]);
        desiredRef.current = { station: null, streams: new Set() };
      }
      if (message.type === "revoked") setRevoked(message.reason);
      handlerRef.current(message);
    };

    socket.onclose = (event) => {
      socketRef.current = null;
      if (event.code === CLOSE_UNAUTHENTICATED) {
        setState("unauthenticated");
        return;
      }
      setState("closed");
      if (!enabled) return;
      // Backoff capped at 10s. A ground station console is expected to sit
      // through Starlink dropouts and come back on its own, so it retries
      // indefinitely rather than giving up and needing a human to reload.
      const delay = Math.min(1000 * 2 ** retryRef.current, 10_000);
      retryRef.current += 1;
      timerRef.current = window.setTimeout(connect, delay);
    };
  }, [enabled, send]);

  useEffect(() => {
    if (!enabled) return;
    connect();
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      socket?.close();
    };
  }, [enabled, connect]);

  const selectStation = useCallback(
    (id: string) => {
      // Switching station drops the previous station's subscriptions server-side,
      // so the local intent has to be cleared too or a reconnect would try to
      // restore streams for a station we are no longer on.
      desiredRef.current = { station: id, streams: new Set() };
      setCapabilities([]);
      send({ type: "select_station", ground_station_id: id });
    },
    [send],
  );

  const subscribe = useCallback(
    (stream: StreamName) => {
      desiredRef.current.streams.add(stream);
      send({ type: "subscribe", stream });
    },
    [send],
  );

  const unsubscribe = useCallback(
    (stream: StreamName) => {
      desiredRef.current.streams.delete(stream);
      send({ type: "unsubscribe", stream });
    },
    [send],
  );

  return { state, capabilities, selectStation, subscribe, unsubscribe, revoked };
}
