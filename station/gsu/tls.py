"""The station's trust root, and the rules about when it may talk at all.

`contract/enrolment.md` §4 gives the station a `broker.ca_pem` at enrolment:
*"pinned; the station verifies the platform"*. That is a stronger statement than
ordinary TLS. A public trust store says "somebody a browser vendor trusts signed
this"; a pinned private CA says "the CA that issued this station's identity
signed this, and nothing else will do". On a box that lives on someone else's
network behind CGNAT, reachable only over the public internet, that difference
is the whole security boundary.

Three rules follow, and all three are enforced here rather than left to the
caller's good manners:

**1. Verification is never disabled.** There is no `verify=False` in this
package and no environment variable that produces one. The only knob is *which*
CA to trust, and the default is the pinned one.

**2. There is no plaintext fallback.** A station that quietly downgrades to
`redis://` when TLS fails is worse than one that refuses to start, because the
refusal is visible and the downgrade is not — nobody finds out until the traffic
is already on the wire. When TLS is required and cannot be established, this
module refuses, the agent raises a health condition, and the local console says
so in words.

**3. The refusal is loud and local.** `Refusal` carries a message written for
whoever is standing in front of the box, not for whoever wrote the code.

The same CA signs the broker and the API — one file, one fingerprint, both
paths. It is persisted next to the credential, 0600, because a CA that is
re-fetched over an unverified channel every boot is not pinned to anything.
"""

from __future__ import annotations

import hashlib
import logging
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("gsu.tls")

#: Trust the CA the platform pinned at enrolment. The default, and what
#: `contract/enrolment.md` §4 describes.
TRUST_PINNED = "pinned"

#: Trust the operating system's CA bundle instead. A deliberate, logged
#: reduction for the case where the platform is fronted by a publicly-signed
#: certificate. Still full verification — it is not a way to turn checking off,
#: and there is no third option that is.
TRUST_SYSTEM = "system"

TRUST_MODES = (TRUST_PINNED, TRUST_SYSTEM)

#: URL schemes that carry TLS, across both transports and the API.
TLS_SCHEMES = ("https", "rediss", "mqtts", "ssl", "wss")

#: ...and the ones that do not. Anything not in either list is refused rather
#: than guessed at: an unrecognised scheme is not evidence of encryption.
PLAINTEXT_SCHEMES = ("http", "redis", "mqtt", "tcp", "ws", "unix")


class Refusal(RuntimeError):
    """This station will not make that connection, and why.

    Raised instead of connecting, never caught and retried into a weaker mode.
    The message is what a technician sees on the console and in the log.
    """


def scheme_of(url: str) -> str:
    return (url.split("://", 1)[0] or "").lower()


def is_tls(url: str) -> bool:
    return scheme_of(url) in TLS_SCHEMES


def is_plaintext(url: str) -> bool:
    return scheme_of(url) in PLAINTEXT_SCHEMES


def host_of(url: str) -> str:
    """Host from a URL, without importing a parser per scheme."""
    rest = url.split("://", 1)[-1]
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    if rest.startswith("["):                       # IPv6 literal
        return rest[1:].split("]", 1)[0]
    return rest.split(":", 1)[0]


def fingerprint(pem: str) -> str | None:
    """SHA-256 over the certificate's DER, the form a person can compare.

    Printed on the console and by `gsu preflight` so that "is this box pinned to
    the right CA" is a question with a one-line answer, checkable against
    `openssl x509 -in ca.pem -noout -fingerprint -sha256` on the platform.
    """
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (ValueError, TypeError):
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


class CaStore:
    """The pinned CA on disk: 0600, in the 0700 state directory.

    Written from the enrolment response and read on every boot afterwards. Kept
    beside the credential deliberately — they are one identity: the secret says
    who this box is, the CA says who it is allowed to say that to.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        try:
            text = self.path.read_text()
        except OSError:
            return None
        return text if "BEGIN CERTIFICATE" in text else None

    def save(self, pem: str) -> bool:
        """Persist the CA. True if it changed.

        A changed CA is worth noticing: it is either a planned rotation or
        somebody else's certificate, and the two are indistinguishable from
        here. The caller logs it; this only reports it.
        """
        if not pem or "BEGIN CERTIFICATE" not in pem:
            raise ValueError("that is not a PEM certificate")
        existing = self.load()
        if existing is not None and existing.strip() == pem.strip():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        tmp = self.path.with_suffix(".tmp")
        # 0600 from the first byte rather than chmod-ed afterwards. The CA is
        # not itself a secret, but its *integrity* is the whole control: a file
        # anyone can rewrite is a trust root anyone can replace.
        handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as file:
            file.write(pem if pem.endswith("\n") else pem + "\n")
        os.replace(tmp, self.path)
        return existing is not None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class Trust:
    """Where this station's trust comes from, resolved once at start.

    `path` is a file, never PEM text, because both consumers want a path:
    `ssl.create_default_context(cafile=…)` and redis-py's `ssl_ca_certs`. One
    resolution, one file, both links.
    """

    mode: str = TRUST_PINNED
    path: Path | None = None
    #: Where it came from, for the console: "enrolment", "installed", "system".
    source: str = "none"
    fingerprint: str | None = None
    #: Refuse plaintext even before a CA has ever been seen. Set on real
    #: deployments; off in development, where the broker is a local container
    #: with no TLS at all.
    require_tls: bool = False

    @property
    def pinned(self) -> bool:
        return self.mode == TRUST_PINNED and self.path is not None

    def describe(self) -> str:
        if self.mode == TRUST_SYSTEM:
            return "system CA bundle (not pinned)"
        if self.path is None:
            return "no CA pinned yet"
        return f"pinned CA from {self.source}, SHA-256 {self.fingerprint or 'unreadable'}"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "require_tls": self.require_tls,
        }

    # --- the two questions everything else asks --------------------------

    def check(self, url: str, what: str) -> None:
        """Refuse, in words, if this URL may not be used as things stand.

        Called before every connection attempt to the broker and the platform.
        Raising here is the design: there is no path from a refusal to a weaker
        connection, only to a health condition and a message on the console.
        """
        scheme = scheme_of(url)
        if is_tls(url):
            if self.mode == TRUST_SYSTEM:
                return
            if self.path is None:
                raise Refusal(
                    f"{what} at {url} needs TLS, but this box has no CA to check "
                    "it against. Enrol it (the platform sends its CA), or install "
                    "the CA file and set GSU_CA_FILE. It will not connect without "
                    "one, and it will not fall back to an unverified connection."
                )
            return
        if is_plaintext(url):
            if self.pinned:
                raise Refusal(
                    f"{what} at {url} is unencrypted, and this box is pinned to a "
                    f"CA ({self.fingerprint}). A station that downgrades quietly "
                    "is worse than one that stops, because nobody finds out. Use "
                    f"{_tls_equivalent(scheme)}:// instead."
                )
            if self.require_tls:
                raise Refusal(
                    f"{what} at {url} is unencrypted and this station is "
                    "configured to require TLS (GSU_REQUIRE_TLS=1). Point it at "
                    f"{_tls_equivalent(scheme)}:// or, on a development box only, "
                    "unset GSU_REQUIRE_TLS."
                )
            return
        raise Refusal(
            f"{what} at {url} uses a scheme this station does not recognise, so "
            "it cannot tell whether it is encrypted. Refusing rather than guessing."
        )

    def context(self) -> ssl.SSLContext:
        """A verifying TLS context. There is no other kind here.

        `check_hostname` and `CERT_REQUIRED` are set explicitly rather than
        relied on as defaults: this code runs on whatever Python and whatever
        redis-py the field box has, and a default that changed between versions
        is not something to discover after deployment.
        """
        if self.mode == TRUST_SYSTEM:
            context = ssl.create_default_context()
        else:
            if self.path is None:
                raise Refusal("no pinned CA to build a TLS context from")
            try:
                context = ssl.create_default_context(cafile=str(self.path))
            except OSError as exc:
                # The pinned CA has gone missing or become unreadable — a wiped
                # state directory, a bad permission change. A refusal, because
                # the alternative is a station that starts trusting whatever
                # answers the moment its trust root disappears.
                raise Refusal(
                    f"The pinned CA at {self.path} cannot be read ({exc}). "
                    "Refusing to connect: a missing trust root is not a reason "
                    "to trust anything else. Re-enrol, or reinstall the CA."
                ) from exc
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def redis_kwargs(self, url: str) -> dict:
        """What redis-py needs to make the same guarantees.

        redis-py has shipped `ssl_check_hostname` defaulting to False in some
        versions and True in others. Passing it explicitly means the station
        does not depend on which one pip resolved on the day the box was built
        — a difference that would otherwise be invisible until somebody looked
        at a packet capture.

        The first three keys are not optional: `redis_transport` refuses to
        connect rather than dropping one it cannot pass.
        """
        if not is_tls(url):
            return {}
        kwargs: dict = {
            "ssl_cert_reqs": "required",
            "ssl_check_hostname": True,
            "ssl_min_version": ssl.TLSVersion.TLSv1_2,
        }
        if self.mode == TRUST_PINNED:
            if self.path is None:
                raise Refusal("no pinned CA to verify the broker against")
            kwargs["ssl_ca_certs"] = str(self.path)
        return kwargs


def _tls_equivalent(scheme: str) -> str:
    return {
        "http": "https", "redis": "rediss", "mqtt": "mqtts",
        "tcp": "ssl", "ws": "wss",
    }.get(scheme, "a TLS")


def resolve(
    store: CaStore,
    installed: str | None = None,
    mode: str = TRUST_PINNED,
    require_tls: bool = False,
) -> Trust:
    """Work out what this box trusts, in a fixed order of precedence.

    1. **An installed CA file** (`GSU_CA_FILE`). Provisioned with the image or
       copied on by whoever set the box up. This is the only thing that can be
       trusted for the *first* enrolment call, because until that call returns
       there is no pinned CA — the bootstrap has to come from the installer, out
       of band, or it is not a bootstrap at all.
    2. **The CA persisted from a previous enrolment response.**
    3. Nothing, which is fine on a development box talking plaintext to a local
       broker and fatal to any `https://` or `rediss://` URL.
    """
    if mode not in TRUST_MODES:
        log.warning("Unknown trust mode %r; using %r.", mode, TRUST_PINNED)
        mode = TRUST_PINNED
    if mode == TRUST_SYSTEM:
        log.warning(
            "TLS trust is the system CA bundle, not the platform's pinned CA. "
            "Verification is still on, but any CA the OS trusts will be accepted."
        )
        return Trust(mode=mode, source="system", require_tls=require_tls)

    if installed:
        path = Path(installed)
        try:
            pem = path.read_text()
        except OSError as exc:
            raise Refusal(
                f"GSU_CA_FILE is set to {installed} but it cannot be read: {exc}. "
                "Refusing to start with an unusable trust root rather than "
                "silently falling back to something weaker."
            ) from exc
        if "BEGIN CERTIFICATE" not in pem:
            raise Refusal(f"GSU_CA_FILE at {installed} is not a PEM certificate.")
        return Trust(mode=mode, path=path, source="installed",
                     fingerprint=fingerprint(pem), require_tls=require_tls)

    pem = store.load()
    if pem:
        return Trust(mode=mode, path=store.path, source="enrolment",
                     fingerprint=fingerprint(pem), require_tls=require_tls)
    return Trust(mode=mode, require_tls=require_tls)
