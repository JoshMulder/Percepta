#!/usr/bin/env python3
"""The privileged host-shell helper: a host PTY, bridged to the platform.

This runs in the `hostshell` helper container (`deploy/hostshell.Dockerfile`,
behind an off-by-default compose profile), which is `pid: host` and holds
CAP_SYS_ADMIN for one reason: to `nsenter` into the host's init and open a
**host** shell — the thing the sandboxed agent cannot and must not do. It is the
most dangerous component in the station, so it does as little as possible and
only when told.

It does NOT talk to the platform on its own. The agent — which holds the
credential and the command channel — writes a request into the shared handoff
volume when a platform admin asks (`gsu/transport/host_shell.py`), the same
handoff shape the updater uses. This watches that file: while a request is open
and its deadline is in the future, it opens a PTY on the host and connects a
socket outward to the platform's `/host/ingest`, carrying the credential the
agent handed it; when the request closes or lapses, it kills the PTY.

Wire (mirrors the platform's `realtime/host.py`, which forwards it verbatim):

    binary   PTY bytes, both directions (output up, keystrokes down)
    text     {"t":"resize","rows":R,"cols":C}   from the terminal

The WebSocket client is `websocket.py`, copied verbatim from the agent's own
(`gsu/media/websocket.py`) so the helper stays a handful of files with no pip
dependency — it is stdlib-only and has no relative imports.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time

import websocket

logging.basicConfig(
    level=logging.INFO, format="%(levelname)-5.5s [hostshell] %(message)s"
)
log = logging.getLogger("hostshell")

#: The handoff directory the agent writes into, shared as a volume. The default
#: is the container path the compose file mounts it at.
HANDOFF_DIR = os.environ.get("GSU_HOST_SHELL_DIR", "/handoff")
REQUEST_FILE = os.path.join(HANDOFF_DIR, "hostshell.json")

#: How often the request file is reconsidered. It moves in whole seconds (a
#: lease deadline), so a slow poll is free.
POLL_S = 1.0

#: The least time between connect attempts, so an unreachable platform is not
#: hammered while a request is open.
REOPEN_INTERVAL_S = 5.0

#: Enter the host's namespaces from PID 1 (the host init, visible because the
#: container is `pid: host`): mount, uts, ipc, net and pid. This is what makes
#: the shell a *host* shell rather than a shell in this container.
NSENTER = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"]

#: The shell to run on the host. Overridable, because a host is not guaranteed to
#: have bash — though the station boxes (Debian/Raspberry Pi OS) do.
SHELL = os.environ.get("GSU_HOST_SHELL_CMD", "").split() or ["bash", "-l"]

#: How much PTY output to read at once. Small: a terminal's bursts are tiny, and
#: the platform's frame cap is generous headroom above this.
READ_CHUNK = 65536


class Session:
    """One host PTY and the socket carrying it to the platform."""

    def __init__(self, url: str, secret: str) -> None:
        self.url = url
        self.secret = secret
        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self.socket: websocket.WebSocket | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        """Open the PTY, spawn the host shell, and connect. Raises on failure."""
        master, slave = pty.openpty()
        self.master_fd = master
        # `os.setsid` in the child makes the slave its controlling terminal, so
        # job control and window size behave; the child's std streams are the
        # slave, and the parent drives the master.
        self.proc = subprocess.Popen(
            NSENTER + SHELL,
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=os.setsid, close_fds=True,
        )
        os.close(slave)
        self.socket = websocket.WebSocket(
            self.url,
            headers={"Authorization": f"Bearer {self.secret}",
                     "User-Agent": "percepta-hostshell"},
            on_message=self._on_message,
            what="the host shell",
        )
        self.socket.connect()
        threading.Thread(target=self._pump_pty, name="pty", daemon=True).start()

    def alive(self) -> bool:
        return (
            not self._closed.is_set()
            and self.socket is not None and self.socket.connected
            and self.proc is not None and self.proc.poll() is None
        )

    # --- platform -> PTY -------------------------------------------------

    def _on_message(self, opcode: int, payload: bytes) -> None:
        if opcode == websocket.OP_BINARY:
            self._write_pty(payload)
        elif opcode == websocket.OP_TEXT:
            self._control(payload)

    def _control(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return
        if isinstance(message, dict) and message.get("t") == "resize":
            self._resize(int(message.get("rows", 24)), int(message.get("cols", 80)))

    def _resize(self, rows: int, cols: int) -> None:
        if self.master_fd is None:
            return
        # A garbled terminal is what a wrong window size looks like, so this is
        # not cosmetic: full-screen programs (less, vim, top) need it right.
        winsize = struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def _write_pty(self, data: bytes) -> None:
        if self.master_fd is None:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            self.close()

    # --- PTY -> platform -------------------------------------------------

    def _pump_pty(self) -> None:
        while not self._closed.is_set():
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 1.0)
            except (OSError, ValueError):
                break
            if not ready:
                if self.proc is not None and self.proc.poll() is not None:
                    break  # the shell exited (the operator typed `exit`)
                continue
            try:
                chunk = os.read(self.master_fd, READ_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            if self.socket is None or not self.socket.send_binary(chunk):
                break
        self.close()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        # Kill the whole process group: the login shell and anything it started.
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        if self.socket is not None:
            self.socket.close("the host session ended")
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


def _read_request() -> dict | None:
    try:
        with open(REQUEST_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _wanted(request: dict | None) -> bool:
    """Whether a host session should be up right now.

    Open, not yet past its deadline, and carrying both a URL and a credential —
    the deadline is the time-box, enforced here so a request the agent stops
    renewing lapses on its own even if a `host.close` never arrives.
    """
    return bool(
        request
        and request.get("open")
        and float(request.get("deadline", 0)) > time.time()
        and request.get("url")
        and request.get("secret")
    )


def main() -> None:
    log.info("Host shell helper watching %s.", REQUEST_FILE)
    session: Session | None = None
    last_attempt = float("-inf")
    while True:
        request = _read_request()
        if _wanted(request):
            if session is not None and not session.alive():
                session.close()
                session = None
            if session is None and time.monotonic() - last_attempt >= REOPEN_INTERVAL_S:
                last_attempt = time.monotonic()
                candidate = Session(request["url"], request["secret"])
                try:
                    candidate.start()
                    session = candidate
                    log.info("Host session open to %s.", request["url"])
                except Exception as exc:  # noqa: BLE001 - retried next tick
                    log.warning("Could not open the host session: %s", exc)
                    candidate.close()
        elif session is not None:
            log.info("Host session request closed or lapsed; ending it.")
            session.close()
            session = None
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
