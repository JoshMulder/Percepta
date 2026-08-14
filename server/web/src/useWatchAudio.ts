import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { AudioPayload } from "./types";
import { WatchAudio, type WatchAudioState } from "./watchAudio";

/**
 * The listening watch's socket and its audio engine, as one thing.
 *
 * Separate from `useSocket` on purpose. That socket is pinned to one station for
 * its whole life — the invariant that leaves the fan-out path with no
 * authorisation decision to get wrong — and a watch is the opposite: several
 * stations, across organisations, changing as the operator works. Bolting one
 * onto the other would mean weakening the pin for every tenant console in order
 * to serve the vendor's wall.
 *
 * THE GUARD SET IS SENT WHOLE, EVERY TIME, and re-sent on reconnect. A watch
 * lasts a shift on a screen nobody is looking at closely, and sockets drop. If
 * this replayed per-channel "guard this" messages, a reconnect that lost one
 * would leave the server guarding a channel the operator can no longer see on
 * their strip: audio with no lamp and no way to stop it. Sending the set makes
 * toggling and reconnecting the same operation.
 *
 * WHAT COMES BACK IS THE AUTHORITY. `watching` is the server's set, not an echo
 * of the request, and the strip renders from it — so a channel that was refused
 * simply is not lit, rather than appearing guarded because the message was
 * accepted in bulk.
 */

const CLOSE_UNAUTHENTICATED = 4401;
const CLOSE_FORBIDDEN = 4403;

export type WatchLink = "connecting" | "open" | "closed" | "denied";

export interface WatchApi {
  /** What the SERVER says is guarded. Render from this, never from the request. */
  guarded: string[];
  /** Replace the guard set. */
  setGuarded: (stationIds: string[]) => void;
  /** Channels with audio in the last few hundred ms. Polled rather than pushed:
   *  see below. */
  talking: Record<string, boolean>;
  link: WatchLink;
  audioState: WatchAudioState;
  volume: number;
  setVolume: (v: number) => void;
  priority: string | null;
  setPriority: (stationId: string | null) => void;
  replay: (stationId: string, seconds?: number) => boolean;
}

export function useWatchAudio(enabled: boolean): WatchApi {
  const engine = useMemo(() => new WatchAudio(), []);
  const [link, setLink] = useState<WatchLink>("closed");
  const [guarded, setGuardedState] = useState<string[]>([]);
  const [audioState, setAudioState] = useState<WatchAudioState>("off");
  const [volume, setVolumeState] = useState(0);
  const [priority, setPriorityState] = useState<string | null>(null);
  const [talking, setTalking] = useState<Record<string, boolean>>({});

  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  /** What the operator wants, replayed on every reconnect. */
  const desiredRef = useRef<string[]>([]);

  useEffect(() => engine.onState(setAudioState), [engine]);

  const send = useCallback((stationIds: string[]) => {
    const socket = socketRef.current;
    // `!socket` explicitly, not just the optional chain. `socket?.readyState`
    // on a null socket is `undefined`, and if `WebSocket.OPEN` is also
    // undefined — which it is under a stubbed WebSocket, and would be in any
    // environment without the global — the comparison is FALSE and execution
    // falls straight through to `null.send`. The chain reads like a guard and
    // is not one.
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: "watch_set", stations: stationIds }));
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
      // The whole set, immediately. This is the reconnect path as much as the
      // first connect — there is no separate resume.
      send(desiredRef.current);
    };

    socket.onmessage = (raw) => {
      let message: { type?: string; [k: string]: unknown };
      try {
        message = JSON.parse(raw.data as string);
      } catch {
        return;
      }
      if (message.type === "watching") {
        const stations = (message.stations as string[]) ?? [];
        setGuardedState(stations);
        engine.setChannels(stations);
        return;
      }
      if (message.type === "watch_revoked") {
        // The server dropped channels underneath us — a tenant deactivated a
        // station, or this operator came off the rota. Nothing to retry: the
        // next `watching` frame is the truth, and asking again would be asking
        // for something that was just refused.
        const gone = (message.stations as string[]) ?? [];
        desiredRef.current = desiredRef.current.filter((s) => !gone.includes(s));
        return;
      }
      if (message.type === "event" && message.stream === "audio") {
        engine.push(
          String(message.station_id),
          message.payload as unknown as AudioPayload,
        );
        return;
      }
    };

    socket.onclose = (ev) => {
      socketRef.current = null;
      if (ev.code === CLOSE_UNAUTHENTICATED || ev.code === CLOSE_FORBIDDEN) {
        // Reconnecting on either would spin for ever against a door that is not
        // going to open. Said plainly instead: an operator whose watch access
        // was removed mid-shift should see that, not a strip that quietly never
        // works again.
        setLink("denied");
        return;
      }
      setLink("closed");
      const delay = Math.min(30_000, 1000 * 2 ** retryRef.current);
      retryRef.current += 1;
      timerRef.current = window.setTimeout(connect, delay);
    };
  }, [enabled, engine, send]);

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

  useEffect(() => () => engine.close(), [engine]);

  /**
   * The talk lamps, polled at 10 Hz.
   *
   * POLLED, not pushed from the frame handler, and deliberately. A lamp has to
   * go OUT when audio stops, and "stopped" is the absence of frames — there is
   * no event for it, so something has to look at the clock. Driving React state
   * from the frame handler instead would also re-render the whole strip eight
   * times a second per channel while doing nothing for the off edge.
   */
  useEffect(() => {
    if (!enabled) return;
    const id = window.setInterval(() => {
      setTalking((previous) => {
        const next: Record<string, boolean> = {};
        let changed = false;
        for (const stationId of desiredRef.current) {
          next[stationId] = engine.isTalking(stationId);
          if (next[stationId] !== previous[stationId]) changed = true;
        }
        if (!changed && Object.keys(previous).length === Object.keys(next).length) {
          return previous;
        }
        return next;
      });
    }, 100);
    return () => window.clearInterval(id);
  }, [enabled, engine]);

  const setGuarded = useCallback(
    (stationIds: string[]) => {
      desiredRef.current = stationIds;
      // Optimistic locally so the strip responds to the click, but the server's
      // `watching` frame overwrites it a moment later and that one is the truth.
      setGuardedState(stationIds);
      engine.setChannels(stationIds);
      send(stationIds);
    },
    [engine, send],
  );

  const setVolume = useCallback(
    (v: number) => {
      setVolumeState(v);
      engine.setVolume(v);
      // Turning a slider up off zero is the gesture the browser wants AND an
      // unambiguous request for sound in a room. It is the only thing that
      // starts audio — see watchAudio.unmute.
      if (v > 0) engine.unmute();
    },
    [engine],
  );

  const setPriority = useCallback(
    (stationId: string | null) => {
      setPriorityState(stationId);
      engine.setPriority(stationId);
    },
    [engine],
  );

  const replay = useCallback(
    (stationId: string, seconds?: number) => engine.replay(stationId, seconds),
    [engine],
  );

  return {
    guarded,
    setGuarded,
    talking,
    link,
    audioState,
    volume,
    setVolume,
    priority,
    setPriority,
    replay,
  };
}
