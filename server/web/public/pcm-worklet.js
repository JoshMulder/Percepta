// Ring-buffer PCM player, ported from Remote-Radio's client.
//
// The main thread posts Float32Array chunks at the context sample rate; this
// prebuffers, then plays, emitting silence and re-buffering on underrun.
// Backlog beyond maxBuffer is dropped, so a network hiccup cannot permanently
// add latency — which matters more here than there: these consoles sit behind
// Starlink, where a brief stall is normal and audio that drifts a second behind
// stays a second behind for the rest of the shift.
//
// **Both thresholds are sized from the chunks that actually arrive**, and that
// is the fix for a year of choppy audio. The ported constants — 90 ms of
// prebuffer, a 300 ms latency cap — were right for Remote-Radio, which sent
// small chunks many times a second over a LAN. This station demodulates one
// second of audio per tick and sends it at 1 Hz, so every single chunk
// overflowed the 300 ms cap the instant it landed and was truncated to the
// last 90 ms: nine-tenths of every second discarded before it could play.
// Hardcoding a bigger number would fix this station and break the next one, so
// the player measures instead.
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = sampleRate * 2; // 2 s ring
    this.buf = new Float32Array(this.size);
    this.r = 0;
    this.w = 0;
    this.playing = false;
    // Floors, until a chunk says otherwise. Anything smaller than a chunk is
    // unusable; anything much larger is latency nobody asked for.
    this.minPrebuffer = Math.floor(sampleRate * 0.09);
    this.prebuffer = this.minPrebuffer;
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
        this.maxBuffer = this.prebuffer + chunk.length * 2;
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
      if (this.w - this.r > this.maxBuffer) this.r = this.w - this.prebuffer;
    };
  }

  process(inputs, outputs) {
    const out = outputs[0][0];
    if (!this.playing && this.w - this.r < this.prebuffer) {
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
        this.playing = false; // underrun: re-buffer before resuming
      }
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
