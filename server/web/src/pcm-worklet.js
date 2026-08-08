// Ring-buffer PCM player, ported from Remote-Radio's client.
//
// The main thread posts Float32Array chunks at the context sample rate; this
// prebuffers, then plays, emitting silence and re-buffering on underrun.
// Backlog beyond maxBuffer is dropped, so a network hiccup cannot permanently
// add latency — which matters more here than there: these consoles sit behind
// Starlink, where a brief stall is normal and audio that drifts a second behind
// stays a second behind for the rest of the shift.
//
// **Both thresholds are sized from the audio that actually arrives**, and that
// is the fix for a year of choppy audio. The ported constants — 90 ms of
// prebuffer, a 300 ms latency cap — were right for Remote-Radio, which sent
// small chunks many times a second over a LAN. A station that demodulates one
// second of audio per tick and sends it at 1 Hz overflowed the 300 ms cap the
// instant a chunk landed and was truncated to the last 90 ms: nine-tenths of
// every second discarded before it could play. Hardcoding a bigger number
// would fix one station and break the next, so the player measures instead.
//
// What it measures has to be the *arrival interval*, not the chunk. Those were
// the same thing until audio became Opus and the main thread began posting one
// 20 ms packet at a time; see BURST_LEAD_S below for what that cost.

// How much to hold before playing, when the chunks cannot say.
//
// The sizing below measures the chunk, and that worked while a chunk was what
// the station sent. Under Opus it is not: the main thread posts one 20 ms
// packet at a time, because the decoder emits one `AudioData` per packet, while
// the station sends a burst of six or seven of them eight times a second. So
// `chunk.length` stopped describing how the audio *arrives*, and 20 ms of it
// bought a 90 ms prebuffer under a 130 ms cap — less than one burst. Every
// burst overflowed the cap on landing, the ring was trimmed back to the
// prebuffer, and the discarded audio was a gap eight times a second, which is
// speech that cuts in and out constantly rather than an occasional click.
//
// 600 ms, raised from 300 ms after measuring a real box. The old number
// assumed delivery jittered by about a burst interval; it does not. The
// station's audio sub-tick shares a thread with the ~1 Hz sensing sweep, and on
// a Pi 2B that sweep stalls audio production for several hundred ms each second,
// so frames arrive clumped into roughly per-second bursts. Under a 300 ms lead
// the ring was seen sawtoothing 0↔500 ms and underrunning 1–2×/s; 600 ms covers
// the gap. Same value and reasoning as `BURST_LEAD_S` in `useAudio.ts`, the
// scheduled player's half of this.
const BURST_LEAD_S = 0.6;

class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = sampleRate * 2; // 2 s ring
    this.buf = new Float32Array(this.size);
    this.r = 0;
    this.w = 0;
    this.playing = false;
    // Floors, until a chunk says otherwise. Anything smaller than an arrival
    // interval is unusable; anything much larger is latency nobody asked for.
    this.minPrebuffer = Math.floor(sampleRate * BURST_LEAD_S);
    this.prebuffer = this.minPrebuffer;
    // Instrumentation. Chrome chops and Edge does not on identical bytes, and
    // every stage before the browser has been measured clean — so this is the
    // one place left with no numbers. Reported once a second rather than per
    // quantum: `process` runs about four hundred times a second and a message
    // per call would itself be the fault being looked for.
    this.stat = {
      underruns: 0, trims: 0, chunks: 0, fed: 0, consumed: 0,
      minDepth: Infinity, maxDepth: 0,
    };
    this.maxBuffer = Math.floor(sampleRate * 0.3);
    this.port.onmessage = (e) => {
      if (e.data && e.data.cmd === "flush") {
        this.r = this.w;
        this.playing = false;
        return;
      }
      const chunk = e.data;
      if (chunk && chunk.length) {
        // A quarter of a chunk of slack before playing, and room for two more
        // behind it before anything is dropped. With 1 s chunks that is 1.25 s
        // of latency, which is right for monitoring a channel and would be
        // wrong for a duplex conversation — this hardware cannot transmit.
        this.prebuffer = Math.max(
          this.minPrebuffer, Math.floor(chunk.length * 1.25),
        );
        // Headroom of a whole lead above the prebuffer, not of two chunks. A
        // burst has to be able to land on top of a full buffer without pushing
        // it over the cap, and under Opus a burst is many chunks rather than
        // one — two of them is 40 ms of slack against 125 ms of arrival, which
        // is what made the trim fire on every burst. For a station that really
        // does send one chunk per burst this is still two chunks, so the case
        // the measurement was written for is unchanged.
        this.maxBuffer =
          this.prebuffer + Math.max(chunk.length * 2, this.minPrebuffer);
        // The ring has to hold the cap with room to spare, or the write
        // pointer laps the read pointer and the audio tears instead of gapping.
        const needed = this.maxBuffer * 2;
        if (needed > this.size) {
          const grown = new Float32Array(needed);
          this.buf = grown;
          this.size = needed;
          this.r = 0;
          this.w = 0;
          this.playing = false;
        }
      }
      for (let i = 0; i < chunk.length; i += 1) {
        this.buf[this.w % this.size] = chunk[i];
        this.w += 1;
      }
      this.stat.chunks += 1;
      this.stat.fed += chunk.length;
      if (this.w - this.r > this.maxBuffer) {
        this.stat.trims += 1;
        this.r = this.w - this.prebuffer;
      }
    };
  }

  process(inputs, outputs) {
    const out = outputs[0][0];
    const depth = this.w - this.r;
    if (depth < this.stat.minDepth) this.stat.minDepth = depth;
    if (depth > this.stat.maxDepth) this.stat.maxDepth = depth;
    this.stat.consumed += out.length;
    if (this.stat.consumed >= sampleRate) {
      const ms = (n) => Math.round((n / sampleRate) * 1000);
      this.port.postMessage({
        stat: {
          underruns: this.stat.underruns,
          trims: this.stat.trims,
          chunks: this.stat.chunks,
          fedMs: ms(this.stat.fed),
          consumedMs: ms(this.stat.consumed),
          minDepthMs: ms(this.stat.minDepth === Infinity ? 0 : this.stat.minDepth),
          maxDepthMs: ms(this.stat.maxDepth),
          prebufferMs: ms(this.prebuffer),
          capMs: ms(this.maxBuffer),
          playing: this.playing,
        },
      });
      this.stat.underruns = 0;
      this.stat.trims = 0;
      this.stat.chunks = 0;
      this.stat.fed = 0;
      this.stat.consumed = 0;
      this.stat.minDepth = Infinity;
      this.stat.maxDepth = 0;
    }

    if (!this.playing && depth < this.prebuffer) {
      out.fill(0);
      return true;
    }
    this.playing = true;
    for (let i = 0; i < out.length; i += 1) {
      if (this.w > this.r) {
        out[i] = this.buf[this.r % this.size];
        this.r += 1;
      } else {
        out[i] = 0;
        if (this.playing) this.stat.underruns += 1;
        this.playing = false; // underrun: re-buffer before resuming
      }
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
