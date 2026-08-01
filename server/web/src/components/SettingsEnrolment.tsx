import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { EnrolmentStatus, IssuedToken } from "../types";

/**
 * Issuing and revoking a station's enrolment.
 *
 * The code appears exactly once. The server stores only a hash and genuinely
 * cannot show it again, so this pane has to make that obvious rather than
 * letting someone close it expecting to come back for the code later.
 *
 * Issuing a code follows the moment it is needed rather than sitting on the
 * station's page forever. It appears when a station is created, because handing
 * the installer a code is the next thing that happens; it stays on the station's
 * page only while that station has never enrolled. Once a box is connected, the
 * way to replace its credential is to revoke it — and an "issue a code" button
 * next to a working station is an invitation to enrol a second box against it,
 * which is the failure this is shaped to avoid.
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
  onCredentialChanged,
}: {
  stationId: string;
  stationName: string | null;
  /** Called after anything that changes whether a credential is live. The
   *  Delete section is a sibling that decides whether to offer itself from
   *  this same status, and it only asked once, on mount — so revoking left it
   *  showing the answer from before, until the page was left and re-entered. */
  onCredentialChanged?: () => void;
}) {
  const [status, setStatus] = useState<EnrolmentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.enrolmentStatus(stationId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load enrolment.");
    }
  }, [stationId]);

  useEffect(() => {
    setConfirmingRevoke(false);
    void load();
  }, [load]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await load();
      // Issue, cancel and revoke all run through here, so the sibling that
      // cares needs telling in exactly one place.
      onCredentialChanged?.();
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
        <h3>Enrolment</h3>
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
          </dd>
        </dl>
      </section>

      {/* Only until the box has enrolled once. After that a code is something
          you reach for deliberately, by revoking below. */}
      {!status.enrolled && (
        <EnrolmentCode stationId={stationId} status={status} onChanged={load} />
      )}

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

/**
 * The "issue a code" step on its own.
 *
 * Used in two places — the new-station page and an unenrolled station's page —
 * so it takes the status it should render rather than fetching its own. Where
 * there is already a status on screen, two fetches could disagree, and the
 * disagreement would be about whether a code is outstanding.
 */
export function EnrolmentCode({
  stationId,
  status,
  onChanged,
}: {
  stationId: string;
  status: EnrolmentStatus;
  onChanged: () => Promise<void> | void;
}) {
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setIssued(null);
    setCopied(false);
  }, [stationId]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="settings-section">
      <h3>Enrolment code</h3>

      {issued ? (
        <div className="token-reveal">
          <p className="settings-note">
            Give this to whoever is installing the box. <strong>It is shown
            once.</strong> We keep only a hash of it and cannot show it again —
            if it is lost, issue another.
          </p>
          {/* The combined string, not the bare code. It carries the code, this
              platform's address and the CA fingerprint — all three had to
              reach the box anyway, and the one easiest to skip was the
              fingerprint, which is the one deciding whether the code is typed
              into the real platform or into whatever answered. Falls back to
              the bare code on a platform that pins no CA. */}
          <div className="token-value">
            <code>{issued.bootstrap || issued.token}</code>
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                void navigator.clipboard?.writeText(
                  issued.bootstrap || issued.token,
                );
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="settings-note">
            On the box: <code>sudo station/deploy/bootstrap.sh --enrol …</code>
          </p>
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
          {/* One or the other. To replace a code you cancel it first, which
              also means there is never a moment with two live codes for one
              station. */}
          <div className="settings-actions">
            {status.token_outstanding ? (
              <button
                type="button"
                className="btn ghost"
                disabled={busy}
                onClick={() => void run(() => api.revokeEnrolmentToken(stationId))}
              >
                Cancel outstanding code
              </button>
            ) : (
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
            )}
          </div>
        </>
      )}

      {error && <p className="settings-error">{error}</p>}
    </section>
  );
}

/**
 * The code step for a station that has just been created.
 *
 * Loads its own status, because on the new-station page there is nothing else
 * on screen that has one.
 */
export function NewStationEnrolment({ stationId }: { stationId: string }) {
  const [status, setStatus] = useState<EnrolmentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await api.enrolmentStatus(stationId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load enrolment.");
    }
  }, [stationId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error && !status) return <p className="settings-error">{error}</p>;
  if (!status) return <p className="settings-note">Loading…</p>;
  return <EnrolmentCode stationId={stationId} status={status} onChanged={load} />;
}
