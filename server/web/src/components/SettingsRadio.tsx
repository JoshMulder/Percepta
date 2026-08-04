import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Capability, RadioPayload } from "../types";
import { Spectrum } from "./Spectrum";

/**
 * Receiver setup, squelch, and the signal meter.
 *
 * These moved off the front panel to give the sidebar back its vertical space.
 * The split is by how often a thing is touched, not by how important it is: the
 * frequency, the presets and the volume are handled constantly, while the
 * squelch threshold and the tuner gain are set at commissioning and then left
 * for months.
 *
 * The signal meter came with the squelch deliberately. It exists to let someone
 * *choose* a threshold — you set it by looking at where the noise sits and where
 * traffic peaks — so separating the two would leave a slider with nothing to aim
 * at. What stays on the front panel is the channel-open light, which is the only
 * part an operator reads at a glance.
 *
 * Pinned to the station the console is watching rather than the settings
 * station selector, because everything here is live telemetry and that only
 * exists for the station currently subscribed.
 */

// Meter span: floor around -90 dBFS, saturation near -10. Identical to the
// scale the front panel used, so the bar an operator learned to read there
// means the same thing here.
const pct = (db: number) => Math.max(0, Math.min(100, ((db + 90) / 80) * 100));

/** Airband channel spacing. Click-to-tune snaps to it, as Remote-Radio does,
 *  so a click lands on a channel rather than between two. */
const STEP_HZ = 25_000;

/** How often the console re-asks for the spectrum. Shorter than the station's
 *  window so an open page never gaps, and the request stops the moment this
 *  component unmounts — a page nobody has open costs nothing. */
const SPECTRUM_REFRESH_MS = 6_000;

/** How long an optimistic squelch control waits for the station to confirm
 *  before giving up and showing telemetry again. Longer than the radio
 *  stream's cadence by enough to survive a dropped frame; short enough that a
 *  command which never landed does not sit on screen looking like it did. */
const SETTLE_MS = 5_000;

function has(caps: Capability[], c: Capability): boolean {
  return caps.includes(c);
}

export function SettingsRadio({
  radio,
  caps,
  stationId,
  stationName,
}: {
  radio: RadioPayload | null;
  caps: Capability[];
  stationId: string | null;
  stationName: string | null;
}) {
  const [error, setError] = useState<string | null>(null);

  /**
   * Optimistic state for the two squelch controls, and why they need it when
   * nothing else on this pane does.
   *
   * "No optimistic update" was right for gain and tune — you set them, the
   * station reports back within a second, and nothing was moving in between.
   * It was wrong for these two, and both of the ways it was wrong read as
   * "auto squelch is broken":
   *
   *   The AUTO button had up to a second of dead time after a click, because
   *   its label came only from telemetry. An operator clicks, sees nothing
   *   happen, clicks again — and the second click undoes the first, so AUTO
   *   ends up back where it started and the button looks like it does nothing.
   *
   *   The threshold slider was driven straight from `threshold_db`, which is
   *   exactly the value AUTO moves: it rides the noise floor, so it changes by
   *   a decibel or so on every frame. Grabbing the handle meant fighting a
   *   control that was being yanked out from under the pointer once a second.
   *
   * This is the pattern the front panel already used before these controls
   * moved here; it did not come with them. The station stays authoritative —
   * the pending value is dropped the moment telemetry agrees, or after
   * SETTLE_MS if a command never lands, so a failed command corrects itself on
   * screen rather than leaving a wrong state showing.
   */
  const [pendingAuto, setPendingAuto] = useState<boolean | null>(null);
  const [pendingSquelch, setPendingSquelch] = useState<number | null>(null);
  const autoSettle = useRef<number | null>(null);
  const squelchSettle = useRef<number | null>(null);
  //: The threshold last actually sent, so the several gestures that end a drag
  //: produce one command between them. See `commitSquelch`.
  const sentSquelch = useRef<number | null>(null);

  const send = (what: string, run: () => Promise<unknown>) => {
    if (!stationId) return;
    setError(null);
    run().catch(() => setError(`${what} failed — the station did not accept it.`));
  };
  const onGain = (g: string | number) =>
    send("Gain", () => api.setGain(stationId!, g));
  const canControl = has(caps, "radio.control");

  const onAutoSquelch = (on: boolean) => {
    setPendingAuto(on);
    if (autoSettle.current) window.clearTimeout(autoSettle.current);
    autoSettle.current = window.setTimeout(() => setPendingAuto(null), SETTLE_MS);
    send("Auto squelch", () => api.autoSquelch(stationId!, on));
  };

  /**
   * Sent when the drag ends, not on every pointer move.
   *
   * `onChange` fires per pixel, and each one was an audited command dispatched
   * over the link — dozens of them for one drag of a control that is set at
   * commissioning and then left. One command, on commit.
   *
   * Idempotent, because four different gestures end a drag and more than one
   * can fire for the same one: a pointer release is followed by a blur, and
   * re-sending the value already sent would put a second command on the link
   * and a second row in the audit log for one adjustment.
   */
  const commitSquelch = () => {
    if (pendingSquelch === null || pendingSquelch === sentSquelch.current) return;
    sentSquelch.current = pendingSquelch;
    // The station drops out of AUTO when a threshold is set by hand, which is
    // deliberate — moving the slider is how you leave AUTO. Shown here at the
    // same moment so the button does not claim AUTO is still on for the second
    // before telemetry says otherwise.
    setPendingAuto(false);
    if (autoSettle.current) window.clearTimeout(autoSettle.current);
    autoSettle.current = window.setTimeout(() => setPendingAuto(null), SETTLE_MS);
    if (squelchSettle.current) window.clearTimeout(squelchSettle.current);
    squelchSettle.current = window.setTimeout(() => setPendingSquelch(null), SETTLE_MS);
    send("Squelch", () => api.squelch(stationId!, pendingSquelch));
  };

  useEffect(() => {
    if (pendingAuto !== null && radio && radio.auto_squelch === pendingAuto) {
      setPendingAuto(null);
    }
  }, [radio, pendingAuto]);

  /**
   * Hand the slider back to telemetry once the station reports what was sent.
   *
   * Measured against the value **sent**, never against the value on screen.
   * Comparing the displayed value with telemetry looks equivalent and is not:
   * dragging to where the station already is — which is the obvious gesture for
   * pinning AUTO's current threshold as a manual one — would match on the first
   * frame, clear the pending value mid-drag, and the commit would then find
   * nothing to send. The command would never leave the console and AUTO would
   * stay on, with the slider sitting innocently at the value the operator
   * chose.
   *
   * So nothing is cleared unless a command is outstanding, and the displayed
   * value is only surrendered if it is still the one that was sent — an
   * operator who has started a fresh drag keeps their handle.
   */
  useEffect(() => {
    const sent = sentSquelch.current;
    if (sent === null || !radio) return;
    // Not equality: the station rounds the threshold to a tenth of a dB.
    if (Math.abs(radio.threshold_db - sent) >= 0.6) return;
    sentSquelch.current = null;
    setPendingSquelch((current) => (current === sent ? null : current));
  }, [radio]);

  useEffect(
    () => () => {
      if (autoSettle.current) window.clearTimeout(autoSettle.current);
      if (squelchSettle.current) window.clearTimeout(squelchSettle.current);
    },
    [],
  );

  /* Ask for the spectrum while this page is open, and only while it is.
     Re-asked rather than held open by a connection: the station's window
     lapses on its own, so a crashed console or a closed lid stops the traffic
     without needing to say goodbye. The array is around 150 MB a day at the
     radio stream's rate, on a link that is metered and shared with video. */
  useEffect(() => {
    if (!stationId) return;
    let live = true;
    const ask = (on: boolean) => {
      if (!live && on) return;
      api.wantSpectrum(stationId, on).catch(() => {});
    };
    ask(true);
    const timer = window.setInterval(() => ask(true), SPECTRUM_REFRESH_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
      // Best effort. If it does not arrive the window lapses anyway.
      ask(false);
    };
  }, [stationId]);
  const canConfigure = has(caps, "config.write");
  const auto = pendingAuto ?? radio?.auto_squelch ?? true;
  const threshold = pendingSquelch ?? radio?.threshold_db ?? -70;
  const gains = radio?.gains ?? [];
  const gain = radio?.gain ?? "auto";

  if (!radio) {
    return (
      <p className="settings-note">
        No radio telemetry from {stationName ?? "this station"} yet. These
        controls appear once the receiver reports.
      </p>
    );
  }

  return (
    <div className="settings-sections">
      <section className="settings-section">
        <h3>Spectrum</h3>
        <Spectrum
          radio={radio}
          onTune={
            canControl
              ? (hz) =>
                  send("Tune", () =>
                    api.tune(stationId!, Math.round(hz / STEP_HZ) * STEP_HZ),
                  )
              : undefined
          }
        />
      </section>

      <section className="settings-section">
        <h3>Squelch</h3>

        <div className="radio-readout">
          <span>
            Signal <b>{radio.rssi_db.toFixed(0)} dB</b>
          </span>
          <span>
            Noise floor <b>{radio.noise_floor_db.toFixed(0)} dB</b>
          </span>
          <span>
            Threshold <b>{threshold.toFixed(0)} dB</b>
          </span>
          {/* Same one colour as the front panel's. See RadioPanel. */}
          <span
            className={`led${radio.squelch_open ? " on" : ""}`}
            title={
              radio.monitor
                ? "Monitor — squelch held open"
                : radio.squelch_open
                  ? "Channel open"
                  : "Squelched"
            }
          />
        </div>

        <div className="meter settings-meter">
          <div
            className={`meter-fill${radio.squelch_open ? " open" : ""}`}
            style={{ width: `${pct(radio.rssi_db)}%` }}
          />
          <div className="meter-floor" style={{ left: `${pct(radio.noise_floor_db)}%` }} />
          <input
            className="squelch-overlay"
            type="range"
            min={-110}
            max={-10}
            step={1}
            value={threshold}
            // Not disabled while auto is on: moving it is how you leave auto.
            // A slider that needs a separate button unlocked first is a slider
            // people fight.
            disabled={!canControl}
            // The handle moves locally; the station hears about it on commit.
            // Held here for the whole drag, so AUTO riding the noise floor
            // cannot pull the value out from under the pointer.
            onChange={(e) => setPendingSquelch(Number(e.target.value))}
            onPointerUp={commitSquelch}
            onPointerCancel={commitSquelch}
            // Keyboard sets it one arrow-press at a time; each keyup is a
            // finished gesture in the same way a pointer release is.
            onKeyUp={commitSquelch}
            onBlur={commitSquelch}
            title="Drag to set the squelch threshold (leaves AUTO)"
            aria-label="Squelch threshold"
          />
        </div>

        <div className="settings-actions">
          <button
            type="button"
            className={`btn ghost${auto ? " active" : ""}`}
            disabled={!canControl}
            onClick={() => onAutoSquelch(!auto)}
          >
            {auto ? "Auto squelch: on" : "Auto squelch: off"}
          </button>
        </div>
        {error && <p className="settings-error">{error}</p>}
        <small>
          Auto rides the threshold a few dB above the measured noise floor, which
          is what keeps a receiver usable as conditions change. A fixed threshold
          drifts out of usefulness as the floor moves — set one only if you have
          a reason to.
          {!canControl && " Changing this needs radio.control."}
        </small>
      </section>

      {canConfigure && (
        <section className="settings-section">
          <h3>Receiver setup</h3>
          <label className="field">
            <span>RF gain</span>
            <select value={String(gain)} onChange={(e) => onGain(e.target.value)}>
              <option value="auto">Auto</option>
              <option value="managed">Managed</option>
              {gains.map((g) => (
                <option key={g} value={g}>
                  {g.toFixed(1)} dB
                </option>
              ))}
            </select>
            <small>
              Auto hands the gain to the tuner's own AGC, which desenses near a
              strong transmitter — a stronger signal can then read <em>lower</em>
              {" "}on the meter. <strong>Managed</strong> instead holds the
              highest <em>fixed</em> gain that does not overload and nudges it
              slowly, so it adapts without the AGC's blowout; a plain fixed gain
              is the other choice.
              {gain === "managed" &&
                typeof radio.managed_gain_db === "number" && (
                  <> Currently <strong>{radio.managed_gain_db.toFixed(1)} dB</strong>.</>
                )}
            </small>
          </label>

          {/* No crystal correction here. It is trimmed once at commissioning
              from the receiver's own settings on the station, and a value that
              can be retyped from a console — by somebody who is not at the
              site and cannot hear the result — is one that will be wrong
              without anybody noticing. */}
        </section>
      )}
    </div>
  );
}
