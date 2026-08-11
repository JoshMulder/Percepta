"""The station half of remote update: record what the platform asked for.

The agent cannot update itself and must not be able to — no docker socket, every
capability dropped, read-only root (`DECISIONS.md` item 48). So a `system.update`
command does not *do* the update here; it writes down the target the platform
named, in a file the host-side updater watches — the same shape as the host
touching `setup-open` to reopen the console window. The privileged work (pull,
verify the signature, recreate the container, gate on it publishing, roll back)
is the host's, outside the sandbox.

This module is only the handoff and the version the station reports, so the
platform can watch `running` move toward `desired`. It deliberately knows nothing
about registries, pulling or Docker: naming those here would be a second place
they live, and the whole point of the split is that the sandbox never learns how
to replace itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("gsu.update")

#: `sha256:` and 64 hex. The digest is the immutable pin the host pulls by; a tag
#: is only a label. Requiring it here means a malformed target is refused at the
#: command rather than written and then puzzled over on the host.
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class UpdateCoordinator:
    """Turns a `system.update` command into a marker the host updater reconciles,
    and reports the running (and last-requested) version."""

    def __init__(self, version: str, handoff_dir: Path) -> None:
        self.version = version or "dev"
        self.handoff_dir = Path(handoff_dir)
        self._request_path = self.handoff_dir / "update-request.json"
        #: Written by the host updater, not here — read back only, so telemetry
        #: can show how the last update went. Absent until the host has run once.
        self._status_path = self.handoff_dir / "update-status.json"

    def request(self, image: str, tag: str, digest: str, force: bool = False) -> str:
        """Record the target the platform named. Raises ValueError on a target
        the host could not act on — a missing image or a malformed digest — so
        the command is refused loudly rather than written and silently ignored.

        Written atomically (temp then rename) because the host updater may read
        it at any moment, and a half-written marker is a target nobody named.
        """
        digest = (digest or "").strip().lower()
        if not _DIGEST.match(digest):
            raise ValueError(f"{digest!r} is not a sha256 digest")
        image = (image or "").strip()
        if not image:
            raise ValueError("no image named")
        marker = {
            "image": image,
            "tag": (tag or "").strip(),
            "digest": digest,
            "force": bool(force),
            "from_version": self.version,
            "requested_at": datetime.now(UTC).isoformat(),
        }
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._request_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(marker, indent=2, sort_keys=True))
        tmp.replace(self._request_path)
        log.info("Update requested: %s@%s (tag %s), from %s.",
                 image, digest[:16], marker["tag"] or "-", self.version)
        return f"requested {marker['tag'] or digest[:16]}"

    def store_signing_keys(self, keys: tuple[str, ...]) -> None:
        """Write the cosign public keys the platform pinned us with into the
        handoff, where the host updater's `cosign verify --key` reads them.

        Enrolment and every renewal hand over the whole set, and this replaces it
        wholesale — each key its own file, named by its own hash so an unchanged
        key keeps its name, and any file no longer in the set is removed. That is
        what makes a rotation (add the new key; later drop the old) land here
        without a site visit: the updater verifies against whatever is present,
        so old-and-new both work through the overlap and the old one simply stops
        being written once the platform stops sending it.
        """
        keys_dir = self.handoff_dir / "signing-keys"
        keys_dir.mkdir(parents=True, exist_ok=True)
        wanted: dict[str, str] = {
            f"cosign-{hashlib.sha256(pem.encode()).hexdigest()[:16]}.pub": pem
            for pem in keys
        }
        for name, pem in wanted.items():
            tmp = (keys_dir / name).with_suffix(".tmp")
            tmp.write_text(pem)
            tmp.replace(keys_dir / name)
        for existing in keys_dir.glob("*.pub"):
            if existing.name not in wanted:
                existing.unlink(missing_ok=True)

    def store_registry_credential(self, username: str, secret: str) -> None:
        """Write the credential the host updater uses to pull from the private
        registry into the handoff. It is the station's own bearer secret — the
        one the platform's registry token endpoint accepts — so no update-specific
        credential exists on the box, and it is refreshed on enrolment and every
        renewal, so a rotated secret follows the same path. 0600, in a volume
        shared only with the updater."""
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        path = self.handoff_dir / "registry-credential.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"username": username, "secret": secret}))
        tmp.chmod(0o600)
        tmp.replace(path)

    @property
    def desired_version(self) -> str | None:
        """The tag (or short digest) the last request named, or None. Read from
        the marker so it survives a restart — a request outlives the process that
        took it, and the host may not have acted yet."""
        marker = self._read(self._request_path)
        if not marker:
            return None
        return marker.get("tag") or (marker.get("digest") or "")[:16] or None

    def state(self) -> dict:
        """Running and desired version for the console, plus the host updater's
        last result if it has written one. All optional beyond the running
        version: a box with no update in flight reports just what it is on."""
        out: dict = {"running_version": self.version}
        desired = self.desired_version
        if desired and desired != self.version:
            out["desired_version"] = desired
        status = self._read(self._status_path)
        if isinstance(status, dict):
            for key in ("last_result", "last_version", "at"):
                if status.get(key) is not None:
                    out[f"update_{key}"] = status[key]
        return out

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None
