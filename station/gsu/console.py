"""The local setup GUI: four small pages for setting a box up and seeing
what it is.

`contract/enrolment.md` §5 wants a page the box serves on its own network where
a technician enters a code and watches for a green light. §7 wants the device
inventory — which sensors are present and how to reach them. Those are the same
job five minutes apart, done by the same person standing in front of the same
box, so they are one app — split across four pages (Summary, Connection,
Devices, Logging) because on a phone in a paddock one long page buries the
answer under everything that is fine. Every page sits behind the same gate:
the guards in `_handle` run before the router looks at the path, so a new page
can never be an unauthenticated one.

The owner's requirement is that this is enough on its own: a station that comes
up unconfigured must be usable by somebody with a laptop or a phone and **no
terminal**. Enter the code, pick what is fitted in each slot, read back what the
box thinks it is. `python -m gsu` still does all of it for anyone who does have
a terminal, and neither path is the special case.

Three rules it is built to:

**It works with the link down.** Everything it shows is local state, and the
device selection, the parameters and the events all come off the box's own disk.
The moment someone is most likely to be standing in front of it is the moment
the platform is unreachable.

**Configured and detected are shown separately, always.** "An Airmar 110WX
should be on /dev/ttyUSB0" and "there is one there" are different facts, and the
UI never merges them into a tick. A camera that has failed and a camera that was
never fitted look identical in a database and completely different at the site.

**It says what has no source.** If a device cannot provide a field the console
renders — rainfall on an instrument with no rain gauge — that is listed at
selection time, not discovered later by an operator reading 0.0 mm during a
downpour.

**The platform address is not a field here.** There is one platform, its address
is fixed in the environment file, and an installer's job is to confirm the box
is pointed at the right one — not to retype it. It is rendered read-only for
exactly that reason: a typo in a URL somebody can edit at 3pm on a roof is a
station that enrols against nothing and reports no error anybody sees.

Who may reach this page, and for how long, is `setup_access.py` and the
reasoning is all in that module's docstring. What this file owes it:

- every response carries `Cache-Control: no-store` and a CSP that permits no
  frame, no off-box form target and no script beyond the single inline block
  the Devices page carries under a per-response nonce. That script is
  progressive enhancement only — live save buttons, a refreshing datastream
  field, and the camera preview's re-fetch — and every page keeps working
  with it blocked or absent (the preview's click-to-expand is a checkbox,
  not script, for exactly that reason)
- every state-changing POST carries a CSRF token bound to the session cookie
- the `Host` header must be an IP literal, `localhost` or a `.local` name, which
  is what stops a public web page rebinding its own name to this station's
  private address and driving this form from a technician's browser
- request bodies are bounded before they are read: this box has 1 GB of RAM and
  `Content-Length` is attacker-controlled
- no secret is ever rendered back into the HTML. A stored camera password is
  shown as the fact that one is stored, never as its value
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .camera.rtsp import split_credentials
from .devices import registry
from .setup_access import COOKIE_NAME, Gate, is_loopback_host

log = logging.getLogger("gsu.console")

#: A setup form is a few hundred bytes. This is three orders of magnitude of
#: headroom and still small enough that a hostile `Content-Length` cannot make
#: a 1 GB box swap. Read in bounded chunks rather than trusting the header.
MAX_BODY_BYTES = 64 * 1024

#: How often the window is re-checked when nobody is asking. Short enough that
#: "it closes after thirty minutes" is true to the minute, long enough to be
#: free on a Pi 2B.
WATCH_SECONDS = 5.0

#: No frames, no off-box form target, no external anything. Script is allowed
#: only as the one inline block the Devices page carries, keyed by a nonce
#: generated per response (`_headers`) — a stored-XSS payload cannot know it,
#: and no other script source is ever valid. `connect-src 'self'` is what lets
#: that script poll status.json; it permits nothing off-box.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; "
    "frame-ancestors 'none'"
)

STYLE = """
 /* The console's palette, transcribed from server/web/src/styles.css rather
    than approximated - an installer moves between this page and the console,
    and two dark themes that almost match read as one of them being wrong.
    Transcribed, not shared: this page is served by a stdlib HTTP server on a
    box in a paddock and must stay self-contained, so the tokens are copied
    and the comment says where from. The console's Inter/JetBrains arrive via
    its bundle; system-ui and ui-monospace are those fonts' own fallbacks. */
 :root { --bg:#070b0f; --panel:#121a23; --panel-2:#0c1219; --line:#22303c;
         --line-soft:#1a2531; --text:#dde6ed; --muted:#7f929f; --dim:#4f626f;
         --brand:#00a0dc; --brand-dim:#0b7ba7; --accent:#35c48a;
         --warn:#e8b04b; --danger:#ff7a45; }
 body { font: 15px/1.5 system-ui, sans-serif; margin: 0; background: var(--bg);
        color: var(--text); }
 main { max-width: 54rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
 h1 { font-size: 1.35rem; margin: 0 0 .2rem; }
 h2 { font-size: 1rem; margin: 2rem 0 .6rem; color: var(--muted);
      text-transform: uppercase; letter-spacing: .08em; }
 .sub { color: var(--muted); margin: 0 0 1.2rem; }
 .card { background: var(--panel); border: 1px solid var(--line);
         border-radius: .625rem; padding: 1rem 1.1rem; margin-bottom: .9rem; }
 .row { display: flex; justify-content: space-between; gap: 1rem; padding: .3rem 0;
        border-bottom: 1px solid var(--line-soft); }
 .row:last-child { border-bottom: 0; }
 .k { color: var(--muted); }
 .ok { color: var(--accent); } .warn { color: var(--warn); } .bad { color: var(--danger); }
 .muted { color: var(--muted); font-size: .88rem; }
 input[type=text], input[type=password], input[type=number], select {
   font: .95rem system-ui, sans-serif; padding: .45rem .55rem; background: var(--panel-2);
   color: var(--text); border: 1px solid var(--line); border-radius: .375rem;
   min-width: 12rem; }
 input:focus-visible, select:focus-visible { outline: 2px solid var(--brand);
   outline-offset: 1px; }
 input.code { font: 1.2rem ui-monospace, monospace; letter-spacing: .12em; width: 100%;
   box-sizing: border-box; text-transform: uppercase; }
 button { margin-top: .7rem; font: inherit; font-weight: 600; padding: .45rem 1rem;
   border-radius: .375rem; border: 1px solid var(--brand); background: var(--brand);
   color: #03202b; cursor: pointer; }
 button:hover { background: var(--brand-dim); border-color: var(--brand-dim); }
 .msg { padding: .7rem .9rem; border-radius: .375rem; margin-bottom: 1rem;
        border: 1px solid; }
 .msg.bad { background: rgba(255,122,69,.08); border-color: rgba(255,122,69,.35);
            color: var(--danger); }
 .msg.good { background: rgba(53,196,138,.08); border-color: rgba(53,196,138,.35);
             color: var(--accent); }
 .pill { font-size: .78rem; padding: .1rem .5rem; border-radius: 999px; border: 1px solid; }
 .pill.ok { border-color: var(--accent); background: rgba(53,196,138,.1); color: var(--accent); }
 .pill.warn { border-color: var(--warn); background: rgba(232,176,75,.1); color: var(--warn); }
 .pill.bad { border-color: var(--danger); background: rgba(255,122,69,.1); color: var(--danger); }
 .pill.off { border-color: var(--line); background: var(--panel-2); color: var(--muted); }
 .field { display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: center;
          margin: .5rem 0; }
 label { color: var(--muted); font-size: .9rem; }
 ul { margin: .4rem 0 0; padding-left: 1.1rem; color: var(--text); }
 li { padding: .1rem 0; }
 code { color: var(--muted); font-family: ui-monospace, monospace; }
 a { color: var(--brand); }
 /* The page strip, transcribed from the console's settings tabs (.tabs/.tab
    in server/web/src/styles.css) with its spacing vars resolved to rem. Links
    rather than buttons: these are four GET pages, not panels, and they must
    work with no script. The horizontal padding centres the strip over main's
    54rem column on anything wider than it. */
 .tabs { display: flex; gap: .25rem; overflow-x: auto; scrollbar-width: none;
   padding: .5rem max(.75rem, calc((100vw - 54rem) / 2));
   background: var(--panel-2); border-bottom: 1px solid var(--line); }
 .tabs::-webkit-scrollbar { display: none; }
 .tabs a { flex: none; border: 1px solid transparent; border-radius: .375rem;
   color: var(--muted); font-size: .8rem; padding: .5rem .75rem;
   text-decoration: none; white-space: nowrap; }
 .tabs a:hover { color: var(--text); }
 .tabs a.active { color: var(--brand); border-color: var(--line);
   background: rgba(0,160,220,.14); }
 .tabs a:focus-visible { outline: 2px solid var(--brand); outline-offset: -2px; }
 .slot-head { display: flex; justify-content: space-between; align-items: baseline;
              gap: 1rem; }
 /* The Devices page's slot strip: same tab language as the page strip, one
    level down, so "which slot am I on" reads the same way as "which page". */
 .subtabs { background: transparent; border-bottom: 0;
   padding: .25rem 0 .75rem; margin: 0; }
 /* The datastream field: the sensor's own last lines, monospace, bounded.
    A fixed min-height so an empty field reads as "no data", not as a
    missing element. */
 pre.raw { font: .8rem ui-monospace, monospace; color: var(--text);
   background: var(--panel-2); border: 1px solid var(--line-soft);
   border-radius: .375rem; padding: .55rem .7rem; margin: .4rem 0 .6rem;
   min-height: 2.4rem; white-space: pre; overflow-x: auto; }
 button:disabled { opacity: .45; cursor: default; }
 button:disabled:hover { background: var(--brand); border-color: var(--brand); }
 /* The camera preview. Same bounded box as the datastream field; the hidden
    checkbox is the expand state, so the zoom works with scripts blocked —
    :checked pins the label over the whole viewport. */
 .zoom-toggle { display: none; }
 .preview { display: block; margin: .4rem 0 .6rem; }
 .preview img { display: block; max-width: 100%; border: 1px solid var(--line-soft);
   border-radius: .375rem; cursor: zoom-in; }
 .preview > span { display: block; padding: .55rem .7rem; min-height: 1.3rem;
   background: var(--panel-2); border: 1px solid var(--line-soft);
   border-radius: .375rem; font-size: .8rem; }
 .zoom-toggle:checked ~ .preview { position: fixed; inset: 0; z-index: 10;
   margin: 0; display: grid; place-items: center; padding: 1.5rem;
   background: rgba(7,11,15,.94); cursor: zoom-out; }
 .zoom-toggle:checked ~ .preview img { max-width: 100%; max-height: 100%;
   border: 0; cursor: zoom-out; }
 .fixed { color: var(--text); font-family: ui-monospace, monospace; font-size: .9rem;
          word-break: break-all; }
 /* The sign-in, shaped like the console's: a centred card under the brand
    glow, the mark above the wordmark at the console's large brand size. The
    page still names no station: PERCEPTA is what the product is, not which
    box this is. */
 .brand-mark { display: block; width: 3.5rem; height: 3.5rem;
   margin: 0 auto .625rem; }
 .login-wrap { min-height: 100vh; display: grid; place-items: center;
   background: radial-gradient(60% 60% at 50% 38%, rgba(0,160,220,.09) 0%, var(--bg) 70%); }
 .login-card { width: min(22.5rem, calc(100vw - 2rem)); background: var(--panel);
   border: 1px solid var(--line); border-radius: .625rem; padding: 1.75rem;
   display: flex; flex-direction: column; box-sizing: border-box; }
 .brand-word { font-weight: 700; letter-spacing: .18em; font-size: .812rem;
   text-align: center; margin-bottom: 1.375rem; }
 .login-card h1 { font-size: 1rem; font-weight: 600; margin: 0 0 .8rem; text-align: center; }
 .login-card label { font-size: .75rem; margin-bottom: .3rem; }
 .login-card input { width: 100%; box-sizing: border-box; min-width: 0; }
 .login-card button { width: 100%; margin-top: 1.125rem; padding: .55rem 1rem; }
 .login-card .muted { margin-top: .9rem; font-size: .8rem; }
"""

#: The four pages, in the order the strip shows them. The path is the whole
#: identity: the router, the nav and the post-redirects all key on it, so a
#: page cannot be reachable without appearing in the strip or vice versa.
PAGES = {
    "/": "Summary",
    "/connection": "Connection",
    "/devices": "Devices",
    "/logging": "Logging",
}

#: Where each POST goes home to: the page its form lives on, fixed here rather
#: than read from the request, so there is no redirect an attacker can choose.
POST_HOME = {
    "/device": "/devices",
    "/enrol": "/connection",
    "/logout": "/",
}

STATUS_PILL = {
    "present": ("ok", "detected"),
    "stalled": ("warn", "configured, gone quiet"),
    "configured_absent": ("bad", "configured, not detected"),
    "not_fitted": ("off", "not fitted"),
}


def _host_is_addressable(host_header: str | None) -> bool:
    """Whether the `Host:` we were asked for is one this box can legitimately
    be called by.

    The attack this is for is DNS rebinding: a page on the public internet
    resolves its own name to this station's private address and then drives
    these forms from inside a technician's browser, with the browser's own
    network position. It needs a *name*, because it works by changing what a
    name resolves to. An IP literal cannot be rebound, and neither can an mDNS
    `.local` name — those are answered on the link, not by the public DNS.
    """
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):                     # [::1]:8088
        host = host.partition("]")[0][1:]
    else:
        host = host.split(":")[0]
    host = host.lower()
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class Console:
    """The setup page, its socket, and the rules about when that socket exists.

    The socket is not a fixed thing. `host` is what the operator asked for;
    `bound_host` is where it actually is, which is loopback whenever the LAN
    listener is not allowed to exist — no password configured, or the window
    closed. Closing means closing: the port stops answering rather than starting
    to answer 403, because a port that answers is a port somebody enumerates.
    """

    def __init__(
        self,
        agent,
        host: str = "127.0.0.1",
        port: int = 8088,
        *,
        password: str | None = None,
        window_minutes: float = 30.0,
        reopen_path: Path | None = None,
    ) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self.gate = Gate(
            password=password,
            window_minutes=window_minutes,
            reopen_path=reopen_path,
            enrolled=lambda: getattr(agent, "enrolment", None) is not None,
        )
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.bound_host: str | None = None
        #: Why the LAN listener is not up, when it is not. Rendered, so that a
        #: technician who cannot reach the page from a laptop but can over SSH
        #: is told the reason on the page they *can* reach.
        self.demotion_reason: str = ""
        self.message: tuple[str, str] | None = None

    @classmethod
    def from_config(cls, agent, config) -> Console:
        return cls(
            agent, config.setup_host, config.setup_port,
            password=config.setup_password,
            window_minutes=config.setup_window_minutes,
            reopen_path=config.setup_reopen_path,
        )

    # --- lifecycle ------------------------------------------------------

    def _target_host(self) -> tuple[str, str]:
        """Where the listener should be right now, and why not where asked.

        This is the safety property the whole design rests on: **there is no
        return value from this function that puts a socket on a routable
        interface without a password.** It is a function rather than a check at
        start-up so that the window closing takes the socket away again.
        """
        if is_loopback_host(self.host):
            return self.host, ""
        if not self.gate.has_password:
            return "127.0.0.1", (
                f"GSU_SETUP_HOST is {self.host} but no GSU_SETUP_PASSWORD_HASH "
                "is set, so the setup page would have been an unauthenticated "
                "form on a routable interface. It is on loopback instead — "
                "reach it over an SSH tunnel, or set a password and restart."
            )
        if not self.gate.window_open():
            return "127.0.0.1", (
                "The setup window has closed. Reboot the station, or touch "
                "the setup-open file in the state directory, to open it again."
            )
        return self.host, ""

    def start(self) -> None:
        host, reason = self._target_host()
        self.demotion_reason = reason
        if reason:
            # Loud, and a health condition: a station whose setup page is not
            # where the installer was told it would be is a site visit unless
            # somebody is told why, and the log on a box nobody can reach is
            # not where they will be told.
            log.error("Setup page demoted to loopback: %s", reason)
            self._raise_condition("setup.demoted", "warning", reason)
        if not self._bind(host):
            return
        if self.gate.window_minutes <= 0 and not is_loopback_host(host):
            log.warning(
                "GSU_SETUP_WINDOW_MINUTES=0: the setup page on %s will stay "
                "open for as long as this station runs.", host,
            )
        self._watcher = threading.Thread(
            target=self._watch, name="gsu-console-window", daemon=True
        )
        self._watcher.start()

    def _bind(self, host: str) -> bool:
        if self._stop.is_set():
            # The watcher and `stop()` can race at shutdown, and the loser must
            # not be the one that leaves a listening socket behind.
            return False
        console = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            #: A held-open connection holds a thread. On a box with 64 tasks in
            #: its systemd TasksMax, that is a denial of service one telnet away.
            timeout = 20

            def log_message(self, *args):  # noqa: A002
                pass

            def do_GET(self):  # noqa: N802
                console._handle(self, "GET")

            def do_POST(self):  # noqa: N802
                console._handle(self, "POST")

        try:
            server = ThreadingHTTPServer((host, self.port), Handler)
        except OSError as exc:
            # A console that cannot bind must not stop the station working.
            log.warning("Console could not start on %s:%s (%s).", host, self.port, exc)
            return False
        server.daemon_threads = True
        with self._lock:
            self._server = server
            self.bound_host = host
        self._thread = threading.Thread(
            target=server.serve_forever, name="gsu-console", daemon=True
        )
        self._thread.start()
        log.info(
            "Setup page at http://%s:%s%s", host, self.port,
            "" if is_loopback_host(host) else " (password required from the LAN)",
        )
        return True

    def _watch(self) -> None:
        """Move the socket when the rules change.

        Two transitions, and both matter. Closing takes the LAN listener away
        when the window expires. Opening brings it back when somebody creates
        the reopen marker — without which the marker would only take effect at
        the next restart, and "reboot the station to reach the setup page" is
        exactly the site visit this is all trying to avoid.
        """
        while not self._stop.wait(WATCH_SECONDS):
            try:
                target, reason = self._target_host()
                if target == self.bound_host:
                    self.demotion_reason = reason
                    continue
                log.info(
                    "Setup listener moving from %s to %s. %s",
                    self.bound_host, target, reason or "",
                )
                if target == "127.0.0.1":
                    # Cookies do not outlive the door being shut. A laptop left
                    # on the bench must not walk back in when it reopens.
                    self.gate.forget_all()
                self._shutdown_server()
                self.demotion_reason = reason
                self._bind(target)
            except Exception:  # noqa: BLE001 - a watchdog that dies is silent
                log.exception("Setup window check failed; continuing.")

    def _shutdown_server(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            self.bound_host = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def stop(self) -> None:
        self._stop.set()
        self._shutdown_server()

    def _raise_condition(self, ident: str, severity: str, detail: str) -> None:
        health = getattr(self.agent, "health", None)
        raise_condition = getattr(health, "raise_condition", None)
        if raise_condition:
            try:
                raise_condition(ident, severity, detail)
            except Exception:  # noqa: BLE001
                pass

    # --- request handling -------------------------------------------------

    def _handle(self, handler, method: str) -> None:
        """Everything that is decided before a request is looked at."""
        path = urlsplit(handler.path).path

        if not _host_is_addressable(handler.headers.get("Host")):
            return self._deny(handler, 400, "Bad Host header.")

        peer = handler.client_address[0] if handler.client_address else ""
        decision = self.gate.authorise(peer, handler.headers.get("Cookie"))

        if method == "POST" and path == "/login":
            return self._do_login(handler, peer)

        if not decision.allow:
            if decision.login:
                # A plain first view carries no error: nobody has done
                # anything yet, and "password required" in a red box reads as
                # a fault. Reasons appear only on the responses to an actual
                # attempt — wrong password, lockout — which _do_login renders.
                return self._send_html(
                    handler, self._render_login(""), status=decision.status,
                )
            log.warning(
                "Setup page refused a %s from %s: %s", method, peer, decision.reason
            )
            return self._deny(handler, decision.status, decision.reason)

        session = decision.session
        cookie = session.token if decision.set_cookie and session else None

        if method == "GET":
            if path.startswith("/status.json"):
                return self._send_json(handler, self.agent.snapshot(), cookie)
            if path.startswith("/registry.json"):
                return self._send_json(handler, self._registry_json(), cookie)
            if path.startswith("/frame.jpg"):
                return self._send_frame(handler, cookie)
            if path in ("/index.html", "/login"):
                path = "/"
            if path in PAGES:
                slot = None
                nonce = None
                if path == "/devices":
                    # One sub-tab per slot; the query names it and anything
                    # unrecognised lands on the first tab rather than erroring.
                    query = parse_qs(urlsplit(handler.path).query)
                    slot = (query.get("slot") or [""])[0]
                    if slot not in registry.SLOTS:
                        slot = registry.SLOTS[0]
                    nonce = secrets.token_urlsafe(16)
                return self._send_html(
                    handler, self.render(session, path, slot=slot, nonce=nonce),
                    cookie=cookie, nonce=nonce,
                )
            return self._deny(handler, 404, "No such page.")

        # --- POST, which changes something --------------------------------
        if not self._same_origin(handler):
            return self._deny(handler, 403, "Cross-origin request refused.")
        home = POST_HOME.get(path)
        if home is None:
            return self._deny(handler, 404, "No such action.")
        form = self._read_form(handler)
        if form is None:
            return self._deny(handler, 413, "That request was too large.")
        if not self.gate.check_csrf(session, (form.get("csrf") or [""])[0]):
            # Almost always a stale tab rather than an attack, and the wording
            # says so — but it is refused either way.
            log.warning("Setup POST from %s had no valid CSRF token.", peer)
            self.message = ("bad", "That page had gone stale. Reload and try again.")
            return self._redirect(handler, cookie, home)
        try:
            if path == "/device":
                saved = self._set_device(form)
                if saved:
                    # Back to the sub-tab the form lives on. `saved` has been
                    # validated against registry.SLOTS — nothing from the
                    # request reaches the Location header unchecked.
                    home = f"/devices?slot={saved}"
            elif path == "/enrol":
                self._enrol(form)
            else:  # /logout
                self.gate.forget_all()
                self.message = ("good", "Signed out.")
        except Exception as exc:  # noqa: BLE001 - shown to a person
            self.message = ("bad", str(exc))
        self._redirect(handler, cookie, home)

    def _same_origin(self, handler) -> bool:
        """Refuse a POST whose `Origin` is not us.

        Browsers send `Origin` on every form POST, so a missing one is a
        non-browser client — curl, or the update gate — and those are judged by
        the peer address and the CSRF token instead. A *present* and mismatched
        one is a cross-site post and there is no benign version of that.
        """
        origin = handler.headers.get("Origin")
        if not origin:
            return True
        host = handler.headers.get("Host") or ""
        return urlsplit(origin).netloc.lower() == host.strip().lower()

    def _read_form(self, handler) -> dict | None:
        """Read a bounded body. `Content-Length` is attacker-controlled and this
        box has 1 GB of RAM, so the header is a claim and not a permission."""
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None
        body = handler.rfile.read(length)
        if len(body) != length:
            return None
        return parse_qs(body.decode("utf-8", "replace"))

    def _do_login(self, handler, peer: str) -> None:
        if not self._same_origin(handler):
            return self._deny(handler, 403, "Cross-origin request refused.")
        form = self._read_form(handler)
        if form is None:
            return self._deny(handler, 413, "That request was too large.")
        decision = self.gate.login(peer, (form.get("password") or [""])[0])
        if not decision.allow:
            # The password is never echoed, never logged and never put in a
            # redirect. The only thing that comes back is whether it worked.
            return self._send_html(
                handler, self._render_login(decision.reason), status=decision.status,
            )
        self._redirect(handler, decision.session.token if decision.session else None)

    # --- actions --------------------------------------------------------

    def _enrol(self, form: dict) -> None:
        token = (form.get("token") or [""])[0].strip()
        if not token:
            self.message = ("bad", "Enter the code from the platform.")
            return
        enrolment = self.agent.enrol(token)
        # The code itself is not repeated back. It is single-use, but it is
        # also a shared secret that would then be sitting in a browser's
        # rendered page and in whatever is behind the technician on the roof.
        self.message = (
            "good",
            f"Enrolled as {enrolment.site.name}. Telemetry is on its way.",
        )

    def _set_device(self, form: dict) -> str:
        slot = (form.get("slot") or [""])[0]
        if slot not in registry.SLOTS:
            raise ValueError(f"{slot!r} is not a slot on this station.")
        type_id = (form.get("type_id") or [""])[0]
        if type_id and registry.get(type_id) is None:
            raise ValueError(f"{type_id!r} is not a device this station supports.")
        resource = (form.get("resource") or [""])[0] or None
        device = registry.get(type_id) if type_id else None
        previous = self.agent.inventory.fitted.get(slot)
        previous_params = dict((previous.params or {}) if previous else {})
        params: dict = {}
        if device is not None:
            for parameter in device.parameters:
                raw = (form.get(f"p_{parameter.name}") or [""])[0]
                if parameter.type == "bool":
                    params[parameter.name] = raw == "on"
                elif parameter.type == "password":
                    # Blank means "leave it as it was", because blank is what
                    # the form always shows: the stored value is never rendered
                    # back, so an empty box cannot be read as "clear it" without
                    # wiping a working camera's password on every other save.
                    if raw:
                        params[parameter.name] = raw
                    elif previous and previous.type_id == type_id:
                        kept = previous_params.get(parameter.name)
                        if kept:
                            params[parameter.name] = kept
                elif parameter.type == "number" and raw != "":
                    params[parameter.name] = float(raw) if "." in raw else int(raw)
                elif raw != "":
                    params[parameter.name] = raw
        note = ""
        if device is not None and device.connection == "network":
            note = self._strip_url_credentials(form, params)
        self.agent.inventory.set_device(slot, type_id, params, resource)
        # Rebuild immediately: an installer who changes a port expects to see
        # within seconds whether the box can now talk to the thing.
        self.agent.build_devices()
        if slot == "camera" and getattr(self.agent, "_stream_holds_camera",
                                        lambda: False)():
            # The rebuild deliberately leaves the camera alone while the live
            # stream holds the sensor (agent.build_devices); say so rather
            # than reporting the old driver's state as this save's outcome.
            self.message = ("good", f"{slot}: saved. Applies when the live "
                                    f"stream stops.{note}")
            return slot
        report = {r.slot: r for r in self.agent.inventory.report()}[slot]
        if not type_id:
            self.message = ("good", f"{slot}: nothing fitted.")
        elif report.status == "present":
            self.message = ("good", f"{slot}: {report.label} — detected.{note}")
        else:
            self.message = (
                "bad",
                f"{slot}: {report.label} saved, but not detected. "
                f"{report.detail}{note}",
            )
        return slot

    @staticmethod
    def _strip_url_credentials(form: dict, params: dict) -> str:
        """A pasted `rtsp://user:pass@…` never survives to the stored address.

        Camera vendors hand installers the whole line, credentials embedded,
        and this form's address box is where it gets pasted. Refusing it makes
        somebody retype a password on a phone on a roof; storing it as typed
        puts a secret in a plain-text field this page renders back on every
        visit — which is exactly the leak the password field was built never
        to have. So the URL is split: the address is stored without its
        userinfo, and the credentials move into the username and password
        parameters, which are stored once and never echoed. Values typed into
        those fields on the same save win over ones embedded in the URL — the
        separate field is the more deliberate act — and a URL-borne password
        replaces a stored one, because a freshly pasted URL means the paste is
        what the installer believes.

        Returns a sentence for the save message when anything moved.
        """
        address = str(params.get("address") or "")
        if not address:
            return ""
        cleaned, username, password = split_credentials(address)
        if cleaned == address:
            return ""
        params["address"] = cleaned
        if username and not (form.get("p_username") or [""])[0]:
            params["username"] = username
        if password and not (form.get("p_password") or [""])[0]:
            params["password"] = password
        if username or password:
            return (" The URL's credentials moved into the username and "
                    "password fields; the URL is stored without them.")
        return ""

    # --- rendering ------------------------------------------------------

    def _registry_json(self) -> dict:
        return {
            "slots": list(registry.SLOTS),
            "devices": [
                {
                    "id": device.id, "slot": device.slot, "label": device.label,
                    "connection": device.connection, "simulated": device.simulated,
                    "driver": device.driver, "resource": device.resource,
                    "provides": list(device.provides), "absent": list(device.absent),
                    "notes": device.notes,
                    "parameters": [
                        {
                            "name": p.name, "label": p.label, "type": p.type,
                            "default": p.default, "required": p.required,
                            "help": p.help, "choices": list(p.choices),
                        }
                        for p in device.parameters
                    ],
                }
                for device in registry.REGISTRY
            ],
        }

    def _headers(self, handler, status: int, kind: str, length: int,
                 cookie: str | None, nonce: str | None = None,
                 extra: dict | None = None) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", kind)
        handler.send_header("Content-Length", str(length))
        for name, value in (extra or {}).items():
            handler.send_header(name, value)
        # A setup page is state, and every one of these responses names devices,
        # a site and a station id. None of it belongs in a browser cache on a
        # subcontractor's laptop.
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        # same-origin, not no-referrer. Since Chrome 85 the Origin header on a
        # POST follows the referrer policy, so no-referrer redacts it to the
        # literal "null" even on a same-origin form - which _same_origin then
        # rightly refuses, and every browser login 403s while curl (no Origin
        # at all) sails through. same-origin sends nothing to any other site,
        # which for a page with no outbound links is the same privacy, and
        # lets the browser vouch for its own posts.
        handler.send_header("Referrer-Policy", "same-origin")
        # The nonce admits exactly one inline script — the one this response
        # itself carries — and is minted per response, so nothing injected
        # into rendered content can ever name it.
        csp = CSP + (f"; script-src 'nonce-{nonce}'" if nonce else "")
        handler.send_header("Content-Security-Policy", csp)
        if cookie:
            # No `Secure`: this is plain HTTP and always will be — see
            # setup_access.py on why a self-signed certificate here is theatre.
            # HttpOnly and SameSite=Strict are the two that do work.
            handler.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Strict",
            )
        handler.end_headers()

    def _send_frame(self, handler, cookie: str | None) -> None:
        """The newest frame the video publisher took, as the JPEG it is.

        A reader, never a trigger: this serves the publisher's cached frame
        and cannot start a capture, so it cannot contend for a sensor the
        live stream holds — while the stream runs, the cached frame simply
        ages, and the age is stated. Behind the same gate as every page, and
        `no-store` like every response (`_headers`), because the newest frame
        is the only one worth anything.
        """
        video = getattr(self.agent, "video", None)
        frame = getattr(video, "last_frame", None)
        if frame is None:
            return self._deny(handler, 404, "No frame yet.")
        age = video.frame_age_s() or 0.0
        self._headers(handler, 200, "image/jpeg", len(frame.jpeg), cookie,
                      extra={"X-Frame-Age": f"{age:.1f}"})
        handler.wfile.write(frame.jpeg)

    def _send_json(self, handler, payload: dict, cookie: str | None = None) -> None:
        body = json.dumps(payload, indent=2, default=str).encode()
        self._headers(handler, 200, "application/json", len(body), cookie)
        handler.wfile.write(body)

    def _send_html(self, handler, text: str, cookie: str | None = None,
                   status: int = 200, nonce: str | None = None) -> None:
        body = text.encode()
        self._headers(handler, status, "text/html; charset=utf-8", len(body),
                      cookie, nonce)
        handler.wfile.write(body)

    def _deny(self, handler, status: int, reason: str) -> None:
        body = f"{status} {html.escape(reason)}\n".encode()
        self._headers(handler, status, "text/plain; charset=utf-8", len(body), None)
        handler.wfile.write(body)

    def _redirect(self, handler, cookie: str | None = None,
                  location: str = "/") -> None:
        # Only ever one of our own page paths — see POST_HOME. Nothing from
        # the request reaches this header.
        handler.send_response(303)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        if cookie:
            handler.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Strict",
            )
        handler.end_headers()

    def _render_login(self, reason: str) -> str:
        """Deliberately says nothing about the station.

        Not the site name, not whether it is enrolled, not what is fitted.
        Somebody who has reached this page has reached a private network and
        nothing more, and there is no reason to confirm for them which box they
        have found before they can prove they are meant to be here.

        `reason` is non-empty only on the response to a failed attempt or a
        lockout; a first view shows no error, because there is none. The mark
        is a data: URI (gsu/brand.py) so the page stays self-contained.
        """
        from .brand import LOGO_DATA_URI

        return "".join([
            "<!doctype html><meta charset=utf-8>",
            "<meta name=viewport content='width=device-width,initial-scale=1'>",
            "<title>Ground station setup</title>",
            f"<style>{STYLE}</style>",
            "<div class=login-wrap><div class=login-card>",
            f"<img class=brand-mark src='{LOGO_DATA_URI}' alt='' "
            "width=56 height=56>",
            "<div class=brand-word>PERCEPTA</div>",
            "<h1>Ground station setup</h1>",
            f"<div class='msg bad'>{html.escape(reason)}</div>" if reason else "",
            "<form method=post action='/login'>",
            "<label for=password>Setup password</label>",
            "<input id=password name=password type=password autocomplete='off' "
            "autofocus>",
            "<button type=submit>Sign in</button></form>",
            "<div class=muted>The login password can be found on this box's "
            "label, or with whoever provisioned it.</div>",
            "</div></div>",
        ])

    def render(self, session=None, page: str = "/", slot: str | None = None,
               nonce: str | None = None) -> str:
        state = self.agent.snapshot()
        csrf = self.gate.csrf_token(session)
        out = [
            "<!doctype html><meta charset=utf-8>",
            "<meta name=viewport content='width=device-width,initial-scale=1'>",
            f"<title>Ground station — {PAGES.get(page, 'Summary')}</title>",
            f"<style>{STYLE}</style>",
            self._nav(page),
            "<main>",
            "<h1>Ground station</h1>",
        ]
        if self.message:
            kind, text = self.message
            out.append(f"<div class='msg {kind}'>{html.escape(text)}</div>")
            self.message = None

        if page == "/connection":
            out.append(self._section_enrol(state, csrf))
            out.append(self._section_platform(state))
            out.append(self._section_security(state))
            out.append(self._section_access(session, csrf))
        elif page == "/devices":
            slot = slot if slot in registry.SLOTS else registry.SLOTS[0]
            out.append(self._section_devices(state, csrf, slot))
            if slot == "camera":
                out.append(self._section_camera(state))
            if nonce:
                out.append(self._devices_script(nonce))
        elif page == "/logging":
            out.append(self._section_events(state))
        else:
            out.append(self._page_summary(state))
        out.append("</main>")
        return "".join(out)

    @staticmethod
    def _nav(page: str) -> str:
        out = ["<nav class=tabs>"]
        for path, label in PAGES.items():
            active = " class=active" if path == page else ""
            out.append(f"<a href='{path}'{active}>{html.escape(label)}</a>")
        out.append("</nav>")
        return "".join(out)

    @staticmethod
    def _csrf_field(csrf: str) -> str:
        return f"<input type=hidden name=csrf value='{html.escape(csrf)}'>"

    def _page_summary(self, state: dict) -> str:
        """The landing page: what an installer checks before leaving site,
        worst news first, and nothing they can edit — every fix lives on the
        page whose tab names the thing that is wrong."""
        out = []
        if state["enrolled"]:
            out.append(f"<p class=sub>{html.escape(state['station'] or '')}</p>")
        else:
            out.append(
                "<p class=sub>Not set up yet — "
                "<a href='/connection'>enter the enrolment code</a>.</p>"
            )
        if state["health"]:
            out.append("<div class=card><div class=k>Needs attention</div><ul>")
            for condition in state["health"]:
                css = "bad" if condition["severity"] == "critical" else "warn"
                out.append(
                    f"<li class={css}>{html.escape(condition['id'])}: "
                    f"{html.escape(condition['detail'])}</li>"
                )
            out.append("</ul></div>")
        clock_state = state.get("clock_source") or {}
        rows = [
            ("Enrolled", "yes" if state["enrolled"] else "not yet",
             "ok" if state["enrolled"] else "warn"),
            ("Link to the platform", "up" if state["link"] else "down",
             "ok" if state["link"] else "bad"),
            ("Telemetry sent", f"{state['published']} frames", "ok"),
            ("Dropped while offline", f"{state['dropped']} frames",
             "ok" if not state["dropped"] else "warn"),
            ("Station clock", state["clock"], "ok"),
            ("Clock kept by", self._clock_wording(clock_state),
             "ok" if clock_state.get("synchronised") else "warn"),
        ]
        out.append("<div class=card>")
        for label, value, css in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class='{css}'>{html.escape(str(value))}</span></div>"
            )
        out.append("</div>")
        # One line per slot: the same pill the Devices page shows, without the
        # forms. Intent (the label) and fact (the pill), still never merged.
        out.append("<h2>Slots</h2><div class=card>")
        by_slot = {report["slot"]: report for report in state["devices"]}
        for slot in registry.SLOTS:
            report = by_slot[slot]
            css, wording = STATUS_PILL.get(report["status"], ("off", report["status"]))
            out.append(
                f"<div class=row><span class=k>{html.escape(slot)}</span>"
                f"<span>{html.escape(report['label'])} "
                f"<span class='pill {css}'>{html.escape(wording)}</span></span></div>"
            )
        out.append(
            "<div class=muted>Selection and parameters are on the "
            "<a href='/devices'>Devices</a> page.</div></div>"
        )
        return "".join(out)

    def _section_enrol(self, state: dict, csrf: str) -> str:
        if state["enrolled"]:
            return (
                f"<p class=sub>Enrolled as {html.escape(state['station'] or '')}.</p>"
            )
        return (
            "<p class=sub>Not set up yet.</p>"
            "<div class=card><form method=post action='/enrol'>"
            + self._csrf_field(csrf) +
            "<label for=token>Enter the code you were given</label><br>"
            "<input id=token class=code name=token type=text autocomplete=off "
            "placeholder='XXXX-XXXX-XXXX' autofocus>"
            "<button type=submit>Set this station up</button>"
            "</form></div>"
        )

    def _section_security(self, state: dict) -> str:
        """Whether each link is encrypted and verified, and whether the
        credential behind them is still good — in the same list a technician
        checks before leaving site. Separate rows because they have separate
        trust roots, and none of it a question that should need a packet
        capture to answer."""
        security = state.get("security") or {}
        trust = security.get("trust") or {}
        rows = [
            self._security_row(security, trust),
            self._api_security_row(state, security),
            self._credential_row(),
        ]
        out = ["<h2>Security</h2><div class=card>"]
        for label, value, css in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class='{css}'>{html.escape(str(value))}</span></div>"
            )
        out.append("</div>")
        return "".join(out)

    def _credential_row(self) -> tuple[str, str, str]:
        """The station's own credential, which quietly renews itself — and
        which, when renewal is quietly failing, gives weeks of warning that
        only counts if it is written somewhere somebody looks."""
        enrolment = getattr(self.agent, "enrolment", None)
        credential = getattr(enrolment, "credential", None)
        if credential is None:
            return ("Broker credential", "none until this station enrols", "warn")
        when = credential.expires_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        if credential.expired():
            return ("Broker credential",
                    f"EXPIRED {when} — this station must re-enrol", "bad")
        if credential.due_for_renewal():
            return ("Broker credential",
                    f"renewal due now; expires {when}", "warn")
        return ("Broker credential", f"expires {when}, renews itself", "ok")

    def _section_platform(self, state: dict) -> str:
        """The addresses, read-only and said to be read-only.

        There is one platform and its address is fixed in the environment file.
        An installer's job here is to confirm the box is pointed at the right
        one before they leave, which is a different job from being able to
        change it — and one that is worth doing, because an address that is
        wrong produces a station that looks like it has no signal.
        """
        security = state.get("security") or {}
        rows = [
            ("Platform API", state.get("platform") or "not set"),
            ("Broker", security.get("broker_url")
             or "not known until this station enrols"),
            ("Publishing to", state.get("telemetry_topic") or "—"),
            ("Station id", state.get("station_id") or "not enrolled"),
        ]
        out = ["<h2>Where this box talks</h2><div class=card>"]
        for label, value in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class=fixed>{html.escape(str(value))}</span></div>"
            )
        out.append(
            "<div class=muted>Fixed in the station's environment file "
            "(<code>GSU_PLATFORM_URL</code>, <code>GSU_BROKER_URL</code>) and "
            "deliberately not editable here: there is one platform, and a URL "
            "that can be retyped on site is a station that enrols against "
            "nothing and reports no error anybody sees. Check it matches what "
            "you were told, then carry on.</div></div>"
        )
        return "".join(out)

    def _section_camera(self, state: dict) -> str:
        """Why the camera is doing what it is doing, without an SSH session.

        The specific fault this exists for: `picamera2` is a Debian package, a
        virtual environment built without `--system-site-packages` cannot import
        it, and the station silently falls back to one subprocess per frame.
        That is several times slower and presents as "the camera is slow" —
        which is a hardware conversation about a packaging mistake. The driver
        already computes the reason; this puts it where the question is asked.
        """
        video = state.get("video") or {}
        camera = video.get("camera") or {}
        stream = video.get("stream") or {}
        reason = camera.get("backend_reason") or ""
        rows = [
            ("Snapshots", "on" if video.get("enabled") else "off",
             "ok" if video.get("enabled") else "off"),
            ("Frame rate", f"{video.get('fps_measured', 0)} fps measured, "
                           f"{video.get('fps_configured', 0)} configured", "ok"),
            ("Cost on the link", f"{round((video.get('bitrate_bps') or 0) / 1000)} kbit/s, "
                                 f"{video.get('bytes_per_frame') or 0} bytes/frame", "ok"),
            ("Published / dropped",
             f"{video.get('frames_published', 0)} / {video.get('frames_dropped', 0)}",
             "ok" if not video.get("frames_dropped") else "warn"),
            ("Live stream", stream.get("state") or "idle", "ok"),
        ]
        if camera.get("backend"):
            rows.insert(0, ("Capture path", camera["backend"],
                            "ok" if camera["backend"] == "picamera2" else "warn"))
        out = ["<h2>Camera</h2><div class=card>"]
        for label, value, css in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class='{css}'>{html.escape(str(value))}</span></div>"
            )
        if reason:
            css = "muted" if camera.get("backend") == "picamera2" else "warn"
            out.append(f"<div class='{css}'>{html.escape(reason)}</div>")
        if video.get("reason"):
            out.append(
                f"<div class=warn>{html.escape(str(video['reason']))}</div>"
            )
        out.append("</div>")
        return "".join(out)

    @staticmethod
    def _security_row(security: dict, trust: dict) -> tuple[str, str, str]:
        """One line answering "is this link safe to leave running".

        Deliberately blunt in the failure cases. A station that has stopped
        publishing because it will not accept a certificate looks, from every
        other row on this page, exactly like a station with no signal — and the
        two need completely different people called.
        """
        if security.get("tls_failed"):
            return ("Broker security",
                    "REFUSED — the broker's certificate did not verify", "bad")
        if not security.get("publishing") and security.get("broker_url"):
            return ("Broker security", "REFUSED — see the conditions below", "bad")
        if security.get("broker_tls") is None:
            return ("Broker security", "no broker yet", "warn")
        if not security.get("broker_tls"):
            return ("Broker security", "PLAINTEXT — development only", "bad")
        if trust.get("mode") == "system":
            return ("Broker security", "TLS, system CA bundle (not pinned)", "warn")
        fingerprint = (trust.get("fingerprint") or "")[:23]
        return ("Broker security", f"TLS, CA pinned {fingerprint}…", "ok")

    @staticmethod
    def _api_security_row(state: dict, security: dict) -> tuple[str, str, str]:
        """The other half, which has a different trust root and different fixes.

        Shown even though the API is only used at enrolment and renewal: a
        station whose renewal is quietly failing on a certificate has weeks
        before anyone finds out the hard way, and this is where somebody would
        look first.
        """
        api = security.get("api_trust") or {}
        if not security.get("platform_tls"):
            return ("Platform API security", "PLAINTEXT — development only", "bad")
        if api.get("mode") == "system":
            return ("Platform API security", "TLS, public certificate", "ok")
        if not api.get("fingerprint"):
            return ("Platform API security", "pinning asked for, CA unusable", "bad")
        return ("Platform API security",
                f"TLS, CA pinned {(api.get('fingerprint') or '')[:23]}…", "ok")

    @staticmethod
    def _clock_wording(state: dict) -> str:
        source = state.get("source", "unknown")
        wording = {
            "gps": "GPS", "ntp": "NTP", "rtc-only": "a hardware RTC, not synced",
            "none": "nothing — the time is a guess",
            "unknown": "cannot tell",
        }.get(source, source)
        return wording if state.get("rtc_present") else f"{wording} (no RTC fitted)"

    @staticmethod
    def _slot_tabs(active: str) -> str:
        """One sub-tab per slot, same tab language as the page strip. Links,
        not buttons: they must work with no script, and each is a GET."""
        out = ["<nav class='tabs subtabs'>"]
        for slot in registry.SLOTS:
            css = " class=active" if slot == active else ""
            out.append(f"<a href='/devices?slot={slot}'{css}>{html.escape(slot)}</a>")
        out.append("</nav>")
        return "".join(out)

    def _section_devices(self, state: dict, csrf: str, slot: str) -> str:
        # Rendering rule for this page, an owner requirement: labels and short
        # constraints only. Everything that used to be explained on screen —
        # why ports are assigned by-id, why a tuner serves one band, what a
        # device cannot measure and why that matters — lives in the registry
        # and in code comments. The page states facts; the reasoning is here.
        out = ["<h2>What is fitted</h2>", self._slot_tabs(slot)]
        if state["conflicts"]:
            # Not prose: these are faults, in the words an operator acts on.
            out.append("<div class='msg bad'><ul>")
            for conflict in state["conflicts"]:
                out.append(f"<li>{html.escape(conflict)}</li>")
            out.append("</ul></div>")

        resources = state["resources"]
        report = {r["slot"]: r for r in state["devices"]}[slot]
        entry = self.agent.inventory.fitted.get(slot)
        css, wording = STATUS_PILL.get(report["status"], ("off", report["status"]))

        out.append("<div class=card>")
        out.append(
            f"<div class=slot-head><strong>{html.escape(slot)}</strong>"
            f"<span class='pill {css}'>{html.escape(wording)}</span></div>"
        )
        # Intent and fact, on separate lines, always both.
        out.append(f"<div class=muted>selected: {html.escape(report['label'])}</div>")
        if report["detail"]:
            out.append(f"<div class=muted>found: {html.escape(report['detail'])}</div>")
        elif report["configured"]:
            out.append("<div class=muted>found: nothing reported yet</div>")

        if slot == "camera":
            # A picture instead of the datastream lines: the camera's raw tap
            # is capture statistics, and the question an installer is actually
            # asking is "is it pointed at the right thing". The image is the
            # publisher's cached frame (/frame.jpg — never a fresh capture),
            # the nonce'd script re-fetches it, and the checkbox is the whole
            # zoom mechanism: :checked pins the label full-screen, so
            # expanding works with scripts blocked.
            out.append(self._preview(state.get("video") or {}))
        else:
            # The sensor's own last lines — empty when nothing is connected.
            # The nonce'd script refreshes it from status.json; without script
            # it is the state at render time, which is still the truth.
            lines = (state.get("raw_samples") or {}).get(slot) or []
            out.append("<div class=field><label>Data</label></div>")
            out.append(
                f"<pre class=raw id=raw data-slot='{slot}'>"
                + html.escape("\n".join(lines)) + "</pre>"
            )

        out.append(
            f"<form method=post action='/device' data-device>"
            f"<input type=hidden name=slot value='{slot}'>"
        )
        out.append(self._csrf_field(csrf))
        out.append("<div class=field><label>Device</label><select name=type_id>")
        out.append(
            f"<option value=''{' selected' if not report['configured'] else ''}>"
            "— not fitted —</option>"
        )
        for device in registry.by_slot(slot):
            selected = " selected" if entry and entry.type_id == device.id else ""
            suffix = "" if device.driver else "  (no driver in this build)"
            out.append(
                f"<option value='{device.id}'{selected}>"
                f"{html.escape(device.label)}{suffix}</option>"
            )
        out.append("</select></div>")

        selected_device = registry.get(entry.type_id) if entry and entry.type_id else None
        if selected_device is not None:
            for parameter in selected_device.parameters:
                value = (entry.params or {}).get(parameter.name, parameter.default)
                name = f"p_{parameter.name}"
                out.append("<div class=field>")
                out.append(
                    f"<label for='{name}'>{html.escape(parameter.label)}</label>"
                )
                if parameter.type == "bool":
                    checked = " checked" if value else ""
                    out.append(
                        f"<input type=checkbox id='{name}' name='{name}'{checked}>"
                    )
                elif parameter.type == "password":
                    # The one field whose current value is a secret. Never
                    # rendered — not as a value, not in a placeholder. What is
                    # rendered is whether one is stored, which is the fact an
                    # installer needs; blank means "keep it" (see _set_device).
                    stored = bool((entry.params or {}).get(parameter.name))
                    out.append(
                        f"<input type=password id='{name}' name='{name}' "
                        f"value='' autocomplete='new-password' "
                        f"placeholder='{'unchanged' if stored else 'not set'}'>"
                    )
                    out.append(
                        "<span class=muted>"
                        + ("Stored. Blank keeps it." if stored else "Not set.")
                        + "</span>"
                    )
                elif parameter.type == "select":
                    out.append(f"<select id='{name}' name='{name}'>")
                    for choice in parameter.choices:
                        sel = " selected" if str(value) == str(choice) else ""
                        out.append(f"<option{sel}>{html.escape(str(choice))}</option>")
                    out.append("</select>")
                elif parameter.name == "port":
                    # The ports that exist right now are offered; free text is
                    # kept because the device may not be plugged in yet.
                    out.append(
                        f"<input type=text id='{name}' name='{name}' "
                        f"list='ports-{slot}' value='{html.escape(str(value))}' "
                        "placeholder='/dev/serial/by-id/…'>"
                    )
                    out.append(f"<datalist id='ports-{slot}'>")
                    for port in state.get("serial_ports") or []:
                        out.append(
                            f"<option value='{html.escape(port['id'])}'>"
                            f"{html.escape(port['detail'] or port['model'])}</option>"
                        )
                    out.append("</datalist>")
                else:
                    field_type = "number" if parameter.type == "number" else "text"
                    out.append(
                        f"<input type={field_type} id='{name}' name='{name}' "
                        f"value='{html.escape(str(value))}'>"
                    )
                out.append("</div>")

            if selected_device.resource:
                out.append("<div class=field><label>Receiver</label><select name=resource>")
                out.append("<option value=''>— none assigned —</option>")
                for resource in resources:
                    sel = " selected" if entry and entry.resource == resource["id"] else ""
                    label = f"{resource['model']} serial {resource['serial'] or 'unset'}"
                    out.append(
                        f"<option value='{html.escape(resource['id'])}'{sel}>"
                        f"{html.escape(label)}</option>"
                    )
                out.append("</select><span class=muted>One tuner, one band.</span></div>")

            if selected_device.absent:
                out.append(
                    "<div class=muted>No source for: "
                    + html.escape(", ".join(selected_device.absent)) + "</div>"
                )
        # Enabled without script (degradation the design accepts); the nonce'd
        # script disables it until a field differs from its loaded value.
        out.append("<button type=submit>Save</button></form>")
        out.append("</div>")

        connections = {device.connection for device in registry.by_slot(slot)}
        if "serial" in connections:
            ports = state.get("serial_ports") or []
            out.append("<div class=card><div class=k>Serial ports present now</div><ul>")
            if not ports:
                out.append("<li class=warn>none</li>")
            for port in ports:
                out.append(
                    f"<li><code>{html.escape(port['id'])}</code>"
                    + (f" <span class=muted>→ {html.escape(port['detail'])}</span>"
                       if port["detail"] else "")
                    + "</li>"
                )
            out.append(
                "</ul><div class=muted>Use the <code>/dev/serial/by-id/…</code> "
                "name.</div></div>"
            )
        if "usb-sdr" in connections and not resources:
            out.append(
                "<div class=card><div class=muted>No SDR receivers on the USB "
                "bus.</div></div>"
            )
        return "".join(out)

    @staticmethod
    def _preview(video: dict) -> str:
        """The camera preview: latest frame, its age, click to expand."""
        out = [
            "<div class=field><label>Preview</label></div>",
            "<input type=checkbox id=zoom class=zoom-toggle>",
        ]
        if video.get("has_frame"):
            age = video.get("frame_age_s") or 0
            out.append(
                "<label for=zoom class=preview id=preview-wrap>"
                "<img id=preview src='/frame.jpg' alt='latest camera frame'>"
                "</label>"
            )
            out.append(
                f"<div class=muted id=preview-age>frame {age:.0f} s old</div>"
            )
        else:
            out.append(
                "<label for=zoom class=preview id=preview-wrap>"
                "<span class=muted>no frame yet</span></label>"
            )
            out.append("<div class=muted id=preview-age></div>")
        return "".join(out)

    @staticmethod
    def _devices_script(nonce: str) -> str:
        """The one script this app carries, admitted by a per-response nonce.

        Three jobs, all progressive enhancement over a page that already works
        without it: the save button goes disabled until a field differs from
        its loaded value, and the datastream field — or, on the camera tab,
        the frame preview and its age — refreshes from status.json, same auth
        gate as every page, every 2.5 seconds, nothing off-box (the CSP's
        connect-src enforces that). Password fields count as changed when
        non-empty: their loaded value is never in the page to compare
        against, by design. The preview image is re-fetched with a timestamp
        query because the response is no-store and the browser still needs
        the src to change before it asks again.
        """
        script = """
"use strict";
(function () {
  function fingerprint(form) {
    var out = [];
    var fields = form.querySelectorAll("input, select");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === "hidden") continue;
      if (f.type === "checkbox") out.push(f.name + "=" + f.checked);
      else if (f.type === "password") out.push(f.name + "=" + (f.value ? "!" : ""));
      else out.push(f.name + "=" + f.value);
    }
    return out.join("&");
  }
  var forms = document.querySelectorAll("form[data-device]");
  for (var i = 0; i < forms.length; i++) {
    (function (form) {
      var button = form.querySelector("button[type=submit]");
      if (!button) return;
      var loaded = fingerprint(form);
      button.disabled = true;
      var update = function () { button.disabled = fingerprint(form) === loaded; };
      form.addEventListener("input", update);
      form.addEventListener("change", update);
    })(forms[i]);
  }
  var raw = document.getElementById("raw");
  var wrap = document.getElementById("preview-wrap");
  if (raw || wrap) {
    var poll = function () {
      fetch("/status.json", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) {
          if (!s) return;
          if (raw && s.raw_samples) {
            var lines = s.raw_samples[raw.getAttribute("data-slot")] || [];
            raw.textContent = lines.join("\\n");
          }
          if (wrap && s.video && s.video.has_frame) {
            var img = document.getElementById("preview");
            if (!img) {
              wrap.textContent = "";
              img = document.createElement("img");
              img.id = "preview";
              img.alt = "latest camera frame";
              wrap.appendChild(img);
            }
            img.src = "/frame.jpg?t=" + Date.now();
            var age = document.getElementById("preview-age");
            if (age && typeof s.video.frame_age_s === "number") {
              age.textContent = "frame " + Math.round(s.video.frame_age_s) + " s old";
            }
          }
        })
        .catch(function () {});
    };
    setInterval(poll, 2500);
  }
})();
"""
        return f"<script nonce='{nonce}'>{script}</script>"

    def _section_events(self, state: dict) -> str:
        """Read straight off the store rather than the snapshot: the snapshot
        carries fifteen events because it also goes over the wire in
        status.json, and this page is the one place the longer history is
        worth its bytes. The store is built to be read from this thread —
        see the check_same_thread note in store.py."""
        events = self.agent.store.recent_events(100)
        zone = datetime.now().astimezone().tzname() or "local time"
        out = [
            "<h2>Recent events (kept on the box)</h2><div class=card>",
            f"<div class=muted>Newest first, at most 100. Times are the "
            f"station's own, {html.escape(zone)}.</div><ul>",
        ]
        for event in events:
            css = {"critical": "bad", "error": "bad", "warning": "warn"}.get(
                event.severity, ""
            )
            when = event.at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            out.append(
                f"<li class='{css}'><code>{when}</code> "
                f"{html.escape(event.kind)} — {html.escape(event.detail)}</li>"
            )
        if not events:
            out.append("<li>nothing yet</li>")
        out.append("</ul>")
        storage = state["storage"]
        out.append(
            f"<div class=muted>{storage['recordings']} audio recording(s), "
            f"{storage['recordings_mb']} MB; {storage['events']} events stored, "
            f"{storage['events_pending']} not yet sent to the platform.</div>"
        )
        out.append("</div>")
        return "".join(out)

    def _section_access(self, session, csrf: str) -> str:
        """What this page itself is doing, said on the page itself.

        An installer needs to know the door shuts behind them, and the person
        who has to reopen it in six months needs to know how. Both are one
        paragraph, and neither is discoverable from anywhere else on site.
        """
        out = ["<h2>This setup page</h2><div class=card>"]
        where = self.bound_host or "not listening"
        out.append(
            f"<div class=row><span class=k>Listening on</span>"
            f"<span class=fixed>{html.escape(str(where))}:{self.port}</span></div>"
        )
        left = self.gate.seconds_left()
        if left is None:
            closing = (
                "stays open while this station is unenrolled"
                if self.gate.window_minutes > 0 else
                "pinned open by GSU_SETUP_WINDOW_MINUTES=0"
            )
            css = "ok" if self.gate.window_minutes > 0 else "warn"
        else:
            closing = f"closes in {int(left // 60)} min"
            css = "ok"
        out.append(
            f"<div class=row><span class=k>Access window</span>"
            f"<span class='{css}'>{html.escape(closing)}</span></div>"
        )
        if self.demotion_reason:
            out.append(f"<div class=warn>{html.escape(self.demotion_reason)}</div>")
        out.append(
            "<div class=muted>When the window closes this page stops answering "
            "on the local network entirely — it is not a permanent service. "
            "Reboot the station to open it again, or, with a shell on the box, "
            "create the <code>setup-open</code> file in the state directory. "
            "Loopback keeps working over an SSH tunnel either way.</div>"
        )
        if session is not None and session.scope == "local":
            out.append(
                "<form method=post action='/logout'>"
                + self._csrf_field(csrf)
                + "<button type=submit>Sign out</button></form>"
            )
        out.append("</div>")
        return "".join(out)
