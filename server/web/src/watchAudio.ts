import type { AudioPayload } from "./types";

/**
 * Several airband channels at once, through ONE AudioContext.
 *
 * This is `useAudio` widened from one station to eight, and the widening is not
 * cosmetic — three things stop being free the moment there is more than one
 * channel, and each of them is a decision here rather than an accident.
 *
 * ONE CONTEXT, NOT ONE PER CHANNEL. Browsers cap AudioContexts hard (Chrome at
 * six), so eight contexts is not a heavy design, it is a design that stops
 * working at channel seven — with the failure landing on whichever channel the
 * operator happened to guard last. Contexts also have independent clocks, so
 * scheduling across them cannot be reasoned about at all.
 *
 * RESAMPLER STATE IS PER CHANNEL. `useAudio` carries a fractional read position
 * and the previous chunk's last sample across chunks, because restarting the
 * interpolation per chunk makes the waveform jump fifty times a second on any
 * context that is not an exact multiple of the stream rate — the 44.1 kHz bug
 * that read as "Chrome chops, Edge does not". Sharing one phase across eight
 * interleaved streams reintroduces exactly that, except now every channel
 * corrupts every other channel's phase and it happens on 48 kHz too.
 *
 * DECODERS ARE PER CHANNEL, for the same reason they are one-per-stream in
 * `useAudio`: Opus carries prediction state between packets, and feeding two
 * stations' packets to one decoder decodes each as if it followed the other's
 * audio.
 *
 * Not a React hook. The channel set changes as the operator guards and releases,
 * and audio state that is rebuilt when a component re-renders is audio that
 * clicks — so this is a plain object with a lifetime the UI does not control.
 * `useWatchAudio` is the thin hook around it.
 */

/** See useAudio: about two and a half burst intervals of lead, all latency, and
 *  irrelevant on a monitoring console next to speech that stutters. */
const BURST_LEAD_S = 0.3;

/** How long a channel counts as talking after its last frame.
 *
 *  Frames arrive about eight times a second while the squelch is open, so this
 *  is generous — it has to be, because the lamp is the ONLY talk indicator the
 *  watch has. Telemetry would say squelch_open authoritatively, but joining a
 *  station's telemetry group to light a lamp drags its full ADS-B down the same
 *  drop-oldest queue as the audio (see hub.watch_join), which would trade the
 *  sound for the lamp. A frame ARRIVING is the gate, at 125 ms granularity. */
const TALKING_MS = 400;

/** Ducking depth for non-priority channels while the priority one is talking.
 *
 *  -12 dB, not mute. An operator who has marked a channel priority still needs
 *  to know the others are active — dispatchers work by noticing a second channel
 *  open while listening to a first — and silencing them turns the strip into a
 *  single-channel radio with seven lamps. Down far enough to hear over, not far
 *  enough to lose. */
const DUCK_GAIN = 0.25;

/** Seconds of instant replay kept per channel.
 *
 *  Stored at the STATION'S rate rather than the context's, which is roughly half
 *  the memory, and allocated on a channel's first audio rather than when it is
 *  guarded — a quiet channel costs nothing. Thirty seconds is about two long
 *  overs, which is the actual use: "what did they just say".
 */
const REPLAY_SECONDS = 30;

export type WatchAudioState = "off" | "blocked" | "playing" | "unsupported";

interface Channel {
  stationId: string;
  gain: GainNode;
  panner: StereoPannerNode | null;
  decoder: AudioDecoder | null;
  /** Stream rate the decoder was configured for. A change rebuilds it. */
  rate: number;
  /** Decoder presentation timestamp, microseconds. */
  timestamp: number;
  /** Resampler carry — see the note above on why these are per channel. */
  phase: number;
  tail: number | null;
  /** Scheduled-path cursor: context time the next chunk should start at. */
  nextTime: number;
  /** Samples decoded but not yet scheduled, coalesced across one event-loop
   *  turn. Without this every 20 ms packet becomes its own BufferSource node —
   *  fifty a second per channel, four hundred across a full strip. */
  pending: Float32Array[];
  flushQueued: boolean;
  /** Instant replay, a ring at the station's rate. Lazily allocated. */
  ring: Float32Array | null;
  ringWrite: number;
  ringFilled: number;
  ringRate: number;
  /** performance.now() of the last frame, for the talk lamp. */
  lastFrameAt: number;
}

export class WatchAudio {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private channels = new Map<string, Channel>();
  private priority: string | null = null;
  private volume = 1;
  private stateListeners = new Set<(s: WatchAudioState) => void>();
  private _state: WatchAudioState = "off";

  get state(): WatchAudioState {
    return this._state;
  }

  onState(fn: (s: WatchAudioState) => void): () => void {
    this.stateListeners.add(fn);
    return () => this.stateListeners.delete(fn);
  }

  private setState(s: WatchAudioState): void {
    if (s === this._state) return;
    this._state = s;
    for (const fn of this.stateListeners) fn(s);
  }

  /** Build the context. Safe to call repeatedly; only the first does anything. */
  start(): void {
    if (this.ctx) return;
    let ctx: AudioContext;
    try {
      ctx = new AudioContext();
    } catch {
      this.setState("unsupported");
      return;
    }
    const master = ctx.createGain();
    master.gain.value = this.volume;
    master.connect(ctx.destination);
    this.ctx = ctx;
    this.master = master;

    // The context is the authority on its own state — track it rather than
    // inferring it once, which also covers a backgrounded tab being suspended.
    const sync = () =>
      this.setState(ctx.state === "running" ? "playing" : "blocked");
    ctx.addEventListener("statechange", sync);
    sync();

    // Asked, NOT awaited. Chrome leaves this promise pending — not rejected —
    // until autoplay is permitted, so awaiting it hangs the initialiser forever
    // and the state never leaves "off". That looked like a playback bug and was
    // a control-flow one; see useAudio for the full post-mortem.
    void ctx.resume().catch(() => {});
  }

  /** Resume on a user gesture. The volume control is the only thing that calls
   *  this: turning up a slider sitting at zero is an unambiguous request for
   *  sound in a room, which a stray click on a map is not. */
  unmute(): void {
    this.start();
    void this.ctx?.resume().catch(() => {});
  }

  setVolume(v: number): void {
    this.volume = v;
    if (this.master) this.master.gain.value = v;
  }

  /** Mark one channel as the one that matters, or null for none. */
  setPriority(stationId: string | null): void {
    this.priority = stationId;
    this.applyDucking();
  }

  /** Make the live channel set exactly `stationIds`, in strip order.
   *
   *  Order matters because pan position is derived from it: a channel's place in
   *  the stereo field is how an operator tells two simultaneous overs apart
   *  without looking, so it has to be STABLE and it has to match what is on the
   *  screen. Re-deriving it here, from the same list the strip renders, is what
   *  keeps those two from drifting.
   */
  setChannels(stationIds: string[]): void {
    for (const id of [...this.channels.keys()]) {
      if (!stationIds.includes(id)) this.removeChannel(id);
    }
    stationIds.forEach((id, index) => {
      const channel = this.ensureChannel(id);
      if (channel?.panner) {
        channel.panner.pan.value = panFor(index, stationIds.length);
      }
    });
    this.applyDucking();
  }

  private ensureChannel(stationId: string): Channel | null {
    const existing = this.channels.get(stationId);
    if (existing) return existing;
    this.start();
    const ctx = this.ctx;
    const master = this.master;
    if (!ctx || !master) return null;

    const gain = ctx.createGain();
    gain.gain.value = 1;
    // StereoPannerNode is not universal (older Safari). Without it every channel
    // sits centre, which is worse but not broken — so it is optional rather than
    // a reason for the strip not to work.
    let panner: StereoPannerNode | null = null;
    if (typeof ctx.createStereoPanner === "function") {
      panner = ctx.createStereoPanner();
      gain.connect(panner);
      panner.connect(master);
    } else {
      gain.connect(master);
    }

    const channel: Channel = {
      stationId,
      gain,
      panner,
      decoder: null,
      rate: 0,
      timestamp: 0,
      phase: 0,
      tail: null,
      nextTime: 0,
      pending: [],
      flushQueued: false,
      ring: null,
      ringWrite: 0,
      ringFilled: 0,
      ringRate: 0,
      lastFrameAt: 0,
    };
    this.channels.set(stationId, channel);
    return channel;
  }

  private removeChannel(stationId: string): void {
    const channel = this.channels.get(stationId);
    if (!channel) return;
    channel.decoder?.close();
    try {
      channel.gain.disconnect();
      channel.panner?.disconnect();
    } catch {
      // Already torn down with the context. Nothing to do and nothing to say.
    }
    this.channels.delete(stationId);
  }

  /** True while this channel has had audio within TALKING_MS. */
  isTalking(stationId: string): boolean {
    const channel = this.channels.get(stationId);
    if (!channel) return false;
    return performance.now() - channel.lastFrameAt < TALKING_MS;
  }

  private applyDucking(): void {
    const priorityTalking =
      this.priority !== null && this.isTalking(this.priority);
    for (const [id, channel] of this.channels) {
      const ducked = priorityTalking && id !== this.priority;
      const target = ducked ? DUCK_GAIN : 1;
      if (channel.gain.gain.value !== target) {
        // Ramped, not stepped. A gain that jumps mid-waveform is a click, and a
        // click on every over is worse than the ducking is worth.
        const ctx = this.ctx;
        if (ctx) {
          channel.gain.gain.setTargetAtTime(target, ctx.currentTime, 0.05);
        } else {
          channel.gain.gain.value = target;
        }
      }
    }
  }

  /** One audio frame for one station. */
  push(stationId: string, frame: AudioPayload): void {
    const channel = this.channels.get(stationId);
    if (!channel) return;
    const ctx = this.ctx;
    if (!ctx || ctx.state === "closed") return;
    if (frame.codec !== "opus") {
      // Dropped knowingly: the contract fixes the codec, so this is a station
      // ahead of this console, and playing unknown bytes as samples is a burst
      // of noise into a room.
      return;
    }

    channel.lastFrameAt = performance.now();
    this.applyDucking();

    const decoder = this.decoderFor(channel, frame);
    if (!decoder || decoder.state !== "configured") return;

    const perPacket = (frame.frame_ms || 20) * 1000; // microseconds
    for (const packet of frame.packets ?? []) {
      const bytes = Uint8Array.from(atob(packet), (c) => c.charCodeAt(0));
      decoder.decode(
        new EncodedAudioChunk({
          // Every Opus packet is independently decodable, and marking them key
          // is what lets playback start mid-transmission rather than waiting for
          // something that never comes.
          type: "key",
          timestamp: channel.timestamp,
          data: bytes,
        }),
      );
      channel.timestamp += perPacket;
    }
  }

  private decoderFor(channel: Channel, frame: AudioPayload): AudioDecoder | null {
    if (channel.decoder && channel.rate === frame.rate) return channel.decoder;
    if (typeof AudioDecoder === "undefined") {
      // Safari, and Firefox until recently. Reported rather than failed into
      // silence: an operator who cannot hear a channel needs to know it is their
      // browser and not the site.
      this.setState("unsupported");
      return null;
    }
    channel.decoder?.close();
    channel.timestamp = 0;
    channel.rate = frame.rate;
    // A new rate makes the carried phase meaningless — it is a position in a
    // stream that no longer exists.
    channel.phase = 0;
    channel.tail = null;

    const decoder = new AudioDecoder({
      output: (data) => {
        const samples = new Float32Array(data.numberOfFrames);
        try {
          data.copyTo(samples, { planeIndex: 0, format: "f32-planar" });
          this.record(channel, samples, data.sampleRate);
          this.enqueue(channel, samples, data.sampleRate);
        } finally {
          // Not garbage collected: a decoder whose outputs are never closed
          // stalls once its pool is exhausted, and the symptom is audio that
          // works for a few seconds and then stops for good.
          data.close();
        }
      },
      error: () => {
        // A corrupt packet must not kill the channel. Dropping the decoder means
        // the next frame builds a fresh one, at the cost of a moment of
        // prediction state.
        channel.decoder?.close();
        channel.decoder = null;
        channel.rate = 0;
      },
    });
    decoder.configure({
      codec: "opus",
      sampleRate: frame.rate,
      numberOfChannels: frame.channels || 1,
    });
    channel.decoder = decoder;
    return decoder;
  }

  /** Keep the last REPLAY_SECONDS of this channel, pre-resample. */
  private record(channel: Channel, samples: Float32Array, rate: number): void {
    if (!channel.ring || channel.ringRate !== rate) {
      channel.ring = new Float32Array(Math.ceil(rate * REPLAY_SECONDS));
      channel.ringWrite = 0;
      channel.ringFilled = 0;
      channel.ringRate = rate;
    }
    const ring = channel.ring;
    for (let i = 0; i < samples.length; i += 1) {
      ring[channel.ringWrite] = samples[i];
      channel.ringWrite = (channel.ringWrite + 1) % ring.length;
    }
    channel.ringFilled = Math.min(ring.length, channel.ringFilled + samples.length);
  }

  /**
   * Replay the last `seconds` of a channel, immediately, without disturbing the
   * live audio.
   *
   * Played through the SAME per-channel gain and panner, so a replay comes from
   * where that channel lives in the stereo field. An operator who hits replay on
   * channel three while channel one is talking has to be able to tell which is
   * which, and position is how — a replay routed to the master would arrive
   * centre and sound like a new station.
   */
  replay(stationId: string, seconds = 8): boolean {
    const channel = this.channels.get(stationId);
    const ctx = this.ctx;
    if (!channel || !ctx || !channel.ring || channel.ringFilled === 0) return false;

    const want = Math.min(
      channel.ringFilled,
      Math.floor(channel.ringRate * seconds),
    );
    if (want <= 0) return false;

    const out = new Float32Array(want);
    const ring = channel.ring;
    let read = (channel.ringWrite - want + ring.length) % ring.length;
    for (let i = 0; i < want; i += 1) {
      out[i] = ring[read];
      read = (read + 1) % ring.length;
    }

    const buffer = ctx.createBuffer(1, out.length, channel.ringRate);
    buffer.copyToChannel(out, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(channel.gain);
    source.start();
    return true;
  }

  /**
   * Coalesce decoded samples and schedule them as few buffers as possible.
   *
   * The decoder emits one AudioData per 20 ms packet, so scheduling directly
   * would create fifty BufferSource nodes a second per channel — four hundred
   * across a full strip, every second, for ever. Batching across one event-loop
   * turn collapses that to roughly one node per arriving frame per channel,
   * which is what the station actually sends.
   */
  private enqueue(channel: Channel, samples: Float32Array, rate: number): void {
    channel.pending.push(samples);
    channel.ringRate = rate;
    if (channel.flushQueued) return;
    channel.flushQueued = true;
    queueMicrotask(() => {
      channel.flushQueued = false;
      const chunks = channel.pending;
      channel.pending = [];
      if (chunks.length === 0) return;
      let total = 0;
      for (const c of chunks) total += c.length;
      const joined = new Float32Array(total);
      let at = 0;
      for (const c of chunks) {
        joined.set(c, at);
        at += c.length;
      }
      this.schedule(channel, joined, rate);
    });
  }

  private schedule(channel: Channel, samples: Float32Array, rate: number): void {
    const ctx = this.ctx;
    if (!ctx || ctx.state === "closed") return;

    // An AudioBuffer created at the stream's own rate is resampled by the
    // browser on playback, so the interpolation `useAudio` does for its worklet
    // path is not needed on this one. The phase/tail fields are kept per channel
    // regardless, because they are the state a worklet path would need and
    // sharing them across channels is the bug this class exists to avoid.
    const buffer = ctx.createBuffer(1, samples.length, rate);
    buffer.copyToChannel(samples, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(channel.gain);

    const lead = Math.max(BURST_LEAD_S, buffer.duration * 1.25);
    const now = ctx.currentTime;
    if (channel.nextTime < now + 0.02) channel.nextTime = now + lead;
    source.start(channel.nextTime);
    channel.nextTime += buffer.duration;
  }

  /** Drop everything queued for a channel — used when it is released, where
   *  playing out audio from a channel the operator has just let go is worse than
   *  a gap. */
  flush(stationId: string): void {
    const channel = this.channels.get(stationId);
    if (!channel) return;
    channel.decoder?.close();
    channel.decoder = null;
    channel.rate = 0;
    channel.timestamp = 0;
    channel.phase = 0;
    channel.tail = null;
    channel.pending = [];
    channel.nextTime = 0;
  }

  close(): void {
    for (const id of [...this.channels.keys()]) this.removeChannel(id);
    void this.ctx?.close();
    this.ctx = null;
    this.master = null;
    this.setState("off");
  }
}

/**
 * Where a channel sits in the stereo field.
 *
 * Spread across most of the field but never hard left or right: a channel panned
 * fully to one side is inaudible to an operator wearing one earpiece, which is
 * how half of them work. ±0.7 is wide enough to separate eight channels and
 * still present in both ears.
 *
 * A single channel sits centre. Panning the only thing playing would be a
 * gratuitous statement about a field with nothing else in it.
 */
export function panFor(index: number, count: number): number {
  if (count <= 1) return 0;
  const spread = 0.7;
  return -spread + (2 * spread * index) / (count - 1);
}
