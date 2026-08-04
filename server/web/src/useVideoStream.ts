import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";

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
 *
 * **A dropped stream reconnects; a closed one does not.** The two look the same
 * on the wire - a socket closing - and mean opposite things. The viewer leaving
 * must stop the station; the platform redeploying, or a link blip, must not
 * strand an operator on a dead panel that says idle. So an unexpected close
 * tears the media pipeline down and tries again with a fresh ticket, backing
 * off to thirty seconds, forever - a security console left open overnight
 * should be showing the camera in the morning. Only two things stop the
 * retries: the hook's own cleanup, and the platform refusing the ticket
 * (403/404) - permission does not come back by asking more often, and every
 * attach starts the camera at the far end, which is satellite bandwidth.
 */

export type StreamState = "idle" | "connecting" | "playing" | "unavailable";

/** Staging-queue ceiling, in fragments. Sized to hold one whole group of
 *  pictures replayed at attach in a burst — the relay bounds a group at
 *  GOP_CACHE_MAX_FRAGMENTS (120) plus the init segment, and this leaves headroom
 *  for the live fragments that land while that burst drains. Kept in step with
 *  backend/realtime/media.py: too low and the burst's front (init/keyframe) is
 *  spliced away and nothing decodes. */
const MAX_PENDING = 192;

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
    let pending: ArrayBuffer[] = [];
    let objectUrl: string | null = null;
    let attempt = 0;
    let retryTimer: number | null = null;
    // Connections in a row that delivered no media at all. Distinct from
    // `attempt`, which paces the backoff: this one decides when to stop
    // asking. Reconnecting through a platform restart is the point of the
    // retries; reconnecting forever into a station whose camera is wedged
    // starts the encoder every thirty seconds, each start writes two events
    // to the station's log and burns its uplink for nothing, and the first
    // wedged station filled six minutes of journal with exactly that duet
    // before anyone looked. Ten silent connections is past every legitimate
    // spin-up; after that the panel says unavailable and a human decides.
    let silentConnections = 0;

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

    /** Unwind one connection's media pipeline so the next starts clean. A new
     *  connection is a new encoder session with its own parameter sets, and
     *  fragments appended to the old buffer decode as corruption. */
    const teardownMedia = () => {
      pending = [];
      buffer = null;
      try {
        if (source && source.readyState === "open") source.endOfStream();
      } catch {
        /* already torn down */
      }
      source = null;
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
        objectUrl = null;
      }
      if (video.current) {
        video.current.removeAttribute("src");
        // Not just the attribute: an element keeps its old MediaSource
        // attached until it reloads, and a codec change reuses this same
        // element immediately. Without the load() the new source never
        // attaches and the picture stops for good.
        video.current.load();
      }
    };

    const scheduleRetry = () => {
      if (cancelled || retryTimer !== null) return;
      attempt += 1;
      // 1s, 2s, 4s ... capped at 30s, with jitter so a fleet of consoles
      // severed by one redeploy does not reattach as a thundering herd.
      const delay =
        Math.min(30_000, 1000 * 2 ** Math.min(attempt - 1, 5)) *
        (0.75 + Math.random() * 0.5);
      setState("connecting");
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (cancelled) return;
      setState("connecting");
      let ticket: string;
      try {
        ticket = (await api.streamTicket(stationId)).ticket;
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
          // Not allowed, or no such station. Retrying cannot change either,
          // and every attach starts a camera somewhere.
          setState("unavailable");
          return;
        }
        scheduleRetry();
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
          let reset = false;
          try {
            const parsed = JSON.parse(event.data);
            codec = parsed.codec;
            reset = Boolean(parsed.reset);
          } catch {
            return;
          }
          if (!codec) return;
          if (source) {
            // A second codec means the station's encoder changed underneath
            // us - somebody switched the camera from H.265 to H.264, which is
            // a checkbox in its own web interface. A SourceBuffer's type is
            // fixed for its lifetime, so the whole MediaSource has to be
            // rebuilt; ignoring this (as this hook used to) leaves the player
            // decoding new bytes against the old codec's parameters, which
            // does not error - it just looks broken.
            if (!reset) return;
            teardownMedia();
          }
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
        // Bound the staging queue, but from BELOW live, never the front: a viewer
        // attaching mid-stream is handed the whole current group of pictures in
        // one burst (init + keyframe + the frames since), and splicing off the
        // front would drop the init segment or the keyframe and leave a run that
        // decodes as nothing. So the cap sits above the relay's per-group
        // fragment bound (GOP_CACHE_MAX_FRAGMENTS, +1 for the init segment, plus
        // headroom for the live fragments that arrive while the burst drains) and
        // trims the OLDEST only once past it. Latency is kept in check by catchUp,
        // not by this — this is only a memory bound.
        if (pending.length > MAX_PENDING) {
          pending.splice(0, pending.length - MAX_PENDING);
        }
        drain();
        if (!cancelled) {
          // Media arriving is the one signal that the whole path works, so it
          // is what resets the backoff - not the socket opening, which succeeds
          // and immediately dies while the platform is mid-restart.
          attempt = 0;
          silentConnections = 0;
          setState("playing");
        }
      };

      // onclose fires after onerror in every failure, so the close handler is
      // the one place reconnection is decided.
      socket.onerror = () => undefined;
      socket.onclose = () => {
        if (cancelled) return;
        const gotMedia = pending.length > 0 || buffer !== null;
        teardownMedia();
        if (!gotMedia && ++silentConnections >= 10) {
          setState("unavailable");
          return;
        }
        scheduleRetry();
      };
    };

    void connect();

    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      // Closing is what tells the platform the last viewer has gone, which is
      // what stops the station encoding. Not housekeeping - and `cancelled`
      // above is what keeps this close from reading as a dropout and
      // reconnecting a stream the viewer just left.
      socket?.close();
      socketRef.current = null;
      teardownMedia();
      setState("idle");
    };
  }, [stationId, enabled, video]);

  return state;
}
