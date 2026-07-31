import { useCallback, useEffect, useRef, useState } from "react";

export type AudioState = "off" | "blocked" | "playing" | "unsupported";

/**
 * Airband audio playback.
 *
 * Two engines, chosen at runtime:
 *
 *   worklet   Remote-Radio's AudioWorklet ring buffer, ported. Lowest latency
 *             and the best behaviour under jitter.
 *   scheduled AudioBufferSourceNodes queued against the context clock.
 *
 * The fallback exists because AudioWorklet is a **secure-context-only** API. A
 * console served over plain HTTP on a LAN address — which is exactly how this
 * gets deployed on a site network — has no `ctx.audioWorklet` at all, and the
 * first version of this failed into silence with nothing said. The scheduled
 * player works anywhere an AudioContext does.
 *
 * Also carried across from Remote-Radio: the buffer is flushed on retune,
 * because playing out the previous channel after the operator has moved is
 * worse than a gap.
 *
 * ON AUTOPLAY. Audio starts on its own whenever the browser permits it, and
 * silently waits for the first interaction when it does not. There is no way to
 * waive the rule from the page - it is enforced by the browser, not by us. For
 * a fixed console that should have sound from the moment it loads, the answer
 * is a deployment one: allow sound for the origin in the browser's site
 * settings, or launch it with --autoplay-policy=no-user-gesture-required. See
 * docs/04-production-readiness.md.
 */
export function useAudio(enabled: boolean) {
  const ctxRef = useRef<AudioContext | null>(null);
  const nodeRef = useRef<AudioWorkletNode | null>(null);
  const gainRef = useRef<GainNode | null>(null);
  // Scheduled-player cursor: when the next chunk should start.
  const nextTimeRef = useRef(0);
  const [engine, setEngine] = useState<"worklet" | "scheduled" | null>(null);
  const [state, setState] = useState<AudioState>("off");

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    (async () => {
      let ctx: AudioContext;
      try {
        ctx = new AudioContext();
      } catch {
        setState("unsupported");
        return;
      }
      if (cancelled) {
        void ctx.close();
        return;
      }

      const gain = ctx.createGain();
      gain.gain.value = 1;
      gain.connect(ctx.destination);
      ctxRef.current = ctx;
      gainRef.current = gain;

      // Worklet first; fall back without complaint if the page is not a secure
      // context, which is the common case on a site LAN.
      if (window.isSecureContext && ctx.audioWorklet) {
        try {
          await ctx.audioWorklet.addModule("/pcm-worklet.js");
          if (cancelled) return;
          const node = new AudioWorkletNode(ctx, "pcm-player");
          node.connect(gain);
          nodeRef.current = node;
          setEngine("worklet");
        } catch {
          setEngine("scheduled");
        }
      } else {
        setEngine("scheduled");
      }

      // The context is the authority on its own state, so track it rather than
      // inferring it once. This also covers the browser suspending us later,
      // which happens when a tab is backgrounded for long enough.
      const sync = () => setState(ctx.state === "running" ? "playing" : "blocked");
      ctx.addEventListener("statechange", sync);
      sync();

      // Ask, but DO NOT await.
      //
      // Chrome's resume() returns a promise that stays *pending* - not
      // rejected - until autoplay is permitted. Awaiting it here hung this
      // whole initialiser forever: the state was never set, so it stayed "off"
      // rather than "blocked", so the gesture listener below never attached,
      // and audio could not start by any route. It looked like a playback bug
      // and was a control-flow one.
      void ctx.resume().catch(() => {});
    })();

    return () => {
      cancelled = true;
      void ctxRef.current?.close();
      ctxRef.current = null;
      nodeRef.current = null;
      gainRef.current = null;
      setEngine(null);
      setState("off");
    };
  }, [enabled]);

  /**
   * Resume on the first interaction, silently.
   *
   * The gesture requirement itself is browser policy and cannot be waived — an
   * AudioContext created without one starts suspended, full stop. What *can* go
   * is asking the operator to do anything about it: they are going to click
   * something within seconds of the console loading, and audio simply starts
   * when they do. Prompting for a tap makes a browser rule look like a fault in
   * the product.
   *
   * Listeners are not `once`, because a resume can be refused if the gesture
   * was not user-initiated; retrying on the next one costs nothing.
   */
  /**
   * Deliberately NOT resumed by any click on the page.
   *
   * An earlier version listened for the first interaction anywhere and started
   * audio on it. That is the wrong behaviour for this console: an operator who
   * opens it to check a battery level should not have sound arrive because they
   * happened to click a map. Audio is loud, shared, and often unwanted in the
   * room the console is in.
   *
   * So the volume control is the only thing that starts it - see `unmute`.
   * Turning up a slider that is sitting at zero is an unambiguous request for
   * sound, and it is a user gesture, which is what the browser needs anyway.
   */
  const unmute = useCallback(() => {
    // Not awaited: Chrome leaves this promise pending rather than rejecting it
    // when autoplay is refused, and the statechange listener reports the result.
    void ctxRef.current?.resume().catch(() => {});
  }, []);

  const push = useCallback((pcmBase64: string, rate: number) => {
    const ctx = ctxRef.current;
    if (!ctx || ctx.state === "closed") return;

    const bytes = Uint8Array.from(atob(pcmBase64), (c) => c.charCodeAt(0));
    const pcm = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length >> 1);
    const samples = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i += 1) samples[i] = pcm[i] / 32768;

    const node = nodeRef.current;
    if (node) {
      // Worklet path: resample to the context rate, since browsers rarely grant
      // a 24 kHz context, then hand the buffer over.
      let out = samples;
      if (Math.abs(ctx.sampleRate - rate) > 1) {
        const ratio = ctx.sampleRate / rate;
        const resampled = new Float32Array(Math.round(samples.length * ratio));
        for (let i = 0; i < resampled.length; i += 1) {
          const src = i / ratio;
          const j = Math.floor(src);
          const a = samples[j] ?? 0;
          const b = samples[j + 1] ?? a;
          resampled[i] = a + (b - a) * (src - j);
        }
        out = resampled;
      }
      node.port.postMessage(out, [out.buffer]);
      return;
    }

    // Scheduled path. An AudioBuffer created at the stream's own rate is
    // resampled by the browser on playback, so no interpolation is needed here.
    const gain = gainRef.current;
    if (!gain) return;
    const buffer = ctx.createBuffer(1, samples.length, rate);
    buffer.copyToChannel(samples, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(gain);

    // Lead sized from the chunk, for the same reason as the worklet: a fixed
    // 140 ms was ported from a client that received small chunks many times a
    // second, and this station sends one second of audio once a second. A lead
    // shorter than a chunk underruns on the first scrap of jitter and then
    // re-establishes the same too-short lead, so it never recovers.
    const lead = Math.max(0.14, buffer.duration * 1.25);
    const now = ctx.currentTime;
    if (nextTimeRef.current < now + 0.02) nextTimeRef.current = now + lead;
    source.start(nextTimeRef.current);
    nextTimeRef.current += buffer.duration;
  }, []);

  const flush = useCallback(() => {
    nodeRef.current?.port.postMessage({ cmd: "flush" });
    // Scheduled path: nothing already queued can be unscheduled cheaply, so the
    // cursor is reset and the next chunk starts fresh.
    nextTimeRef.current = 0;
  }, []);

  const setVolume = useCallback((v: number) => {
    if (gainRef.current) gainRef.current.gain.value = v;
  }, []);

  return { push, flush, setVolume, unmute, state, engine };
}
