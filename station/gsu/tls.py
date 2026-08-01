"""The station's trust roots, and the rules about when it may talk at all.

**There are two of them, and conflating them was a mistake worth naming.**

`contract/enrolment.md` §4 gives the station a `broker.ca_pem` at enrolment:
*"pinned; the station verifies the platform"*. The field is named for the broker
because that is what it is — **the broker's trust root, not the API's**. An
earlier version of this file used it for both, which works only for as long as
the two happen to share a certificate authority, and stops working the moment
the API moves behind a reverse proxy with a public certificate.

So:

| | Trust | Why |
|---|---|---|
| **Broker** (`rediss://`, a private service) | **pinned** to `broker.ca_pem` | A private CA the station explicitly trusts. Nothing else will do |
| **Broker** (`wss://`, the 443 relay) | whichever the platform **states** | The relay is served by the platform's own 443, so it is the API's endpoint wearing a different path — and it inherits the API's answer |
| **Platform API** (`https://`) | **system trust store** by default, pinned only when told | It is expected to sit behind a TLS-terminating proxy with a public certificate on a real domain |

The middle row is the one that changed, and it changed because the deployment
did: pinning is a statement about *an endpoint*, and once the broker moved onto
the same host, port and certificate as the API, insisting it was still a
private service with a private CA described nothing real. It is the platform
that knows which case it is in, so `broker.ca_mode` is how it says.

The pinning on the broker is the stronger statement and it is the one that
matters most: a public trust store says "somebody a browser vendor trusts signed
this", while a pinned private CA says "the CA that issued this station's
identity signed this". On a box on someone else's network behind CGNAT, that
difference is the security boundary.

The API is not weakened by using the system store — a public certificate for a
real domain is verified against a well-audited set of roots, which is what that
set is for. It is weakened by pinning it to a CA that will not be the one
answering, which is what the previous arrangement would have produced.

Three rules survive both, and all three are enforced here rather than left to
the caller's good manners:

**1. Verification is never disabled.** There is no `verify=False` in this
package and no environment variable that produces one. The only knob is *which*
roots to trust.

**2. There is no plaintext fallback.** A station that quietly downgrades to
`redis://` when TLS fails is worse than one that refuses to start, because the
refusal is visible and the downgrade is not — nobody finds out until the traffic
is already on the wire. When TLS cannot be established, this module refuses, the
agent raises a health condition, and the local console says so in words.

**3. The refusal is loud and local.** `Refusal` carries a message written for
whoever is standing in front of the box, not for whoever wrote the code.

The broker's CA is persisted next to the credential, 0600, because a CA that is
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

#: Trust exactly one CA and nothing else. Always the broker's mode
#: (`contract/enrolment.md` §4), and the API's when a CA is configured for it.
TRUST_PINNED = "pinned"

#: Trust the operating system's CA bundle. The API's default, because it is
#: expected behind a proxy with a publicly-signed certificate for a real
#: domain. Still full verification — it is not a way to turn checking off, and
#: there is no third option that is.
TRUST_SYSTEM = "system"

TRUST_MODES = (TRUST_PINNED, TRUST_SYSTEM)

#: URL schemes that carry TLS, across both transports and the API.
TLS_SCHEMES = ("https", "rediss", "mqtts", "ssl", "wss")

#: Substrings in a connection error that mean the handshake was refused rather
#: than the network being down. The two need different words in front of a
#: technician: one is weather, the other is a certificate nobody will notice
#: otherwise. Lives here rather than in either transport because both need it
#: and a second copy would drift.
TLS_FAILURE_MARKERS = (
    "certificate verify failed", "ssl", "tlsv1", "wrong version number",
)


def looks_like_tls_failure(error: str) -> bool:
    return any(marker in error.lower() for marker in TLS_FAILURE_MARKERS)

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
    #: "broker" or "api". Only ever affects the words in a refusal — but those
    #: words are the product: the two links are fixed in completely different
    #: ways, and a message naming the wrong one sends a technician after the
    #: wrong file.
    purpose: str = "broker"

    @property
    def pinned(self) -> bool:
        return self.mode == TRUST_PINNED and self.path is not None

    def describe(self) -> str:
        if self.mode == TRUST_SYSTEM:
            return "system CA bundle (public certificate, not pinned)"
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

    def _no_ca_message(self, url: str, what: str) -> str:
        """The one refusal whose fix differs completely between the two links."""
        if self.purpose == "broker":
            return (
                f"{what} at {url} needs TLS, and this box has no broker CA to "
                "check it against, and the platform did not say to use the "
                "public roots instead. Both arrive in the enrolment response "
                "as broker.ca_pem and broker.ca_mode (contract/enrolment.md "
                "§4), so enrol — or re-enrol, if this box was enrolled against "
                "an older platform that sent neither. GSU_CA_FILE "
                "pre-provisions a CA out of band. It will not connect without "
                "one and it will not fall back to an unverified connection."
            )
        return (
            f"{what} at {url} needs TLS, and GSU_API_CA_FILE was set but its CA "
            "could not be used. Fix or unset it: with it unset the API is "
            "verified against the system CA bundle, which is the right answer "
            "for a platform behind a proxy with a public certificate."
        )

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
                raise Refusal(self._no_ca_message(url, what))
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


def _installed(path_text: str, variable: str) -> tuple[Path, str]:
    """Read a CA file a person put there, or refuse. Never fall back."""
    path = Path(path_text)
    try:
        pem = path.read_text()
    except OSError as exc:
        raise Refusal(
            f"{variable} is set to {path_text} but it cannot be read: {exc}. "
            "Refusing to start with an unusable trust root rather than silently "
            "falling back to something weaker."
        ) from exc
    if "BEGIN CERTIFICATE" not in pem:
        raise Refusal(f"{variable} at {path_text} is not a PEM certificate.")
    return path, pem


def resolve_broker(
    store: CaStore,
    installed: str | None = None,
    require_tls: bool = False,
    stated_mode: str | None = None,
) -> Trust:
    """What the broker is verified against.

    Precedence:

    1. **An installed CA file** (`GSU_CA_FILE`), if someone pre-provisioned one.
       Wins because it was put there deliberately, out of band.
    2. **The CA persisted from the enrolment response** — the normal path. The
       broker's CA is delivered by `broker.ca_pem` and pinned from then on.
    3. **`stated_mode == "system"`** — the platform said, at enrolment, that it
       sits behind a publicly trusted certificate. See below.
    4. Nothing, which is fine on a development box talking plaintext to a local
       broker and refuses any `rediss://` or `wss://` URL.

    ON THE SYSTEM OPTION
    --------------------
    This file used to say there was no system-trust option here on purpose,
    because "the broker is a private service with a private CA". That was true
    of `rediss://` on 6380. It stopped being true when the relay moved the
    broker onto the platform's own 443: the broker is now *the same TLS
    endpoint as the API*, and the reason given at the top of this module for
    why the API uses the system store — it sits behind a proxy holding a public
    certificate for a real domain — applies to it unchanged. **Trust follows
    the endpoint, not the role.**

    Two properties keep this from being the downgrade rule this module exists
    to prevent. It is never *inferred*: a station reaches here only because the
    platform stated `ca_mode: "system"` in a response it received over a
    verified connection, and an absent or unrecognised mode still refuses.
    And it is never a *fallback*: a pinned CA, from either source, always wins,
    so no box that is pinned today can be argued out of it by a later answer.
    """
    if installed:
        path, pem = _installed(installed, "GSU_CA_FILE")
        return Trust(mode=TRUST_PINNED, path=path, source="installed",
                     fingerprint=fingerprint(pem), require_tls=require_tls,
                     purpose="broker")
    pem = store.load()
    if pem:
        return Trust(mode=TRUST_PINNED, path=store.path, source="enrolment",
                     fingerprint=fingerprint(pem), require_tls=require_tls,
                     purpose="broker")
    if stated_mode == TRUST_SYSTEM:
        return Trust(mode=TRUST_SYSTEM, source="platform stated",
                     require_tls=require_tls, purpose="broker")
    return Trust(mode=TRUST_PINNED, require_tls=require_tls, purpose="broker")


def resolve_api(installed: str | None = None, require_tls: bool = False) -> Trust:
    """What the platform API is verified against.

    The system CA bundle by default: the API is expected to sit behind a
    TLS-terminating reverse proxy with a public certificate for a real domain,
    and the public trust store is exactly the right tool for checking one.

    `GSU_API_CA_FILE` pins it instead. That is the correct setting for a
    platform serving its own certificate — the interim arrangement today — and
    for any deployment with no proxy in front of it. It is opt-in because
    pinning the API to a CA that will not be the one answering is not security,
    it is an outage with a certificate error attached.
    """
    if installed:
        path, pem = _installed(installed, "GSU_API_CA_FILE")
        log.info("Platform API pinned to the CA at %s.", path)
        return Trust(mode=TRUST_PINNED, path=path, source="installed",
                     fingerprint=fingerprint(pem), require_tls=require_tls,
                     purpose="api")
    return Trust(mode=TRUST_SYSTEM, source="system", require_tls=require_tls,
                 purpose="api")
