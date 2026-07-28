import type { CSSProperties } from "react";
import { useEffect, useRef, useState } from "react";
import { memo } from "react";
import { api } from "../api";
import type { Capability, RadioPayload } from "../types";
import { IconSpeaker } from "./Icons";
import { has, NotPermitted } from "./Panels";

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

// Meter span: floor around -90 dBFS, saturation near -10.
const pct = (db: number) => Math.max(0, Math.min(100, ((db + 90) / 80) * 100));

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

  const [presets, setPresets] = useState<(Preset | null)[]>(() =>
    loadPresets(stationId),
  );
  const [editing, setEditing] = useState<number | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [typing, setTyping] = useState<string | null>(null);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  if (!canListen) return <NotPermitted what="the radio" />;

  const hz = pending ?? radio?.freq_hz ?? 0;
  const auto = pendingAuto ?? radio?.auto_squelch ?? false;
  const threshold =
    pendingSquelch ?? (radio ? Math.round(radio.threshold_db) : -40);
  // Silent covers both reasons there is no sound: the operator muted it, or the
  // browser has not let us start. They look the same and are fixed the same way.
  const silent = muted || audioState === "blocked";
  const gain = radio?.gain ?? "auto";
  const gains = radio?.gains ?? [];
  const ppm = radio?.ppm ?? 0;
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
    // At most six digits; the sixth completes the frequency and tunes, which is
    // what makes "118700" a single fluent action rather than type-then-Enter.
    let next = value;
    while (digitsOf(next).length > 6) next = next.slice(0, -1);
    setTyping(next);
    if (digitsOf(next).length === 6) commitTyped(next);
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

  const setSquelch = (db: number) => {
    if (!canControl) return;
    // Hold the dragged value locally so the handle tracks the pointer, and let
    // the station's own report take over once it agrees.
    setPendingSquelch(db);
    // Moving the slider leaves auto - that is what the operator is saying by
    // moving it. Reflected immediately so the AUTO chip goes out under the
    // finger rather than a second later.
    if (auto) setPendingAuto(false);
    setError(null);
    api.squelch(stationId, db).catch(() => {
      setPendingSquelch(null);
      setPendingAuto(null);
      setError("Squelch change failed");
    });
    if (squelchSettle.current) window.clearTimeout(squelchSettle.current);
    squelchSettle.current = window.setTimeout(() => setPendingSquelch(null), 4000);
  };

  const setGain = (value: string) => {
    const next = value === "auto" ? "auto" : Number(value);
    api.setGain(stationId, next).catch(() => setError("Gain change failed"));
  };

  const setPpm = (value: number) => {
    api.setPpm(stationId, value).catch(() => setError("Correction change failed"));
  };

  const toggleAuto = () => {
    if (!canControl) return;
    const next = !auto;
    setPendingAuto(next);
    setError(null);
    api.autoSquelch(stationId, next).catch(() => {
      setPendingAuto(null);
      setError("Auto squelch change failed");
    });
    if (autoSettle.current) window.clearTimeout(autoSettle.current);
    autoSettle.current = window.setTimeout(() => setPendingAuto(null), 5000);
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
      <div className="freq-row">
        <div className="step-col">
          <button type="button" className="step" title="+1 MHz"
                  disabled={!canControl} onClick={() => step(1e6)}>▲</button>
          <button type="button" className="step" title="−1 MHz"
                  disabled={!canControl} onClick={() => step(-1e6)}>▼</button>
        </div>

        <div className="freq-display">
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

      <div className="meter-label">
        <span>SIGNAL</span>
        <span className="rssi">{radio ? `${radio.rssi_db.toFixed(0)} dB` : "--"}</span>
        <span className="floor">
          SQL <b>{radio ? `${threshold.toFixed(0)} dB` : "--"}</b>
        </span>
        <button
          type="button"
          className={`chip toggle${auto ? " on" : ""}`}
          disabled={!canControl}
          onClick={toggleAuto}
          title={
            auto
              ? "Riding the noise floor — click for a fixed threshold"
              : "Ride the squelch automatically above the noise floor"
          }
        >
          AUTO
        </button>
        <span
          className={`led${radio?.squelch_open ? " on" : ""}${
            radio?.monitor ? " monitor" : ""
          }`}
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

      <div className="meter">
        <div
          className={`meter-fill${radio?.squelch_open ? " open" : ""}`}
          style={{ width: `${radio ? pct(radio.rssi_db) : 0}%` }}
        />
        {radio && (
          <div className="meter-floor" style={{ left: `${pct(radio.noise_floor_db)}%` }} />
        )}
        <input
          className="squelch-overlay"
          type="range"
          min={-110}
          max={-10}
          step={1}
          value={threshold}
          // Deliberately NOT disabled while auto is on: moving it is how an
          // operator leaves auto, and a slider they have to first find a
          // separate button to unlock is a slider they will fight.
          disabled={!canControl}
          onChange={(e) => setSquelch(Number(e.target.value))}
          title="Drag to set the squelch threshold (leaves AUTO)"
          aria-label="Squelch threshold"
        />
      </div>

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

      {has(caps, "config.write") && (
        <details className="radio-config">
          <summary>Receiver setup</summary>
          <label className="cfg-row">
            <span>RF gain</span>
            <select value={String(gain)} onChange={(e) => setGain(e.target.value)}>
              <option value="auto">Auto</option>
              {gains.map((g) => (
                <option key={g} value={g}>
                  {g.toFixed(1)} dB
                </option>
              ))}
            </select>
          </label>
          <p className="hint">
            Auto desenses the tuner near a strong transmitter — a stronger
            signal can then read <em>lower</em> on the meter.
          </p>
          {/* Correction stays - a fitted receiver still has a crystal error
              worth trimming once at commissioning. The calibrate routine that
              solved it from a test transmission does not: it existed to work
              around a cheap SDR, and a certified receiver does not need
              someone standing there keying a carrier. */}
          <label className="cfg-row">
            <span>Correction</span>
            <span className="ppm-row">
              <input
                type="number"
                min={-1000}
                max={1000}
                step={1}
                value={ppm}
                onChange={(e) => setPpm(Number(e.target.value))}
              />
              <span className="unit">ppm</span>
            </span>
          </label>
        </details>
      )}

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
