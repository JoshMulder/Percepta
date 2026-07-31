import { useEffect, useState } from "react";
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

  // No optimistic update. The console's radio panel needs one because an
  // operator is watching the control they just moved; here the station's own
  // telemetry lands within a second and is the honest answer. Nothing on this
  // pane pretends a command succeeded.
  const send = (what: string, run: () => Promise<unknown>) => {
    if (!stationId) return;
    setError(null);
    run().catch(() => setError(`${what} failed — the station did not accept it.`));
  };
  const onSquelch = (db: number) =>
    send("Squelch", () => api.squelch(stationId!, db));
  const onAutoSquelch = (on: boolean) =>
    send("Auto squelch", () => api.autoSquelch(stationId!, on));
  const onGain = (g: string | number) =>
    send("Gain", () => api.setGain(stationId!, g));
  const canControl = has(caps, "radio.control");

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
  const auto = radio?.auto_squelch ?? true;
  const threshold = radio?.threshold_db ?? -70;
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
          <span
            className={`led${radio.squelch_open ? " on" : ""}${radio.monitor ? " monitor" : ""}`}
            title={radio.squelch_open ? "Channel open" : "Squelched"}
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
            onChange={(e) => onSquelch(Number(e.target.value))}
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
              {gains.map((g) => (
                <option key={g} value={g}>
                  {g.toFixed(1)} dB
                </option>
              ))}
            </select>
            <small>
              Auto desenses the tuner near a strong transmitter — a stronger
              signal can then read <em>lower</em> on the meter. A mast-mounted
              antenna at a remote site is exactly where that bites, which is why
              a fixed gain is usually the right answer here.
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
