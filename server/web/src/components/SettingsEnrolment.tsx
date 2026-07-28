import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { EnrolmentStatus, IssuedToken } from "../types";

/**
 * Issuing and revoking a station's enrolment.
 *
 * The code appears exactly once. The server stores only a hash and genuinely
 * cannot show it again, so this pane has to make that obvious rather than
 * letting someone close it expecting to come back for the code later.
 */

function when(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function relative(value: string | null): string {
  if (!value) return "";
  const ms = new Date(value).getTime() - Date.now();
  if (Number.isNaN(ms)) return "";
  const hours = Math.round(ms / 3_600_000);
  if (ms <= 0) return " (expired)";
  if (hours < 48) return ` (in ${hours}h)`;
  return ` (in ${Math.round(hours / 24)} days)`;
}

export function SettingsEnrolment({
  stationId,
  stationName,
}: {
  stationId: string;
  stationName: string | null;
}) {
  const [status, setStatus] = useState<EnrolmentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.enrolmentStatus(stationId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load enrolment.");
    }
  }, [stationId]);

  useEffect(() => {
    setIssued(null);
    setConfirmingRevoke(false);
    void load();
  }, [load]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !status) return <p className="settings-error">{error}</p>;
  if (!status) return <p className="settings-note">Loading…</p>;

  return (
    <div className="settings-sections">
      <section className="settings-section">
        <h3>{stationName ?? "Station"}</h3>
        <dl className="settings-facts">
          <dt>Enrolled</dt>
          <dd>
            {status.enrolled ? (
              <>Yes, {when(status.enrolled_at)}</>
            ) : (
              <span className="settings-warn">Not enrolled</span>
            )}
          </dd>

          <dt>Credential</dt>
          <dd>
            {status.credential_valid ? (
              <>
                Valid until {when(status.credential_expires_at)}
                {relative(status.credential_expires_at)}
              </>
            ) : status.credential_expires_at ? (
              <span className="settings-warn">
                Not valid — expired or revoked ({when(status.credential_expires_at)})
              </span>
            ) : (
              "None issued"
            )}
          </dd>

          {status.credential_valid && !status.broker_provisioned && (
            <>
              <dt>Broker</dt>
              <dd className="settings-warn">
                The credential is valid but the broker was never told about it.
                The station will not be able to connect. Revoke and re-enrol to
                retry.
              </dd>
            </>
          )}

          <dt>Reported hardware</dt>
          <dd>
            {status.hardware ? (
              <ul className="settings-kv">
                {Object.entries(status.hardware).map(([k, v]) => (
                  <li key={k}>
                    <span>{k}</span>
                    <code>{String(v)}</code>
                  </li>
                ))}
              </ul>
            ) : (
              "Nothing reported"
            )}
            <small>
              What the box said about itself when it enrolled. Inventory only —
              nothing here decides what it is allowed to do.
            </small>
          </dd>
        </dl>
      </section>

      <section className="settings-section">
        <h3>Enrolment code</h3>

        {issued ? (
          <div className="token-reveal">
            <p className="settings-note">
              Give this to whoever is installing the box. <strong>It is shown
              once.</strong> We keep only a hash of it and cannot show it again —
              if it is lost, issue another.
            </p>
            <div className="token-value">
              <code>{issued.token}</code>
              <button
                type="button"
                className="btn ghost"
                onClick={() => {
                  void navigator.clipboard?.writeText(issued.token);
                  setCopied(true);
                }}
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <p className="settings-note">
              Expires {when(issued.expires_at)}
              {relative(issued.expires_at)}.
            </p>
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                setIssued(null);
                setCopied(false);
              }}
            >
              Done
            </button>
          </div>
        ) : (
          <>
            {status.token_outstanding ? (
              <p className="settings-note">
                {status.token_claimed
                  ? "A code was issued and has already been used. It stays usable until it expires so an installer who lost signal can retry."
                  : "A code is outstanding and has not been used yet."}{" "}
                Expires {when(status.token_expires_at)}
                {relative(status.token_expires_at)}.
              </p>
            ) : (
              <p className="settings-note">No code outstanding.</p>
            )}
            <div className="settings-actions">
              <button
                type="button"
                className="btn primary"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    setIssued(await api.issueEnrolmentToken(stationId));
                    setCopied(false);
                  })
                }
              >
                Issue a code
              </button>
              {status.token_outstanding && (
                <button
                  type="button"
                  className="btn ghost"
                  disabled={busy}
                  onClick={() =>
                    void run(() => api.revokeEnrolmentToken(stationId))
                  }
                >
                  Cancel outstanding code
                </button>
              )}
            </div>
            <small>
              Issuing a new code cancels any previous one. Two live codes for one
              station is a way to enrol the wrong box and not find out.
            </small>
          </>
        )}
      </section>

      {status.enrolled && (
        <section className="settings-section danger">
          <h3>Cut this station off</h3>
          <p className="settings-note">
            Its credential stops working immediately and its broker access is
            removed. The station keeps sensing and recording locally — it is cut
            off, not disabled — and its history, grants and configuration all
            survive. Bring it back with a new code.
          </p>
          <div className="settings-actions">
            {confirmingRevoke ? (
              <>
                <button
                  type="button"
                  className="btn danger"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      await api.revokeStationCredentials(stationId);
                      setConfirmingRevoke(false);
                    })
                  }
                >
                  Yes, revoke {stationName ?? "this station"}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setConfirmingRevoke(false)}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn ghost danger-text"
                disabled={busy}
                onClick={() => setConfirmingRevoke(true)}
              >
                Revoke credentials
              </button>
            )}
          </div>
        </section>
      )}

      {error && <p className="settings-error">{error}</p>}
    </div>
  );
}
