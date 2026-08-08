import type { CSSProperties } from "react";
import { useEffect, useRef, useState } from "react";
import { memo } from "react";
import { api } from "../api";
import type { Capability, RadioPayload } from "../types";
import { IconSpeaker, IconTranscript } from "./Icons";
import { NotPermitted } from "./Panels";
import { Popout } from "./Popout";

interface Transcript {
  t: string;
  clock: string;
  message: string;
}

/**
 * Airband receiver controls, ported from Remote-Radio's own client so an
 * operator moving between the two is not relearning the radio.
 *
 * Carried across unchanged: the stepper columns either side of an editable
 * frequency (±1 MHz outer, ±25 kHz inner - NZ airband is 25 kHz spaced), the
 * six-digit typing behaviour, the squelch slider riding directly on the signal
 * meter, the AUTO chip, and the squelch LED.
 *
 * Not carried across: the spectrum display. It was dropped as unnecessary here,
 * and it was actively harmful - a canvas that re-sized itself on every 1 Hz
 * telemetry frame kept re-triggering the sidebar's fit measurement.
 *
 * RF gain and ppm correction sit behind config.write in a collapsed "receiver
 * setup" section: they are set once per site from its RF environment, not
 * adjusted while watching it, and getting gain wrong quietly degrades every
 * listener rather than producing an obvious symptom.
 *
 * Not carried across: the calibrate routine, which solved ppm from a test
 * transmission. It was a workaround for a cheap dongle's crystal error, and a
 * fitted receiver does not need someone standing at the site keying a carrier.
 * Audio playback is still absent - there is no audio pipeline yet.
 */

const MIN_HZ = 108_000_000;
const MAX_HZ = 137_000_000;
const CHANNEL_HZ = 25_000;
// Four, not five. Wider slots hold a readable name at the sizes this panel is
// actually rendered at; five made every name an ellipsis.
//
// Note that a stored fifth preset is dropped on load rather than migrated -
// acceptable while presets live in localStorage and are per-browser, but worth
// remembering if they ever move server-side.
const PRESET_SLOTS = 4;

/** How often the open transcription log re-fetches, so new transmissions appear
 *  without the operator closing and re-opening it. Cheap — it reads the event
 *  table, not a downsample — and only runs while the popout is open. */
const TRANSCRIPT_REFRESH_MS = 4_000;

/**
 * Remote-Radio's parser, verbatim in behaviour.
 *
 * "128950" with no separator means 128.950 MHz - the first three digits are the
 * whole-MHz part. That is how a frequency is read aloud and how an operator
 * types it, and it is the reason the box takes six digits and tunes itself on
 * the sixth. "118.7" and "118" parse normally.
 */
function parseFreqMhz(text: string): number {
  const trimmed = text.trim();
  if (/^\d{4,}$/.test(trimmed)) {
    return Number.parseFloat(`${trimmed.slice(0, 3)}.${trimmed.slice(3)}`);
  }
  return Number.parseFloat(trimmed);
}

const digitsOf = (text: string) => text.replace(/\D/g, "");

/** What the frequency box shows as it is typed: airband is always xxx.xxx, so
 *  the point is inserted after the third digit rather than being something the
 *  operator has to key. At most six digits; the sixth completes the frequency.
 *  "128950" and "128.950" and "128.9" all format to the same running value. */
export function formatFreqEntry(raw: string): string {
  const digits = digitsOf(raw).slice(0, 6);
  return digits.length > 3 ? `${digits.slice(0, 3)}.${digits.slice(3)}` : digits;
}

interface Preset {
  hz: number;
  name: string;
}

function presetKey(stationId: string) {
  return `percepta.radio.presets.${stationId}`;
}

function loadPresets(stationId: string): (Preset | null)[] {
  const empty = (): (Preset | null)[] => Array(PRESET_SLOTS).fill(null);
  try {
    const raw = localStorage.getItem(presetKey(stationId));
    if (!raw) return empty();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return empty();
    return Array.from({ length: PRESET_SLOTS }, (_, i) => {
      const value = parsed[i];
      // Tolerate the earlier bare-number format rather than discarding presets
      // someone has already set.
      if (typeof value === "number") return { hz: value, name: "" };
      if (value && typeof value.hz === "number") {
        return { hz: value.hz, name: typeof value.name === "string" ? value.name : "" };
      }
      return null;
    });
  } catch {
    return empty();
  }
}

function RadioPanelInner({
  stationId,
  radio,
  caps,
  onVolume,
  onUnmute,
  onRetune,
  audioState,
}: {
  stationId: string;
  radio: RadioPayload | null;
  caps: Capability[];
  onVolume?: (v: number) => void;
  /** Called when the operator moves the volume control, which is the only
   *  thing that starts audio - see useAudio. */
  onUnmute?: () => void;
  /** Called when the operator retunes, so queued audio for the old channel is
   *  dropped rather than played out after the move. */
  onRetune?: () => void;
  audioState?: "off" | "blocked" | "playing" | "unsupported";
}) {
  const canListen = caps.includes("radio.listen");
  const canControl = caps.includes("radio.control");
  // No gain control here. It belongs with the receiver's other setup — it is
  // set once for a site from its RF environment, not adjusted while listening,
  // and the station's own radio settings already carry it. Two places to set
  // one value is two places for it to disagree, and this is the one an
  // operator has open while doing something else.

  const [presets, setPresets] = useState<(Preset | null)[]>(() =>
    loadPresets(stationId),
  );
  const [editing, setEditing] = useState<number | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [typing, setTyping] = useState<string | null>(null);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [transcriptsOpen, setTranscriptsOpen] = useState(false);
  const [transcripts, setTranscripts] = useState<Transcript[]>([]);
  const [transcriptsLoading, setTranscriptsLoading] = useState(false);

  /**
   * Optimistic frequency, exactly as Remote-Radio does it: stepping updates
   * locally so rapid clicks accumulate instead of each one starting from a
   * stale value. The station stays authoritative - the next telemetry frame
   * overwrites this - so a command that never lands corrects itself within a
   * second rather than leaving a wrong number on screen.
   */
  const [pending, setPending] = useState<number | null>(null);
  // Same pattern for AUTO: show the click immediately, but let the station's
  // own report win as soon as it arrives. Previously this was pure local state,
  // so the button lit up whether or not the station had done anything.
  const [pendingAuto, setPendingAuto] = useState<boolean | null>(null);
  /**
   * Squelch is dragged, so it needs a local value while the pointer is down.
   * Driving the slider straight from telemetry made it stutter: the station
   * reports once a second, so every frame yanked the handle back to where the
   * radio was rather than leaving it where the finger is.
   */
  const [pendingSquelch, setPendingSquelch] = useState<number | null>(null);
  const squelchSettle = useRef<number | null>(null);
  const settle = useRef<number | null>(null);
  const autoSettle = useRef<number | null>(null);
  const freqInput = useRef<HTMLInputElement>(null);

  useEffect(() => setPresets(loadPresets(stationId)), [stationId]);

  // Once the station reports the frequency we asked for, stop overriding it.
  useEffect(() => {
    if (pending !== null && radio && radio.freq_hz === pending) setPending(null);
  }, [radio, pending]);

  useEffect(() => {
    if (pendingAuto !== null && radio && radio.auto_squelch === pendingAuto) {
      setPendingAuto(null);
    }
  }, [radio, pendingAuto]);

  useEffect(() => {
    if (
      pendingSquelch !== null &&
      radio &&
      Math.abs(radio.threshold_db - pendingSquelch) < 0.6
    ) {
      setPendingSquelch(null);
    }
  }, [radio, pendingSquelch]);

  useEffect(
    () => () => {
      if (settle.current) window.clearTimeout(settle.current);
      if (autoSettle.current) window.clearTimeout(autoSettle.current);
      if (squelchSettle.current) window.clearTimeout(squelchSettle.current);
    },
    [],
  );

  // The transcription log, kept live while the popout is open so a new
  // transmission appears without closing and re-opening it — which is what an
  // operator watching a channel actually wants. Only polls while open, and only
  // the first fetch shows the loader or clears to empty; a failed refresh keeps
  // what is on screen. Empty unless the station has on-box transcription on.
  useEffect(() => {
    if (!transcriptsOpen) return;
    let cancelled = false;
    const load = (initial: boolean) => {
      if (initial) setTranscriptsLoading(true);
      api
        .radioTranscripts(stationId)
        .then((rows) => !cancelled && setTranscripts(rows))
        .catch(() => initial && !cancelled && setTranscripts([]))
        .finally(() => initial && !cancelled && setTranscriptsLoading(false));
    };
    load(true);
    const timer = window.setInterval(() => load(false), TRANSCRIPT_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [transcriptsOpen, stationId]);

  if (!canListen) return <NotPermitted what="the radio" />;

  const hz = pending ?? radio?.freq_hz ?? 0;
  // Silent covers both reasons there is no sound: the operator muted it, or the
  // browser has not let us start. They look the same and are fixed the same way.
  const silent = muted || audioState === "blocked";
  const mhz = radio || pending ? (hz / 1e6).toFixed(3) : "---.---";

  const tune = (raw: number) => {
    if (!canControl) return;
    // Clamp to the band and snap to the nearest kHz before sending, as
    // Remote-Radio does, so the server and the radio agree on the value.
    const next = Math.round(Math.max(MIN_HZ, Math.min(MAX_HZ, raw)) / 1000) * 1000;
    if (next === hz) return;
    setPending(next);
    setError(null);
    onRetune?.();
    api.tune(stationId, next).catch(() => {
      setError("Station did not accept the command");
      setPending(null);
    });
    // Give up on the optimistic value if telemetry never confirms it.
    if (settle.current) window.clearTimeout(settle.current);
    settle.current = window.setTimeout(() => setPending(null), 5000);
  };

  const step = (delta: number) => tune(hz + delta);

  const commitTyped = (text: string) => {
    const parsed = parseFreqMhz(text);
    if (Number.isFinite(parsed)) tune(parsed * 1e6);
    setTyping(null);
    freqInput.current?.blur();
  };

  const onType = (value: string) => {
    // The point goes in after the third digit as they type, so "128950" shows
    // as "128.950" — the airband format — and the sixth digit tunes, one fluent
    // action rather than type-then-Enter.
    const shown = formatFreqEntry(value);
    setTyping(shown);
    if (digitsOf(shown).length === 6) commitTyped(shown);
  };

  const persist = (next: (Preset | null)[]) => {
    setPresets(next);
    try {
      localStorage.setItem(presetKey(stationId), JSON.stringify(next));
    } catch {
      /* storage unavailable; the preset just does not persist */
    }
  };

  const savePreset = (index: number) => {
    if (!radio && pending === null) return;
    const next = [...presets];
    next[index] = { hz, name: "" };
    persist(next);
    // Storing and naming are one action from the operator's point of view.
    setNameDraft("");
    setEditing(index);
  };

  const renamePreset = (index: number, name: string) => {
    const preset = presets[index];
    if (!preset) return;
    const next = [...presets];
    next[index] = { ...preset, name: name.trim().slice(0, 12) };
    persist(next);
    setEditing(null);
  };

  const clearPreset = (index: number) => {
    const next = [...presets];
    next[index] = null;
    persist(next);
    setEditing(null);
  };

  /**
   * Hold the squelch open while the volume slider is being dragged.
   *
   * Setting a level against silence is guesswork; a real radio's monitor button
   * exists so you can hear the channel's noise while you do it. Held only for
   * the duration of the drag, and released on pointer-up or if the pointer
   * leaves - a monitor left latched on would sit there hissing at every other
   * listener on the station.
   */
  const holdMonitor = (on: boolean) => {
    if (!canControl) return;
    api.monitor(stationId, on).catch(() => {});
  };





  // Priority: a failed command first, then what this user cannot do, then why
  // there is no sound. Only one can be acted on at a time.
  const [statusText, statusKind] = error
    ? [error, "error"]
    : !canControl
      ? ["Listening only — tuning needs radio.control", "muted"]
      : audioState === "unsupported"
        ? ["Audio unavailable in this browser", "muted"]
        : ["", "muted"];

  return (
    <div className="radio">
      {/* The transcription log, behind a button in the corner like the graph
          popouts. */}
      <button
        type="button"
        className="power-detail-btn"
        onClick={() => setTranscriptsOpen(true)}
        title="Transcription history"
        aria-label="Transcription history"
      >
        <IconTranscript />
      </button>

      <div className="freq-row">
        <div className="step-col">
          <button type="button" className="step" title="+1 MHz"
                  disabled={!canControl} onClick={() => step(1e6)}>▲</button>
          <button type="button" className="step" title="−1 MHz"
                  disabled={!canControl} onClick={() => step(-1e6)}>▼</button>
        </div>

        {/* The whole box is the target, not just the digits. The input is
            narrower than the panel it sits in and the "MHz" beside it looked
            like part of the same control — clicking there, or on the padding,
            did nothing. A div rather than a <label> because the unit is not a
            name for the field, and a label wrapping it would be read out as
            one. */}
        <div
          className="freq-display"
          onMouseDown={(e) => {
            if (!canControl) return;
            if (e.target === freqInput.current) return;
            // Before focus moves anywhere else, so the click does not land on
            // the div and then blur straight back out of the input.
            e.preventDefault();
            freqInput.current?.focus();
          }}
        >
          <input
            ref={freqInput}
            className="freq-input"
            inputMode="decimal"
            spellCheck={false}
            disabled={!canControl}
            value={typing ?? mhz}
            onChange={(e) => onType(e.target.value)}
            // Focus selects all, so typing replaces rather than appends.
            onFocus={(e) => e.target.select()}
            onBlur={() => setTyping(null)}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitTyped(typing ?? mhz);
              if (e.key === "Escape") {
                setTyping(null);
                freqInput.current?.blur();
              }
            }}
            aria-label="Frequency in MHz"
          />
          <span className="unit">MHz</span>
        </div>

        <div className="step-col">
          <button type="button" className="step" title="+25 kHz"
                  disabled={!canControl} onClick={() => step(CHANNEL_HZ)}>▲</button>
          <button type="button" className="step" title="−25 kHz"
                  disabled={!canControl} onClick={() => step(-CHANNEL_HZ)}>▼</button>
        </div>

        {/* Outside the readout rather than inside it. In the box it read as
            part of the frequency; beside it, it is what it is - the state of
            the channel that frequency is tuned to. */}
        {/* One colour, whatever opened the gate.

            This used to go amber while monitor held the squelch open, and
            monitor is held for the whole time the volume slider is being
            dragged - so setting the level turned the light a different colour
            and pointed at a distinction nobody was asking about mid-gesture.
            The light answers one question, "is the channel open", and it is
            open either way. The tooltip still says which. */}
        <span
          className={`led${radio?.squelch_open ? " on" : ""}`}
          title={
            radio?.monitor
              ? "Monitor — squelch held open"
              : radio?.squelch_open
                ? "Channel open"
                : "Squelched"
          }
          aria-label={
            radio?.monitor
              ? "Monitor, squelch held open"
              : radio?.squelch_open
                ? "Channel open"
                : "Squelched"
          }
        />
      </div>

      <div className="presets">
        {presets.map((preset, i) =>
          editing === i ? (
            <input
              key={i}
              className="preset-name-input"
              autoFocus
              maxLength={12}
              placeholder="name"
              value={nameDraft}
              onChange={(e) => setNameDraft(e.target.value)}
              onBlur={() => renamePreset(i, nameDraft)}
              onKeyDown={(e) => {
                if (e.key === "Enter") renamePreset(i, nameDraft);
                if (e.key === "Escape") setEditing(null);
              }}
              aria-label={`Name for preset ${i + 1}`}
            />
          ) : (
            <button
              key={i}
              type="button"
              className={`preset${preset && preset.hz === hz ? " active" : ""}`}
              disabled={!canControl}
              onClick={() => (preset === null ? savePreset(i) : tune(preset.hz))}
              onDoubleClick={() => {
                if (!preset) return;
                setNameDraft(preset.name);
                setEditing(i);
              }}
              onContextMenu={(e) => {
                // Right-click clears, matching Remote-Radio.
                e.preventDefault();
                if (preset !== null) clearPreset(i);
              }}
              title={
                preset === null
                  ? "Empty — click to store the current frequency"
                  : "Click to tune, double-click to rename, right-click to clear"
              }
            >
              {preset === null ? (
                "＋"
              ) : (
                <>
                  <span className="preset-name">
                    {preset.name || (preset.hz / 1e6).toFixed(3)}
                  </span>
                  {preset.name && (
                    <span className="preset-freq">{(preset.hz / 1e6).toFixed(3)}</span>
                  )}
                </>
              )}
            </button>
          ),
        )}
      </div>

      {/* SIGNAL readout, squelch slider and AUTO moved to Settings -> Radio.
          They are set at commissioning and then left for months, and they cost
          three rows of a sidebar whose height sets the scale of the whole
          console. The channel-open light stays, inline with the frequency: it
          is the only part of that block an operator reads at a glance. */}

      {/*
        While the browser is holding audio, the slider shows zero.
        
        That is the whole prompt: a volume control sitting at the bottom reads as
        "this is muted, turn it up", and turning it up *is* the gesture the
        browser is waiting for. No banner, no instruction, and the action the
        operator takes is the one they actually wanted - rather than dismissing a
        notice about browser policy, which is not their problem.
      */}
      {/*
        A mute button rather than a "Volume" label.
        
        The label was a word doing no work: the slider is obviously a volume
        control. A button in its place is both the clearest way to silence the
        radio and - when the browser is holding audio - the gesture that starts
        it. Browser-blocked is rendered as muted because that is exactly what it
        is from the operator's side, and pressing the button fixes it either way.
      */}
      <div className={`volume${silent ? " muted-by-browser" : ""}`}>
        <button
          type="button"
          className={`mute-btn${silent ? " muted" : ""}`}
          onClick={() => {
            // Unmuting is also the user gesture the browser wants, so ask for
            // audio at the same time.
            if (silent) onUnmute?.();
            const nextMuted = !silent;
            setMuted(nextMuted);
            // The slider keeps its position either way; the gain is what moves.
            onVolume?.(nextMuted ? 0 : volume);
          }}
          disabled={audioState === "unsupported"}
          title={
            audioState === "unsupported"
              ? "Audio unavailable in this browser"
              : silent
                ? "Unmute"
                : "Mute"
          }
          aria-pressed={silent}
          aria-label={silent ? "Unmute" : "Mute"}
        >
          <IconSpeaker level={silent ? 0 : volume > 1 ? 2 : 1} />
        </button>
        <input
          type="range"
          min={0}
          max={2}
          step={0.05}
          value={volume}
          // The filled portion is drawn from this: a custom track has no
          // equivalent of the native accent-color fill, so the position has to
          // reach CSS explicitly. Range is 0-2, so halve it for a fraction.
          style={{ ["--vol" as string]: `${(volume / 2) * 100}%` } as CSSProperties}
          disabled={audioState === "unsupported"}
          onPointerDown={() => holdMonitor(true)}
          onPointerUp={() => holdMonitor(false)}
          onPointerCancel={() => holdMonitor(false)}
          onPointerLeave={(e) => {
            // Only if the button is no longer held - leaving mid-drag is normal.
            if (e.buttons === 0) holdMonitor(false);
          }}
          onBlur={() => holdMonitor(false)}
          onKeyDown={() => holdMonitor(true)}
          onKeyUp={() => holdMonitor(false)}
          onChange={(e) => {
            const v = Number(e.target.value);
            setVolume(v);
            // Dragging to zero is muting; dragging off zero is unmuting.
            setMuted(v === 0);
            onVolume?.(silent && v > 0 ? v : v);
            // Moving this off zero is also a request for sound.
            if (v > 0) onUnmute?.();
          }}
          aria-label="Volume"
        />
      </div>

      {/*
        One status line, always present, showing whichever message matters most.

        These were three separate conditional lines - an error, a "listening
        only" note, and the audio state - and each appeared or vanished as
        things resolved, changing the panel's height and rescaling the whole
        sidebar. One reserved line costs a fraction of the space and never
        moves. Anything in this bar has to hold its height in every state.
      */}
      <p className={`radio-status${statusText ? "" : " hidden"} ${statusKind}`}>
        {statusText || "\u00a0"}
      </p>

      {/* Present but permanently disabled: the hardware is receive-only and
          radio.transmit is ungrantable server-side until a certified
          transceiver and operator licensing exist. */}
      <button
        type="button"
        className="btn ptt"
        disabled
        title="Receive-only station — transmit hardware not fitted"
      >
        PTT
      </button>

      {transcriptsOpen && (
        <Popout
          onClose={() => setTranscriptsOpen(false)}
          label="Transcription history"
          className="transcript-detail"
        >
          <div className="power-detail-head">
              <h4>Previous Transmissions</h4>
              <button
                type="button"
                className="contact-close"
                style={{ marginLeft: "auto" }}
                onClick={() => setTranscriptsOpen(false)}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <div className="transcript-list">
              {transcriptsLoading && transcripts.length === 0 ? (
                <p className="muted">loading…</p>
              ) : transcripts.length === 0 ? (
                <p className="muted">
                  No transcriptions yet. They appear here when the station has
                  on-box transcription switched on.
                </p>
              ) : (
                transcripts.map((tr, i) => (
                  <div className="transcript-row" key={`${tr.t}-${i}`}>
                    <time
                      className="transcript-time"
                      title={new Date(tr.t).toLocaleString()}
                    >
                      {new Date(tr.t).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                      {/* A box with no synced clock cannot be trusted on the
                          minute; the tilde says the time is approximate. */}
                      {tr.clock === "unsynced" ? " ~" : ""}
                    </time>
                    <span className="transcript-text">{tr.message}</span>
                  </div>
                ))
              )}
            </div>
        </Popout>
      )}
    </div>
  );
}

/**
 * Memoised. Telemetry arrives on several streams at about 1 Hz each, so the
 * console re-renders a few times a second; without this every panel re-rendered
 * on every frame regardless of whose data it was. The map is the expensive one -
 * reconciling it also re-ran its contact update.
 */
export const RadioPanel = memo(RadioPanelInner);
