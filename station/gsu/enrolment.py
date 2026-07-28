"""Claiming an identity, and keeping it.

Three exchanges, all station-initiated because nothing can reach inward through
CGNAT (`contract/enrolment.md` §1):

    POST /api/enrol         token + hardware  → credential, broker, site
    POST /api/enrol/renew   current credential → a fresh one
    GET  /api/enrol/status  current credential → standing, and a reference clock

Written against stdlib HTTP on purpose. This runs on an unattended box that must
boot with whatever is in the image; the fewer things that have to be installable
in the field, the better.

**All three go over TLS verified against the pinned CA** (`gsu/tls.py`), the
same one the broker is checked with — §4 sends one `ca_pem` and one CA signs
both. There is a bootstrap in here worth being explicit about: the *first*
`POST /api/enrol` happens before any CA has been pinned, so it can only be
verified against a CA installed with the image (`GSU_CA_FILE`). Without one this
client refuses the call and says what to install, rather than reaching for the
system trust store — that would make the pinning decorative, since the first
call is the one that hands over a token and receives an identity.

**Renewal is the part that strands sites.** §6 is unambiguous: renew early,
back off, and treat failure as a health alarm long before it is an outage. The
`Renewer` below runs on its own thread, does exactly that, and reports through
`health` so the condition leaves on telemetry.
"""

from __future__ import annotations

import json
import logging
import random
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta

from . import clock
from .credentials import CredentialStore, Enrolment
from .tls import Refusal, Trust

log = logging.getLogger("gsu.enrolment")

HTTP_TIMEOUT = 15.0


class EnrolmentError(RuntimeError):
    """Anything that stopped an exchange completing."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


#: What the technician is shown, per contract/enrolment.md §4. Unknown, expired
#: and already-used are deliberately indistinguishable.
TECHNICIAN_MESSAGE = {
    404: "This code is not valid. Ask for a new one.",
    409: "This station is already set up.",
    422: "The box sent something the platform could not read. This is a bug.",
}


@dataclass(frozen=True)
class Standing:
    """What `/api/enrol/status` says. Thin by design."""

    station_id: str
    name: str
    config_version: int
    credential_expires_at: str | None
    renew_now: bool
    server_time: str | None


class EnrolmentClient:
    def __init__(self, platform_url: str, trust: Trust | None = None) -> None:
        self.platform_url = platform_url.rstrip("/")
        #: Reassigned when a claim brings back a CA the box did not have — the
        #: next call is then pinned to it. Never reassigned to something weaker.
        self.trust = trust or Trust()

    # --- exchanges ------------------------------------------------------

    def claim(self, token: str, hardware: dict) -> Enrolment:
        """Trade a code for a credential.

        Refuses outright if the clock is implausible. Enrolling with a reset
        clock produces a station that authenticates today and is locked out
        tomorrow with nobody on site (contract/enrolment.md §6).
        """
        reason = clock.implausible_reason()
        if reason is not None:
            raise clock.ClockImplausible(
                f"Refusing to enrol: {reason} Sync time and try again."
            )
        body = self._post("/api/enrol", {"token": token.strip(), "hardware": hardware})
        return Enrolment.from_response(body)

    def renew(self, secret: str) -> Enrolment:
        """Renew with the *current* credential. The station never sends its own
        id — it is derived from the credential, so a box holding a valid secret
        still cannot assert which station it is."""
        body = self._post("/api/enrol/renew", None, secret=secret)
        return Enrolment.from_response(body)

    def status(self, secret: str) -> Standing:
        body = self._get("/api/enrol/status", secret=secret)
        return Standing(
            station_id=str(body.get("station_id", "")),
            name=body.get("name") or "",
            config_version=int(body.get("config_version", 0)),
            credential_expires_at=body.get("credential_expires_at"),
            renew_now=bool(body.get("renew_now")),
            server_time=body.get("server_time"),
        )

    # --- plumbing -------------------------------------------------------

    def _context(self) -> ssl.SSLContext | None:
        """A verifying context pinned to the platform's CA, or None for a
        plaintext development URL that policy has already allowed."""
        if not self.platform_url.startswith("https"):
            return None
        return self.trust.context()

    def _request(self, method: str, path: str, payload: dict | None, secret: str | None):
        url = f"{self.platform_url}{path}"
        # Before the socket, not after: a refusal is a decision this station
        # made, and it must never be reachable by retrying on weaker terms.
        try:
            self.trust.check(self.platform_url, "the platform API")
        except Refusal as exc:
            raise EnrolmentError(str(exc), retryable=False) from exc
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        if secret:
            request.add_header("Authorization", f"Bearer {secret}")
        try:
            with urllib.request.urlopen(
                request, timeout=HTTP_TIMEOUT, context=self._context()
            ) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:200]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            raise EnrolmentError(
                TECHNICIAN_MESSAGE.get(exc.code, f"The platform refused: {exc.code}."),
                status=exc.code,
                # A 4xx will not become a 2xx by trying again with the same
                # inputs; a 5xx or a dropped link will.
                retryable=exc.code >= 500 or exc.code == 429,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError):
                # A certificate that does not verify is not a dropped link, and
                # telling a technician to "check the aerial" would send them
                # after the wrong thing entirely.
                raise EnrolmentError(
                    f"The platform at {self.platform_url} presented a certificate "
                    f"this station will not accept ({self.trust.describe()}): "
                    f"{reason}. Nothing was sent. This is either the wrong CA on "
                    "the box or the wrong certificate on the platform — the "
                    "station will not connect without checking.",
                    retryable=False,
                ) from exc
            raise EnrolmentError(
                f"Could not reach the platform at {self.platform_url}: {exc}",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise EnrolmentError(f"Unreadable response: {exc}", retryable=False) from exc

    def _post(self, path: str, payload: dict | None, secret: str | None = None) -> dict:
        return self._request("POST", path, payload if payload is not None else {}, secret)

    def _get(self, path: str, secret: str | None = None) -> dict:
        return self._request("GET", path, None, secret)


#: Renewal backoff. Starts often enough to ride out a Starlink dropout without
#: anyone noticing, caps well below the credential lifetime so a long outage
#: still gets many attempts before expiry.
BACKOFF_START = timedelta(seconds=30)
BACKOFF_MAX = timedelta(minutes=15)

#: How close to expiry the health condition escalates to critical. At this point
#: it is worth a person looking, because past expiry it is a site visit.
CRITICAL_WINDOW = timedelta(hours=6)


class Renewer:
    """Keeps the credential fresh, on its own thread, and complains early.

    Deliberately does not touch the transport. When a renewal succeeds it stores
    the new credential and calls `on_renewed`; reconnecting is the agent's
    business, and the overlap window (§6) means it is not urgent — both
    credentials work for a while, so a station that renews and then loses power
    mid-swap is not locked out.
    """

    def __init__(
        self,
        client: EnrolmentClient,
        store: CredentialStore,
        enrolment: Enrolment,
        health,
        on_renewed=None,
        poll_seconds: float = 30.0,
    ) -> None:
        self.client = client
        self.store = store
        self.enrolment = enrolment
        self.health = health
        self.on_renewed = on_renewed
        self.poll_seconds = poll_seconds
        self.failures = 0
        self.last_error: str | None = None
        #: Set when the platform rejects the credential outright, which is not
        #: something retrying fixes.
        self.revoked = False
        self._backoff = BACKOFF_START
        self._next_attempt = clock.now()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gsu-renewer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a renewer that dies is an outage
                log.exception("Renewal thread hiccup; continuing.")

    def tick(self, at=None) -> None:
        """One pass. Separated from the thread so it is testable."""
        at = at or clock.now()
        credential = self.enrolment.credential

        # Expiry is evaluated against our own clock, and our own clock is
        # checked. A box that wrongly believes it has expired behaves as badly
        # as one that wrongly believes it has not (contract/enrolment.md §6).
        reason = clock.implausible_reason(at)
        if reason is not None:
            self.health.raise_condition("clock.implausible", "critical", reason)
            return
        self.health.clear("clock.implausible")

        remaining = credential.seconds_remaining(at)
        if not credential.due_for_renewal(at):
            return
        if at < self._next_attempt:
            return

        try:
            renewed = self.client.renew(credential.secret)
        except Exception as exc:  # noqa: BLE001 - a renewer that raises is an outage
            self.failures += 1
            self.last_error = str(exc)
            self._backoff = min(BACKOFF_MAX, self._backoff * 2)

            # A rejected credential is a different fault from an unreachable
            # platform, and only one of them can be fixed by waiting. An admin
            # revoking a station, or another box claiming its enrolment, lands
            # here — and the station must keep sensing and recording either way.
            # It is a health condition, never a reason to exit.
            if getattr(exc, "status", None) in (401, 403):
                self.revoked = True
                self.health.raise_condition(
                    "credential.revoked", "critical",
                    "The platform no longer accepts this station's credential. "
                    "Sensing and recording continue locally; publishing will "
                    "stop when the broker notices. Re-enrol with a new code.",
                )
                log.error(
                    "Credential rejected by the platform (%s). Still sensing and "
                    "recording; re-enrolment is needed.", exc,
                )
                self._next_attempt = at + BACKOFF_MAX
                return
            # Jitter: a fleet that all enrolled in the same afternoon renews in
            # the same minute, and would retry in the same minute too.
            jitter = random.uniform(0, self._backoff.total_seconds() * 0.2)
            self._next_attempt = at + self._backoff + timedelta(seconds=jitter)
            severity = "critical" if remaining < CRITICAL_WINDOW.total_seconds() else "warning"
            self.health.raise_condition(
                "credential.renewal_failing",
                severity,
                f"{self.failures} failed renewal(s); credential expires in "
                f"{remaining / 3600:.1f} h. Last error: {exc}",
            )
            log.warning("Credential renewal failed (%s); retrying with backoff.", exc)
            return

        self.enrolment = self.enrolment.with_credential(renewed)
        self.store.save(self.enrolment)
        self.failures = 0
        self.last_error = None
        self.revoked = False
        self._backoff = BACKOFF_START
        self._next_attempt = at
        self.health.clear("credential.renewal_failing")
        self.health.clear("credential.revoked")
        log.info(
            "Credential renewed; expires %s.", self.enrolment.credential.expires_at.isoformat()
        )
        if self.on_renewed:
            self.on_renewed(self.enrolment)
