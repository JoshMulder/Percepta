/**
 * A short alert tone for a critical event, so an operator not looking at the
 * screen still gets told.
 *
 * Synthesised with WebAudio rather than bundled as an audio file: it is two
 * sine beeps, and a few lines that generate them are smaller and more honest
 * than shipping and decoding a clip. One lazily-made AudioContext for the tab —
 * browsers permit an AudioContext to resume after any earlier user gesture, and
 * an operator has clicked their way into this console long before an aircraft
 * gets close and low. If audio is unavailable (no context, autoplay still
 * locked), it fails silent: the visual alert and the drawer stand on their own.
 */
let ctx: AudioContext | null = null;

export function chime(): void {
  try {
    ctx =
      ctx ??
      new (window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext)();
    if (ctx.state === "suspended") void ctx.resume();
    const start = ctx.currentTime;
    // Two short descending beeps — an alert cadence, not a soft notification
    // ping, and brief enough not to step on the airband audio.
    [880, 660].forEach((freq, i) => {
      const osc = ctx!.createOscillator();
      const gain = ctx!.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const at = start + i * 0.18;
      // Attack and decay through the gain node, not the oscillator, so it does
      // not click on start/stop.
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.18, at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.16);
      osc.connect(gain).connect(ctx!.destination);
      osc.start(at);
      osc.stop(at + 0.18);
    });
  } catch {
    /* no audio available; the visual alert is the fallback */
  }
}
