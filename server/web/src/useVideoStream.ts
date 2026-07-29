import { useEffect, useRef, useState } from "react";
import { api } from "./api";

/**
 * Play a station's live stream into a `<video>` element.
 *
 * Media Source Extensions, no player library. The platform relays fragmented
 * MP4, which is exactly what MSE consumes, so the browser does the decoding and
 * nothing here parses a byte of media.
 *
 * The shape of the exchange, and why each part exists:
 *
 * **A ticket, then a socket.** A browser cannot set headers on a WebSocket, so
 * the authorisation has to travel in the URL - which is only acceptable because
 * a ticket is single-use, station-bound and worthless a minute later. The
 * session cookie never goes near a query string.
 *
 * **The codec arrives before the bytes.** MSE needs an exact codec string to
 * create a buffer, and a wrong one fails silently: no error, no picture. The
 * station reports what its encoder actually produced rather than the browser
 * guessing from the first fragment.
 *
 * **Attaching is what starts the camera.** The platform asks the station to
 * begin when the first viewer connects and to stop when the last leaves, so
 * closing this hook is what stops paying for satellite bandwidth. That makes
 * the cleanup path load-bearing rather than tidy-up.
 */

export type StreamState = "idle" | "connecting" | "playing" | "unavailable";

export function useVideoStream(
  video: React.RefObject<HTMLVideoElement | null>,
  stationId: string | null,
  enabled: boolean,
) {
  const [state, setState] = useState<StreamState>("idle");
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!enabled || !stationId || !video.current) {
      setState("idle");
      return;
    }
    if (typeof MediaSource === "undefined") {
      setState("unavailable");
      return;
    }

    let cancelled = false;
    let socket: WebSocket | null = null;
    let source: MediaSource | null = null;
    let buffer: SourceBuffer | null = null;
    const pending: ArrayBuffer[] = [];
    let objectUrl: string | null = null;

    /** Drop media the playhead has left behind.
     *
     *  Without this the SourceBuffer holds every frame ever played and the
     *  stream has a fuse on it: at ~3 Mbit/s Chrome's quota arrives in four to
     *  five minutes (measured - 89.4 MB and 278 s on the first real station),
     *  appends start throwing QuotaExceededError, and the picture collapses
     *  for a viewer who did nothing but watch. Thirty seconds of history is
     *  kept so a brief seek back still works; this is a live view, and the
     *  past belongs to recordings.
     */
    const trim = () => {
      const el = video.current;
      if (!buffer || buffer.updating || !el || !el.buffered.length) return;
      const start = el.buffered.start(0);
      const cut = el.currentTime - 30;
      if (cut - start > 30) {
        try {
          buffer.remove(start, cut);
        } catch {
          /* mid-teardown; the next updateend tries again */
        }
      }
    };

    /** Feed the buffer only when it is idle; MSE throws if it is not. */
    const drain = () => {
      if (!buffer || buffer.updating || pending.length === 0) return;
      const chunk = pending.shift();
      if (chunk) {
        try {
          buffer.appendBuffer(chunk);
        } catch (err) {
          if (chunk && (err as DOMException)?.name === "QuotaExceededError") {
            // Full, not broken. Put the fragment back, make room, and let the
            // updateend from remove() re-run this drain.
            pending.unshift(chunk);
            trim();
            return;
          }
          // Any other failed append means the stream and the buffer have
          // diverged - usually a new encoder session whose parameters do not
          // match. Drop what is queued rather than appending fragments that
          // will not decode.
          pending.length = 0;
        }
      }
    };

    /** Keep close to live. A stalled tab accumulates buffered video and would
     *  otherwise resume minutes behind, which on a security camera is worse
     *  than a gap: it looks current and is not. */
    const catchUp = () => {
      const el = video.current;
      if (!el || !el.buffered.length) return;
      const end = el.buffered.end(el.buffered.length - 1);
      if (end - el.currentTime > 4) el.currentTime = end - 0.5;
    };

    (async () => {
      setState("connecting");
      let ticket: string;
      try {
        ticket = (await api.streamTicket(stationId)).ticket;
      } catch {
        if (!cancelled) setState("unavailable");
        return;
      }
      if (cancelled) return;

      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(
        `${scheme}://${window.location.host}/media/view?ticket=${ticket}`,
      );
      socket.binaryType = "arraybuffer";
      socketRef.current = socket;

      socket.onmessage = (event) => {
        if (typeof event.data === "string") {
          // The codec handshake, and the only text the relay sends.
          let codec: string | undefined;
          try {
            codec = JSON.parse(event.data).codec;
          } catch {
            return;
          }
          if (!codec || source) return;
          const mime = `video/mp4; codecs="${codec}"`;
          if (!MediaSource.isTypeSupported(mime)) {
            setState("unavailable");
            return;
          }
          source = new MediaSource();
          objectUrl = URL.createObjectURL(source);
          if (video.current) video.current.src = objectUrl;
          source.addEventListener("sourceopen", () => {
            if (!source || source.readyState !== "open") return;
            buffer = source.addSourceBuffer(mime);
            buffer.mode = "segments";
            buffer.addEventListener("updateend", () => {
              drain();
              catchUp();
              trim();
            });
            drain();
          });
          return;
        }
        pending.push(event.data as ArrayBuffer);
        // Never let a backlog build: this is a live view, and old fragments are
        // worth less than the time it takes to play them.
        if (pending.length > 24) pending.splice(0, pending.length - 24);
        drain();
        if (!cancelled) setState("playing");
      };

      socket.onerror = () => !cancelled && setState("unavailable");
      socket.onclose = () => !cancelled && setState("idle");
    })();

    return () => {
      cancelled = true;
      // Closing is what tells the platform the last viewer has gone, which is
      // what stops the station encoding. Not housekeeping.
      socket?.close();
      socketRef.current = null;
      pending.length = 0;
      try {
        if (source && source.readyState === "open") source.endOfStream();
      } catch {
        /* already torn down */
      }
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      if (video.current) video.current.removeAttribute("src");
      setState("idle");
    };
  }, [stationId, enabled, video]);

  return state;
}
