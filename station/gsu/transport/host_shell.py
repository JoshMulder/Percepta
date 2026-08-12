"""The agent's half of the host shell: a handoff the privileged helper reads.

The sandboxed agent **cannot** open a host shell, and that is deliberate — its
container drops every capability, mounts its root read-only, and holds no docker
socket (`station/docker-compose.yml`). The host shell is served by a separate,
privileged helper container (`deploy/hostshell/`, behind an off-by-default
compose profile). So the agent's only job here is to *instruct* that helper, the
same shape the agent instructs the updater: it writes a request into a shared
handoff volume, and the helper watches it.

This mirrors `console_proxy.py`'s opt-in and time-box, but the agent never opens
a socket itself — it writes a file. The request carries what the helper needs to
reach the platform (the `/host/ingest` URL and the station's own bearer secret)
and a wall-clock deadline; the helper connects while a request is open and its
deadline is in the future, and closes the PTY when it is not.

**Opt-in, off by default.** A station without `GSU_HOST_SHELL` refuses to write
a request at all — so even with the privileged helper container running, there
is no path from a `host.open` to a live host session. That is one of two gates;
the other is the compose profile the helper container lives behind, which a box
must opt into for the helper to exist at all.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("gsu.host_shell")

#: The path the platform serves the host ingest socket on. Beside the URL
#: derivation, because it is the platform's to change (as `stream.py` keeps
#: `INGEST_PATH`).
INGEST_PATH = "/host/ingest"

#: The handoff file the helper watches. One flat JSON object, replaced
#: atomically, read by the helper on a poll — the same handoff shape the updater
#: uses, for the same reason (two containers, no host directory).
REQUEST_FILE = "hostshell.json"


def host_ingest_url(config, enrolment=None) -> str | None:
    """Where the platform's host ingest is, derived like the media/console URLs.

    `GSU_HOST_SHELL_URL` overrides for a platform whose stated address is only
    routable from inside its own network; otherwise it is the platform API host
    with the scheme switched to WebSocket and this module's path appended.
    """
    override = getattr(config, "host_shell_url", None)
    if override:
        return override
    api = getattr(config, "platform_url", "") or ""
    scheme, separator, rest = api.partition("://")
    if not separator or not rest:
        return None
    ws = "wss" if scheme.lower() == "https" else "ws"
    return f"{ws}://{rest.rstrip('/')}{INGEST_PATH}"


class HostShellCoordinator:
    """Writes the helper its instructions. No socket, no thread — just a file.

    The agent cannot do the privileged work, so this does not try to. It records
    what the platform asked for and lets the helper, which can, act on it.
    """

    def __init__(
        self,
        handoff_dir,
        url: str | None,
        secret: str | None,
        *,
        enabled: bool = False,
        lease_seconds: float = 300.0,
    ) -> None:
        self.handoff_dir = Path(handoff_dir)
        self.url = url
        self.secret = secret
        self.enabled = enabled
        self.lease_seconds = lease_seconds

    @property
    def _request_path(self) -> Path:
        return self.handoff_dir / REQUEST_FILE

    def request_open(self, lease_seconds: float | None = None) -> str:
        """`host.open`: ask the helper to open a host session for a bounded window.

        Refused unless the box has opted in — the safety property: no path from a
        `host.open` to a request file on a station that has not set
        `GSU_HOST_SHELL`, so the default is a box that cannot be reached this way.
        """
        if not self.enabled:
            log.warning(
                "Refusing host.open: host shell access is not enabled on this "
                "station (set GSU_HOST_SHELL to allow it)."
            )
            return "refused: host shell not enabled"
        if not self.url or not self.secret:
            return "refused: no platform host URL or credential"
        lease = self.lease_seconds if lease_seconds is None else float(lease_seconds)
        self._write({
            "open": True,
            "url": self.url,
            # The helper is a separate container with none of the agent's config,
            # so it is handed the URL and the credential rather than deriving
            # them. The file is written 0600 — it holds the station secret.
            "secret": self.secret,
            "deadline": time.time() + lease,
        })
        return f"host session requested for {int(lease)}s"

    def request_close(self, reason: str = "closed by the platform") -> str:
        self._write({"open": False})
        return reason

    def close(self) -> None:
        """Leave no standing request behind on shutdown or factory reset, so the
        helper does not keep a root shell open to a box that has gone away."""
        try:
            self._write({"open": False})
        except OSError:
            pass

    def update_secret(self, secret: str | None) -> None:
        self.secret = secret

    def _write(self, payload: dict) -> None:
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._request_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        try:
            os.chmod(tmp, 0o600)  # it may hold the station secret
        except OSError:
            pass
        tmp.replace(self._request_path)
