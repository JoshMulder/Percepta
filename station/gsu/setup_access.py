"""Who may reach the setup page, from where, and for how long.

`console.py` is the setup GUI. This module is the only thing standing between it
and the internet, so the reasoning lives here rather than being spread through
the request handler.

The problem in one sentence: the station is a box at the far end of a satellite
link, and the page that configures it is an unauthenticated HTML form. Every
class of device that has ever been mass-compromised had exactly that shape. So
four separate controls have to fail before that page is exposed, and each one is
a default rather than a setting somebody remembers to turn on.

**1. Where the socket is.** `GSU_SETUP_HOST` still defaults to `127.0.0.1`, and
a station left alone is reachable only through an SSH tunnel, exactly as before.
Binding it anywhere else is a deliberate edit to the environment file.

**2. A password, or no LAN socket at all.** If `GSU_SETUP_HOST` names anything
other than loopback and no password is configured, the console **refuses to bind
there** and falls back to loopback with a critical health condition. This is the
structural version of "the default must be safe on a public address": there is
no code path that opens a listener to a non-loopback interface without a secret,
so forgetting to set one produces an unreachable page, not an open one.

**3. The source address.** Even bound to `0.0.0.0` — which is the practical
answer on a box whose setup-network address comes from DHCP — a request from
outside the local networks below is refused before it is parsed. Note what is
*not* in that list: `100.64.0.0/10`. Python's `ipaddress.is_private` counts
carrier-grade NAT as private, and on a Starlink site that range is the carrier's
shared network and not this site's LAN. Using the stdlib predicate here would
have quietly admitted every other subscriber behind the same CGNAT pool.

**4. A window that closes.** The listener is not a permanent fixture. It is open
while the station is unenrolled — an unenrolled station does nothing else, and
is useless to an attacker who cannot also enrol it — and for a bounded idle
period after that. When the window closes the LAN socket is genuinely closed and
rebound to loopback, so the port stops answering rather than starting to answer
403. Getting it back is a deliberate act: reboot the station, or create the
reopen marker file.

What this does **not** do, stated plainly rather than left to be discovered: the
setup page is plain HTTP. A station's setup network address is a DHCP lease, no
certificate can be issued for it, and a self-signed certificate an installer
click-throughs is authentication theatre. So anyone already on the setup network
can read the password off the wire. The controls above are what bounds that: the
window is short, the password is per-box, and the setup network is meant to be a
cable or a dedicated AP rather than the site LAN. If the setup page ever has to
live on a shared network, this is the assumption that has to be revisited first.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

log = logging.getLogger("gsu.setup")

COOKIE_NAME = "gsu_setup"

#: Networks a technician's laptop or phone can plausibly be on when it is
#: plugged into, or associated with, this box. Everything else is the internet.
#:
#: Deliberately hand-written rather than `ipaddress.is_private` — see the module
#: docstring for the carrier-grade NAT range that predicate would have let in.
LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",        # RFC 1918
        "172.16.0.0/12",     # RFC 1918
        "192.168.0.0/16",    # RFC 1918
        "169.254.0.0/16",    # link-local: a laptop on a cable with no DHCP
        "fc00::/7",          # IPv6 unique local
        "fe80::/10",         # IPv6 link-local
    )
)

#: PBKDF2 rounds. Chosen for the *slowest* box this runs on, not the fastest:
#: about a second of a Pi 2B's 900 MHz core, which is a rate limit in its own
#: right on a form nobody submits more than twice. It burns that second in the
#: console thread; the sensing loop is a separate thread and tolerates it.
ITERATIONS = 120_000

#: Failed attempts from one peer before it is locked out, and for how long.
#: Loopback is never locked out — it is already authenticated by SSH, and
#: locking it would let a passer-by on the LAN break the SSH recovery path.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 900.0
FAILURE_WINDOW_SECONDS = 900.0

#: Failures across *every* source before the console locks entirely.
#:
#: The per-peer counter above is defeated by rotating the source address,
#: which on a LAN costs nothing — so on its own it bounds a careless attacker
#: and not a deliberate one. Deliberately much looser than the per-peer limit:
#: this one can lock out the person who is actually standing at the station,
#: and the window is short for that reason. What it bounds is a guessing rate,
#: not a single wrong password.
MAX_FAILURES_ALL_SOURCES = 30

#: More than this and something is enumerating rather than typing.
MAX_SESSIONS = 8

#: Idle session lifetime when the access window is pinned open
#: (`GSU_SETUP_WINDOW_MINUTES=0`). The shipped default window, because that is
#: the length of time this system already treats as a reasonable working
#: session — and long enough that somebody configuring a device, walking to the
#: mast and coming back is still logged in.
PINNED_IDLE_MINUTES = 30.0


# --- where a request came from -------------------------------------------


def classify(address: str | None) -> str:
    """`loopback`, `local` or `public` for a peer address.

    Anything unparseable is `public`. That is the safe direction: a source
    address this cannot read is not one it should trust.
    """
    if not address:
        return "public"
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return "public"
    # ::ffff:192.168.1.5 — a v4 peer on a dual-stack socket, which is what a
    # bind to :: produces. Judged as the v4 address it actually is.
    if getattr(parsed, "ipv4_mapped", None) is not None:
        parsed = parsed.ipv4_mapped
    if parsed.is_loopback:
        return "loopback"
    if any(parsed in network for network in LOCAL_NETWORKS):
        return "local"
    return "public"


def is_loopback_host(host: str | None) -> bool:
    """Whether a *bind* address is loopback-only.

    An empty host and `0.0.0.0`/`::` are not: they are every interface, which
    on this box includes the one facing the satellite terminal.
    """
    if host is None:
        return False
    if host in ("localhost",):
        return True
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    return parsed.is_loopback


# --- passwords ------------------------------------------------------------


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    """`pbkdf2_sha256:iterations:salt:hash`, all hex.

    Written by `python -m gsu setup-password` and pasted into the environment
    file, so that the file the installer image ships does not carry a password
    an image-wide compromise could read straight off.

    Colons, not the conventional dollars, because **docker compose interpolates
    `$VAR` inside env_file values**. A hex salt beginning with a letter is a
    valid shell variable name, so `$b91b7ad…` expanded to nothing and the
    container received a hash with its salt silently removed - every login
    refused, with the file on disk perfectly correct and systemd stations
    unaffected because systemd reads EnvironmentFile literally. It cost an
    evening. Roughly two hex salts in five start with a letter, so it was a
    coin-flip bug that would have looked like a bad password on a box in a
    paddock. `$` hashes are still read (see verify_password) - old ones keep
    working, on the path where they already did.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256:{iterations}:{salt.hex()}:{digest.hex()}"


def verify_password(spec: str | None, password: str) -> bool:
    """Check a password against a stored hash, or against a plain value.

    A plain value is accepted because `.env` is written 0600 by bootstrap and
    an operator who sets one has not done anything unreasonable. It is not
    the recommendation: a hash costs one command and survives the file being
    read, which a plain one does not.
    """
    if not spec or not password:
        return False
    if not spec.startswith("pbkdf2_sha256"):
        return hmac.compare_digest(spec, password)
    # Both separators: `:` is what we write now, `$` is what boxes provisioned
    # before the compose-interpolation discovery still carry.
    separator = spec[len("pbkdf2_sha256")]
    if separator not in ":$":
        return hmac.compare_digest(spec, password)
    try:
        _, rounds, salt_hex, expected = spec.split(separator, 3)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, TypeError):
        # A malformed hash must fail closed. Returning True on an unreadable
        # spec would turn a typo in the environment file into an open door.
        log.error("GSU_SETUP_PASSWORD_HASH is malformed; no login can succeed.")
        return False
    return hmac.compare_digest(digest.hex(), expected)


# --- sessions -------------------------------------------------------------


@dataclass
class Session:
    token: str
    scope: str
    peer: str
    created: float
    last_seen: float
    #: Whether this session has proved it knows the password. Loopback callers
    #: get a session without one, purely so that they have a CSRF token — see
    #: `Gate.authorise`.
    authenticated: bool = False

    def csrf(self, key: bytes) -> str:
        return hmac.new(key, self.token.encode(), hashlib.sha256).hexdigest()


@dataclass
class Decision:
    """What the handler should do with one request."""

    allow: bool
    session: Session | None = None
    status: int = 200
    reason: str = ""
    #: Render the login form rather than the page.
    login: bool = False
    set_cookie: bool = False


@dataclass
class Gate:
    """The access rules, and the window, as one object the console can ask.

    Holds no request state and no rendering. It is separated from `console.py`
    so that the rules can be tested without an HTTP server, which is the only
    way anyone will keep testing them.
    """

    password: str | None = None
    window_minutes: float = 30.0
    reopen_path: Path | None = None
    #: Callable returning whether the station has a credential. The window does
    #: not run down while the answer is False: a station that is not yet enrolled
    #: has nothing worth reaching and an installer who is still working on it.
    enrolled: object = None

    _sessions: dict = field(default_factory=dict)
    _failures: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _csrf_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    _deadline: float = 0.0
    _closed: bool = False

    def __post_init__(self) -> None:
        self._deadline = time.monotonic() + max(0.0, self.window_minutes) * 60

    # --- the window -------------------------------------------------------

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    def _is_enrolled(self) -> bool:
        try:
            return bool(self.enrolled()) if callable(self.enrolled) else False
        except Exception:  # noqa: BLE001 - a status probe must not close the door
            return False

    def window_open(self) -> bool:
        """Whether the LAN listener should exist at all, right now."""
        if self.window_minutes <= 0:
            return True          # explicitly pinned open; warned about at start
        if not self._is_enrolled():
            return True
        if self._consume_reopen_marker():
            return True
        return time.monotonic() < self._deadline

    def refresh(self) -> None:
        """Restart the idle countdown. Called on authenticated activity only —
        an unauthenticated poll from the LAN must not be able to hold the door
        open indefinitely."""
        self._deadline = time.monotonic() + max(0.0, self.window_minutes) * 60

    def _consume_reopen_marker(self) -> bool:
        """`touch $GSU_HOME/setup-open` reopens the window, once.

        The deliberate act the brief asks for, in the form available to someone
        who already has a shell on the box. It is removed as it is honoured, so
        a marker left behind by a script does not become a permanent hole.
        """
        path = self.reopen_path
        if path is None or not path.exists():
            return False
        try:
            os.unlink(path)
        except OSError:
            pass
        log.warning("Setup window reopened by %s.", path)
        self.refresh()
        return True

    def seconds_left(self) -> float | None:
        if self.window_minutes <= 0 or not self._is_enrolled():
            return None
        return max(0.0, self._deadline - time.monotonic())

    # --- lockout ----------------------------------------------------------

    def locked_for(self, peer: str) -> float:
        """Seconds this peer must wait. The longer of two independent limits.

        Both are consulted every time, and that is the whole point: the
        per-peer limit answers 0 for an address it has never seen, which is
        exactly the address a rotating attacker is using. Returning early on
        it — which this did — meant the global counter was unreachable in the
        one case it exists for.
        """
        per_peer = 0.0
        with self._lock:
            entry = self._failures.get(peer)
            if entry:
                count, last = entry
                if time.monotonic() - last > FAILURE_WINDOW_SECONDS:
                    self._failures.pop(peer, None)
                elif count >= MAX_FAILURES:
                    per_peer = max(
                        0.0, LOCKOUT_SECONDS - (time.monotonic() - last))
        # Per-source alone is defeated by rotating the source, which on a LAN
        # is free. The whole-console counter is what actually bounds guessing;
        # it is deliberately looser, because one address locking everybody out
        # of a station's setup page is a worse outcome than a slow guess.
        return max(per_peer, self._global_lockout())

    def note_failure(self, peer: str) -> None:
        now = time.monotonic()
        with self._lock:
            # Expired entries go on every failure. Unbounded growth is a real
            # concern in a 384 MB container (docker-compose.yml sets
            # `mem_limit`), and the thing that grows it is the same rotation
            # that defeats a per-source lockout — so the two are fixed
            # together rather than one being left as the price of the other.
            self._failures = {
                address: entry for address, entry in self._failures.items()
                if now - entry[1] <= FAILURE_WINDOW_SECONDS
            }
            count, last = self._failures.get(peer, (0, 0.0))
            if now - last > FAILURE_WINDOW_SECONDS:
                count = 0
            self._failures[peer] = (count + 1, now)
        log.warning("Setup login failed from %s.", peer)

    def _global_lockout(self) -> float:
        """Seconds left on the whole-console lockout, across every source."""
        now = time.monotonic()
        with self._lock:
            recent = [last for _, last in self._failures.values()
                      if now - last <= FAILURE_WINDOW_SECONDS]
            total = sum(count for count, last in self._failures.values()
                        if now - last <= FAILURE_WINDOW_SECONDS)
        if total < MAX_FAILURES_ALL_SOURCES or not recent:
            return 0.0
        return max(0.0, LOCKOUT_SECONDS - (now - max(recent)))

    def note_success(self, peer: str) -> None:
        with self._lock:
            self._failures.pop(peer, None)

    # --- sessions ---------------------------------------------------------

    def _new_session(self, scope: str, peer: str, authenticated: bool) -> Session:
        now = time.monotonic()
        session = Session(
            token=secrets.token_urlsafe(32), scope=scope, peer=peer,
            created=now, last_seen=now, authenticated=authenticated,
        )
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                self._sessions.pop(oldest.token, None)
            self._sessions[session.token] = session
        return session

    def _lookup(self, cookie_header: str | None) -> Session | None:
        token = _cookie_value(cookie_header)
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if time.monotonic() - session.last_seen > self._idle_limit_s():
                self._sessions.pop(token, None)
                return None
            session.last_seen = time.monotonic()
            return session

    def _idle_limit_s(self) -> float:
        """How long a logged-in session survives without activity.

        Tied to the access window because the two are the same intent: the
        window says how long this page is reachable, and there is no point
        holding a session past it.

        Except at zero, which is where this went wrong. `GSU_SETUP_WINDOW_
        MINUTES=0` means *pinned open* — the strongest possible statement that
        the page should stay usable — and the old `max(window_minutes, 1.0)`
        read it as the shortest possible session instead, logging people out
        after sixty seconds on exactly the boxes configured never to close.
        The floor was there to stop a tiny window making sessions unusable and
        it was applied to the one value that does not mean a tiny window.

        Zero now gets the shipped default instead. Not unlimited: a browser
        left open on a bench is still a way in, and the pin is about the socket
        staying bound, not about never having to log in again.
        """
        if self.window_minutes <= 0:
            return PINNED_IDLE_MINUTES * 60
        return max(self.window_minutes, 1.0) * 60

    def forget_all(self) -> None:
        """Drop every session. Used when the window closes, so that a cookie
        held by a laptop still on the bench does not survive into the next time
        the door is opened."""
        with self._lock:
            self._sessions.clear()

    def csrf_token(self, session: Session | None) -> str:
        return session.csrf(self._csrf_key) if session else ""

    def check_csrf(self, session: Session | None, value: str | None) -> bool:
        if session is None or not value:
            return False
        return hmac.compare_digest(self.csrf_token(session), value)

    # --- the decision -----------------------------------------------------

    def authorise(self, peer_address: str, cookie_header: str | None) -> Decision:
        """One request's worth of judgement, before anything is parsed.

        Loopback keeps the behaviour it has always had — no password, because
        reaching loopback already required SSH to this box and adding a second
        secret in front of the first protects nothing. It still gets a session,
        because it still needs a CSRF token: an SSH tunnel puts this page inside
        a technician's ordinary browser, where any other tab can post to it.
        """
        scope = classify(peer_address)
        if scope == "public":
            return Decision(False, status=403, reason="not a local address")

        if scope == "loopback":
            session = self._lookup(cookie_header)
            if session is None or session.scope != "loopback":
                return Decision(
                    True, self._new_session("loopback", peer_address, True),
                    set_cookie=True,
                )
            return Decision(True, session)

        # --- from the LAN ---
        if not self.has_password:
            # Should be unreachable: with no password the console never binds
            # anywhere a LAN peer can reach. Kept as a belt on the braces.
            return Decision(False, status=403, reason="no setup password is set")
        if not self.window_open():
            return Decision(False, status=403, reason="the setup window is closed")

        session = self._lookup(cookie_header)
        if session is not None and session.authenticated and session.scope == "local":
            self.refresh()
            return Decision(True, session)
        return Decision(False, status=401, login=True, reason="password required")

    def login(self, peer_address: str, password: str) -> Decision:
        """Check a password and, if it is right, start a session.

        The window is checked here as well as in `authorise`, and the ordering
        was backwards: `login` called `refresh()` on success, so a correct
        password *extended* a window that had not been checked. The socket is
        supposed to be gone by then — `_watch` rebinds every 5s — so it was a
        five-second race per cycle rather than a hole, but a window that a
        login can reopen is not a window.
        """
        scope = classify(peer_address)
        if scope == "public":
            return Decision(False, status=403, reason="not a local address")
        if not self.window_open():
            return Decision(False, status=403, reason="the setup window is closed")
        wait = self.locked_for(peer_address)
        if wait > 0:
            return Decision(
                False, status=429, login=True,
                reason=f"too many attempts; try again in {int(wait // 60) + 1} minutes",
            )
        if not verify_password(self.password, password):
            self.note_failure(peer_address)
            return Decision(False, status=401, login=True, reason="wrong password")
        self.note_success(peer_address)
        self.refresh()
        log.info("Setup login from %s.", peer_address)
        return Decision(
            True, self._new_session("local", peer_address, True), set_cookie=True,
        )


def _cookie_value(header: str | None) -> str | None:
    if not header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(header)
    except CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else None
