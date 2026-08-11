import { useState } from "react";
import { ApiError, api } from "../api";
import type { Capability, HealthPayload } from "../types";

/**
 * Remote software update: what the station is running, how the last update went,
 * and the control to push a new one.
 *
 * The split between showing and doing is deliberate. Everything at the top is
 * read from live telemetry — `health.software`, which the station sources from
 * its own update coordinator (station/gsu/update.py). The 202 from the push says
 * only that the station was *told*; what it ends up running comes back on the
 * health stream, the same way a radio command's real effect does. So this pane
 * never reports success on the station's behalf — it asks, then watches the
 * version move.
 *
 * `agent_version` alone could not tell that story: it is a build constant baked
 * into the image, so it reads the same before and after a release and cannot
 * show an update landing, a rollback, or a box stuck mid-update. `running_version`
 * is that same string, but it arrives in the object that also carries the desired
 * version and the host updater's last result.
 *
 * The push is two-step because it is the most consequential thing an operator can
 * do here — it recreates the agent on a box that is hard to reach — and a digest
 * is pasted, not remembered, so a mistyped field should not ship. `station.update`
 * gates the whole tab; the check here is belt-and-braces against the API, which
 * is the real gate.
 */

const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;

function when(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

// How the last update read, coloured. `updated` is the only clean landing; a
// rollback kept the station up on the old image (a caution, not a failure);
// anything else left it needing attention.
function resultTone(result: string): "ok" | "warn" | "bad" {
  if (result === "updated") return "ok";
  if (result === "rolled_back") return "warn";
  return "bad";
}

function resultWords(result: string): string {
  switch (result) {
    case "updated":
      return "updated";
    case "rolled_back":
      return "rolled back to the previous image";
    case "signature_rejected":
      return "refused — signature did not verify";
    case "rollback_failed":
      return "rollback failed — needs attention";
    case "rollback_impossible":
      return "failed with no image to roll back to";
    default:
      return result;
  }
}

const TONE_COLOUR: Record<string, string> = {
  ok: "var(--ok, #3fb950)",
  warn: "var(--warn)",
  bad: "var(--bad, #f85149)",
};

export function SettingsUpdate({
  health,
  caps,
  stationId,
  stationName,
}: {
  health: HealthPayload | null;
  caps: Capability[];
  stationId: string | null;
  stationName: string | null;
}) {
  const sw = health?.software;
  const running = sw?.running_version ?? health?.agent_version ?? null;
  const desired = sw?.desired_version ?? null;
  const lastResult = sw?.update_last_result ?? null;
  const lastVersion = sw?.update_last_version ?? null;
  const lastAt = sw?.update_at ?? null;

  const canUpdate = caps.includes("station.update");

  const [image, setImage] = useState("");
  const [digest, setDigest] = useState("");
  const [tag, setTag] = useState("");
  const [force, setForce] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The label the console last asked for, so it can say "requested" until the
  // station's own version telemetry above takes over the story.
  const [requested, setRequested] = useState<string | null>(null);

  const digestOk = DIGEST_RE.test(digest.trim());
  const label = tag.trim() || (digestOk ? `${digest.trim().slice(0, 19)}…` : "");
  const valid = image.trim() !== "" && digestOk && stationId !== null;

  // Any edit re-arms the confirm: you cannot change the target after reading the
  // confirmation and then send the old one.
  const edited = () => {
    setConfirming(false);
    setRequested(null);
  };

  const submit = () => {
    if (!valid || !stationId) return;
    setBusy(true);
    setError(null);
    api
      .updateStation(stationId, {
        image: image.trim(),
        digest: digest.trim(),
        tag: tag.trim() || undefined,
        force: force || undefined,
      })
      .then(() => {
        setRequested(label);
        setConfirming(false);
        // Keep the image (the repository rarely changes between releases); clear
        // the digest so the same push cannot be fired twice by reflex.
        setDigest("");
        setTag("");
        setForce(false);
      })
      .catch((e) =>
        setError(e instanceof ApiError ? e.message : "The station did not accept the update."),
      )
      .finally(() => setBusy(false));
  };

  return (
    <div className="settings-sections">
      <section className="settings-section">
        <h3>Software</h3>
        <dl className="settings-facts">
          <dt>Running</dt>
          <dd>
            {running ? (
              <code>{running}</code>
            ) : (
              <span className="settings-warn">unknown</span>
            )}
          </dd>

          {desired && (
            <>
              <dt>Updating to</dt>
              <dd title="A remote update is in flight; this becomes the running version when it lands, or clears if it rolls back.">
                <code>{desired}</code> …
              </dd>
            </>
          )}

          <dt>Last update</dt>
          <dd>
            {lastResult ? (
              <>
                <b style={{ color: TONE_COLOUR[resultTone(lastResult)] }}>
                  {resultWords(lastResult)}
                </b>
                {lastVersion ? ` (${lastVersion})` : ""}
                {when(lastAt) ? ` — ${when(lastAt)}` : ""}
              </>
            ) : (
              "None yet"
            )}
          </dd>
        </dl>
        {!health && (
          <p className="settings-note">
            No telemetry from {stationName ?? "this station"} yet — the running
            version appears once it reports. A push is still delivered and taken
            when the station is next online.
          </p>
        )}
      </section>

      {canUpdate && (
        <section className="settings-section">
          <h3>Push an update</h3>
          <label className="field">
            <span>Image</span>
            <input
              type="text"
              value={image}
              placeholder="registry.percepta.nz/percepta-gsu"
              spellCheck={false}
              autoCapitalize="none"
              onChange={(e) => {
                setImage(e.target.value);
                edited();
              }}
            />
          </label>
          <label className="field">
            <span>Digest</span>
            <input
              type="text"
              value={digest}
              placeholder="sha256:…"
              spellCheck={false}
              autoCapitalize="none"
              // Not flagged until they have typed enough to mean it, so the field
              // is not scolding an empty box.
              aria-invalid={digest.trim().length > 7 && !digestOk}
              onChange={(e) => {
                setDigest(e.target.value);
                edited();
              }}
            />
          </label>
          <label className="field">
            <span>Tag (optional)</span>
            <input
              type="text"
              value={tag}
              placeholder="v0.2.0"
              spellCheck={false}
              autoCapitalize="none"
              onChange={(e) => {
                setTag(e.target.value);
                edited();
              }}
            />
          </label>
          <label className="field checkbox-field">
            <input
              type="checkbox"
              checked={force}
              onChange={(e) => {
                setForce(e.target.checked);
                edited();
              }}
            />
            <span>Force — re-attempt a digest a previous update rejected</span>
          </label>

          {error && <p className="settings-error">{error}</p>}
          {requested && !error && (
            <p className="settings-note">
              Requested <b>{requested}</b> — the station verifies the signature,
              swaps the agent, and reports the result above.
            </p>
          )}

          <div className="settings-actions">
            {confirming ? (
              <>
                <button
                  type="button"
                  className="btn danger"
                  disabled={busy}
                  onClick={submit}
                >
                  {busy ? "Pushing…" : `Confirm: push ${label}`}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  disabled={busy}
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn primary"
                disabled={!valid || busy}
                onClick={() => setConfirming(true)}
              >
                Push update…
              </button>
            )}
          </div>
          <small>
            The station runs an image only after it verifies the signature against
            the keys it was pinned at enrolment, and rolls back on its own if the
            new one does not come up publishing — so this cannot deploy unsigned or
            broken code, only ask for a signed one. The digest is the immutable
            pin; the tag is a label carried through for the record.
          </small>
        </section>
      )}
    </div>
  );
}
