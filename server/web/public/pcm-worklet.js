// Ring-buffer PCM player, ported from Remote-Radio's client.
//
// The main thread posts Float32Array chunks at the context sample rate; this
// prebuffers ~90 ms, then plays, emitting silence and re-buffering on underrun.
// Backlog beyond maxBuffer is dropped, so a network hiccup cannot permanently
// add latency — which matters more here than there: these consoles sit behind
// Starlink, where a brief stall is normal and audio that drifts a second behind
// stays a second behind for the rest of the shift.
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = sampleRate * 2; // 2 s ring
    this.buf = new Float32Array(this.size);
    this.r = 0;
    this.w = 0;
    this.playing = false;
    this.prebuffer = Math.floor(sampleRate * 0.09);
    this.maxBuffer = Math.floor(sampleRate * 0.3); // latency cap
    this.port.onmessage = (e) => {
      if (e.data && e.data.cmd === "flush") {
        this.r = this.w;
        this.playing = false;
        return;
      }
      const chunk = e.data;
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
