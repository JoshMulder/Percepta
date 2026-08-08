import { useCallback, useEffect, useRef, useState } from "react";

import type { AudioPayload } from "./types";

export type AudioState = "off" | "blocked" | "playing" | "unsupported";

/**
 * How far ahead of the clock playback is scheduled.
 *
 * The station publishes audio about eight times a second, and each message
 * carries roughly 120 ms as a handful of 20 ms Opus packets. So the buffer is
 * refilled in bursts, and the lead has to cover a whole burst interval plus
 * whatever jitter the link adds — otherwise the queue drains between bursts
 * and every underrun costs an audible gap.
 *
 * **600 ms, raised from 300 ms after measuring a real box.** 300 ms assumed
 * delivery jittered by about a burst interval. It does not: the station's audio
 * sub-tick shares a thread with the ~1 Hz sensing sweep — every sensor read,
 * the telemetry batch, the health pass — and on a constrained box (a Pi 2B) that
 * sweep stalls audio production for several hundred ms each second, so frames
 * arrive clumped into roughly per-second bursts with a gap behind each. The ring
 * was seen sawtoothing 0↔500 ms under a 300 ms lead and underrunning 1–2×/s;
 * 600 ms covers that gap. It is pure latency, and it does not matter here: this
 * is a monitoring console, not a duplex radio, and being half a second behind is
 * imperceptible next to speech that stutters.
 */
const BURST_LEAD_S = 0.6;

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
  // Resampler state for the worklet path, carried across chunks: the
  // fractional read position still owing, and the previous chunk's last
  // sample so the pair either side of a boundary can be interpolated. Null
  // tail means "nothing before this", which is true once per stream and after
  // every flush.
  const phaseRef = useRef(0);
  const tailRef = useRef<number | null>(null);
  // The Opus decoder, its running presentation timestamp, and the rate it was
  // configured for. Declared with the other refs rather than beside the
  // decode logic so the teardown below can close it.
  const decoderRef = useRef<AudioDecoder | null>(null);
  const timestampRef = useRef(0);
  const rateRef = useRef(0);
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
          // **Hashed, not a fixed name.** As `/pcm-worklet.js` out of
          // `public/` this had one URL for ever, so a browser that had cached
          // it once kept using it across every deploy — and an AudioWorklet
          // module is cached hard. A fixed buffer-sizing bug went on being
          // heard in one Chrome profile while the same build was clean in
          // Edge and in incognito, which cost a long detour through three
          // parts of the system that were already working.
          //
          // From `src/` with `new URL`, Vite emits it as a content-hashed
          // asset like everything else, so a changed worklet is a changed URL
          // and there is nothing left to serve stale.
          await ctx.audioWorklet.addModule(
            new URL("./pcm-worklet.js", import.meta.url).href);
          if (cancelled) return;
          const node = new AudioWorkletNode(ctx, "pcm-player");
          // The ring buffer's own account of itself, once a second. Every
          // stage before the browser has been measured and is clean, so this
          // is the one place left without numbers — and "Chrome chops, Edge
          // does not" is not a diagnosis.
          //
          // underruns  the ring ran dry; each one costs a full re-buffer, so
          //            even one a second is audible as chopping
          // trims      a burst overflowed the cap and audio was discarded
          // fed/consumed  milliseconds in against milliseconds out; these have
          //            to match or no buffer depth can save it
          // min/maxDepth  how close the ring came to empty
          node.port.onmessage = (event) => {
            const stat = event.data?.stat;
            if (!stat) return;
            console.info(
              "[audio] underruns %d trims %d | fed %dms consumed %dms | " +
              "depth %d-%dms (prebuffer %dms cap %dms) chunks %d",
              stat.underruns, stat.trims, stat.fedMs, stat.consumedMs,
              stat.minDepthMs, stat.maxDepthMs, stat.prebufferMs, stat.capMs,
              stat.chunks,
            );
          };
          node.connect(gain);
          nodeRef.current = node;
          setEngine("worklet");
        } catch {
          setEngine("scheduled");
        }
      } else {
        setEngine("scheduled");
      }

      // Logged once, because the difference between 48000 and 44100 here is
      // the difference between clean audio and audio that chops fifty times a
      // second, and it is not otherwise visible from outside the browser —
      // the same build sounded fine in one and broken in the other.
      console.info(
        "[audio] context %d Hz, engine %s",
        ctx.sampleRate, nodeRef.current ? "worklet" : "scheduled",
      );

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
      decoderRef.current?.close();
      decoderRef.current = null;
      rateRef.current = 0;
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

  /**
   * Hand decoded samples to whichever engine is running.
   *
   * Split out of `push` when audio became Opus: decoding is asynchronous and
   * arrives on the decoder's callback, so the playback half can no longer live
   * in the same synchronous call as the frame that produced it.
   */
  const play = useCallback((samples: Float32Array, rate: number) => {
    const ctx = ctxRef.current;
    if (!ctx || ctx.state === "closed") return;

    const node = nodeRef.current;
    if (node) {
      // Worklet path: resample to the context rate, since browsers rarely grant
      // a 24 kHz context, then hand the buffer over.
      //
      // **The phase carries across chunks, and that is the whole point.** This
      // read `src = i / ratio` and so restarted the interpolation at zero for
      // every chunk — and a chunk here is one 20 ms Opus packet. When the
      // context runs at 48 kHz the ratio is exactly 2, every boundary lands on
      // a whole sample, and nothing is audible. When it runs at 44.1 kHz the
      // ratio is 1.8375, every boundary lands mid-sample, and the waveform
      // jumps fifty times a second. Same build, same station: clean in a
      // browser that gave us 48 kHz and chopping in one that gave us 44.1.
      //
      // So the read position is kept in stream coordinates rather than chunk
      // coordinates, and the previous chunk's last sample is carried so the
      // pair either side of a boundary can still be interpolated between.
      let source = samples;
      let start = 0;
      const tail = tailRef.current;
      if (tail !== null) {
        source = new Float32Array(samples.length + 1);
        source[0] = tail;
        source.set(samples, 1);
        start = phaseRef.current;
      }
      let out = source;
      if (Math.abs(ctx.sampleRate - rate) > 1) {
        const step = rate / ctx.sampleRate;
        const produced: number[] = [];
        let pos = start;
        while (pos + 1 < source.length) {
          const j = Math.floor(pos);
          produced.push(source[j] + (source[j + 1] - source[j]) * (pos - j));
          pos += step;
        }
        // Whatever is left of the step lands in the next chunk, measured from
        // the sample that will be prepended to it.
        phaseRef.current = pos - (source.length - 1);
        out = new Float32Array(produced);
      }
      tailRef.current = source[source.length - 1] ?? null;
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

    // Lead sized from the *arrival* interval, not from the chunk.
    //
    // This read `max(0.14, duration * 1.25)`, written when a chunk was a whole
    // second of audio. Under Opus a chunk is one 20 ms packet — the decoder
    // emits one `AudioData` per packet — so the formula returns its 140 ms
    // floor, while frames arrive in bursts every 125 ms. The queue is then
    // barely one burst deep, any jitter underruns it, and the branch below
    // jumps the cursor forward and opens an audible gap. Continuously, which
    // is choppy speech rather than an occasional click.
    //
    // The lead has to cover an arrival interval plus the jitter on it, and it
    // is latency nobody is measuring against anything: this is a monitoring
    // console, not a duplex radio, and a third of a second behind real time is
    // imperceptible next to speech that stutters.
    const lead = Math.max(BURST_LEAD_S, buffer.duration * 1.25);
    const now = ctx.currentTime;
    if (nextTimeRef.current < now + 0.02) nextTimeRef.current = now + lead;
    source.start(nextTimeRef.current);
    nextTimeRef.current += buffer.duration;
  }, []);

  /**
   * Opus in, samples out, through WebCodecs.
   *
   * The contract carries **raw Opus packets with no container** — the
   * parameters a container would hold are stated in the same JSON frame — and
   * `AudioDecoder` is the one browser API that takes exactly that. Anything
   * else would mean shipping a wasm decoder to un-wrap a format that was never
   * wrapped.
   *
   * One decoder for the life of the stream, not one per frame. Opus carries
   * prediction state between packets, so a decoder rebuilt per frame decodes
   * each one as if it followed silence — which is audible, and wasteful.
   *
   * Configured lazily from the first frame that arrives, because the station
   * states its own rate and channel count and this console does not get to
   * assume them.
   */
  const decoderFor = useCallback((frame: AudioPayload): AudioDecoder | null => {
    if (decoderRef.current && rateRef.current === frame.rate) {
      return decoderRef.current;
    }
    if (typeof AudioDecoder === "undefined") {
      // Safari, and Firefox until recently. Reported rather than failed into
      // silence: an operator who cannot hear a transmission needs to know it
      // is their browser and not the site.
      setState("unsupported");
      return null;
    }
    decoderRef.current?.close();
    timestampRef.current = 0;
    rateRef.current = frame.rate;
    // A new rate makes the carried phase meaningless: it is a position in a
    // stream that no longer exists.
    phaseRef.current = 0;
    tailRef.current = null;

    const decoder = new AudioDecoder({
      output: (data) => {
        // `AudioData` is planar float32 for Opus. One channel is all this
        // contract carries, and taking channel 0 of a stereo frame would be
        // wrong rather than merely lossy — so it is asserted by the schema
        // and simply read here.
        const samples = new Float32Array(data.numberOfFrames);
        try {
          data.copyTo(samples, { planeIndex: 0, format: "f32-planar" });
          play(samples, data.sampleRate);
        } finally {
          // Not garbage collected. A decoder whose outputs are never closed
          // stalls once its internal pool is exhausted, and the symptom is
          // audio that works for a few seconds and then stops for good.
          data.close();
        }
      },
      error: () => {
        // A corrupt packet must not kill the stream. Dropping the decoder
        // means the next frame builds a fresh one, which costs a moment of
        // prediction state and nothing else.
        decoderRef.current?.close();
        decoderRef.current = null;
        rateRef.current = 0;
      },
    });
    decoder.configure({
      codec: "opus",
      sampleRate: frame.rate,
      numberOfChannels: frame.channels || 1,
    });
    decoderRef.current = decoder;
    return decoder;
  }, [play]);

  const push = useCallback((frame: AudioPayload) => {
    const ctx = ctxRef.current;
    if (!ctx || ctx.state === "closed") return;
    if (frame.codec !== "opus") {
      // Dropped knowingly. The contract fixes the codec, so this is a station
      // ahead of this console rather than a fault — and playing unknown bytes
      // as samples is a burst of noise into a room.
      return;
    }
    const decoder = decoderFor(frame);
    if (!decoder || decoder.state !== "configured") return;

    const perPacket = (frame.frame_ms || 20) * 1000;   // microseconds
    for (const packet of frame.packets ?? []) {
      const bytes = Uint8Array.from(atob(packet), (c) => c.charCodeAt(0));
      decoder.decode(new EncodedAudioChunk({
        // Opus has no inter-frame dependency of the kind video does: every
        // packet is decodable on its own, and marking them `key` is what lets
        // playback start mid-transmission rather than waiting for something
        // that never comes.
        type: "key",
        timestamp: timestampRef.current,
        data: bytes,
      }));
      timestampRef.current += perPacket;
    }
  }, [decoderFor]);

  const flush = useCallback(() => {
    // The decoder goes too. It holds prediction state for the channel that was
    // just left, and the buffer is flushed on retune precisely because playing
    // out the previous channel after the operator has moved is worse than a
    // gap — decoding it would be the same mistake one layer down.
    decoderRef.current?.close();
    decoderRef.current = null;
    rateRef.current = 0;
    timestampRef.current = 0;
    // The resampler's carry goes with it: interpolating from the last sample
    // of the channel just left into the first of the new one would smear the
    // two together, which is exactly what flushing exists to prevent.
    phaseRef.current = 0;
    tailRef.current = null;
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
