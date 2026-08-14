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

/** The last time a chime actually sounded. */
let lastAt = 0;

/**
 * One chime per this window, however many alerts arrive.
 *
 * A comms outage raises a dozen stations at once, and a dozen chimes in three
 * seconds is not twelve times the information — it is a noise an operator
 * silences, after which the one that mattered arrives in silence too. The count
 * is on the screen; the sound only has to say "look".
 */
const MIN_GAP_MS = 10_000;

/** Whether enough time has passed to sound again. Exported for the caller that
 *  wants to know without making a noise to find out. */
export function chimeReady(now = Date.now()): boolean {
  return now - lastAt >= MIN_GAP_MS;
}

export function chime(): void {
  const now = Date.now();
  if (!chimeReady(now)) return;
  lastAt = now;
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
