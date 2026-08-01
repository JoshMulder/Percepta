"""Reading a serial port with nothing but the standard library.

Two USB-UARTs are fitted or planned (the Airmar weather head and the ping RX
Pro), and pyserial is a dependency this box would have to carry in its image for
what `termios` already does. Non-blocking throughout: the sensing loop must
never wait on a device that has gone quiet, because a device that has gone quiet
is precisely the case that has to be *reported* rather than waited on.

**Untested against the hardware** — neither UART is connected to this machine.
The termios configuration is standard 8N1 at a selectable baud and the framing
is line-based, but the first thing to check on a real box is that
`ByteSource.read()` returns bytes at all.

Because it is untested, the failures are made as loud and as specific as they
can be. Everything that can go wrong on a first connection has a distinct
message naming the fix: no port set, port missing (with what *is* plugged in
listed), no permission (the `dialout` group), not a serial device at all, or an
unsupported baud. On an unattended box the difference between "no such file" and
"the Airmar is on ttyUSB1 today, not ttyUSB0" is the difference between a fixed
site and a truck.

**Use `/dev/serial/by-id/…`, not `/dev/ttyUSB0`.** Two USB-UARTs enumerate in
whatever order the kernel probed them, and that order changes between boots —
so the weather head and the ADS-B receiver swap places and each driver reads the
other's traffic, which looks like both instruments failing rather than like a
naming problem. `list_ports()` prefers the stable names for exactly this reason.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import termios
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

log = logging.getLogger("gsu.serial")

BAUD = {
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}
# Higher rates exist on some platforms and not others; add whichever this
# kernel's termios actually defines rather than assuming.
for _rate in (230400, 460800, 500000, 921600):
    _symbol = getattr(termios, f"B{_rate}", None)
    if _symbol is not None:
        BAUD[_rate] = _symbol

#: Where the stable, hardware-derived names live. Populated by udev on every
#: mainstream Linux, including Raspberry Pi OS.
BY_ID = Path("/dev/serial/by-id")

#: Fallback globs, in the order a person would try them. `ttyAMA*` is the Pi's
#: own PL011 on the GPIO header; the others are USB-UART bridges.
#:
#: `ttyS*` is deliberately not here. On a PC it matches ten legacy ports that
#: are not connected to anything, and burying two real USB adapters in that list
#: makes the useful answer harder to find. A GPIO-header UART can still be typed
#: in by hand.
TTY_GLOBS = ("ttyUSB*", "ttyACM*", "ttyAMA*")


@dataclass(frozen=True)
class PortInfo:
    """One serial port that exists right now."""

    path: str
    #: The stable `/dev/serial/by-id/…` name, when there is one.
    stable: bool
    #: What it resolves to, so a person can match it against `dmesg`.
    target: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "stable": self.stable, "target": self.target}


def list_ports() -> list[PortInfo]:
    """Every serial port present, stable names first.

    Used by the console's picker, by `gsu devices`, by `gsu preflight` and —
    most usefully — inside the error raised when a configured port is missing,
    so the telemetry that reports the fault also reports the fix.
    """
    ports: list[PortInfo] = []
    seen: set[str] = set()
    try:
        entries = sorted(BY_ID.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        try:
            target = str(entry.resolve())
        except OSError:
            target = ""
        ports.append(PortInfo(str(entry), stable=True, target=target))
        seen.add(target)
    dev = Path("/dev")
    for pattern in TTY_GLOBS:
        try:
            matches = sorted(dev.glob(pattern))
        except OSError:
            continue
        for match in matches:
            if str(match) in seen:
                continue
            ports.append(PortInfo(str(match), stable=False))
    return ports


def _ports_hint() -> str:
    ports = list_ports()
    if not ports:
        return (
            "No serial ports are present at all. Check the USB lead and the "
            "adapter, then `dmesg | tail` and `ls /dev/serial/by-id/`."
        )
    listed = ", ".join(port.path for port in ports[:6])
    return f"Ports present now: {listed}."


class ByteSource(Protocol):
    """Anything that yields bytes: a port, a file of captured traffic, a
    simulation. Drivers take one of these rather than a path, which is what
    makes them testable without hardware."""

    def read(self) -> bytes: ...
    def close(self) -> None: ...


class SerialPort:
    def __init__(self, path: str, baud: int = 4800) -> None:
        self.path = path
        self.baud = int(baud)
        self._check(path)
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError as exc:
            raise PermissionError(
                f"no permission to open {path}. The agent runs as its own user "
                "and needs to be in the 'dialout' group: "
                "`sudo usermod -aG dialout gsu` then restart the service."
            ) from exc
        except OSError as exc:
            raise OSError(f"could not open {path}: {exc}") from exc
        try:
            self._configure()
        except Exception:
            os.close(self._fd)
            raise

    @staticmethod
    def _check(path: str) -> None:
        """Everything that can be wrong before the open, said precisely.

        These messages end up in `unavailable_reason` and on the local console,
        so they are written for whoever is holding the box, not for whoever
        wrote the driver.
        """
        if not path or path in ("None", "none"):
            raise FileNotFoundError(
                "no serial port set for this device. Choose one on the setup "
                f"page. {_ports_hint()}"
            )
        target = Path(path)
        if not target.exists():
            hint = _ports_hint()
            stable = " Prefer a /dev/serial/by-id/… name: ttyUSB numbering " \
                     "changes between boots, and two adapters swap over." \
                     if not path.startswith(str(BY_ID)) else ""
            raise FileNotFoundError(f"no such serial port: {path}. {hint}{stable}")
        try:
            mode = target.stat().st_mode
        except OSError as exc:
            raise OSError(f"cannot stat {path}: {exc}") from exc
        if not stat.S_ISCHR(mode):
            raise OSError(
                f"{path} exists but is not a serial device (it is a "
                f"{'directory' if stat.S_ISDIR(mode) else 'regular file'}). "
                f"{_ports_hint()}"
            )

    def _configure(self) -> None:
        speed = BAUD.get(self.baud)
        if speed is None:
            raise ValueError(
                f"unsupported baud {self.baud}. This build can do: "
                + ", ".join(str(rate) for rate in sorted(BAUD))
            )
        try:
            attrs = termios.tcgetattr(self._fd)
        except termios.error as exc:
            # A path that is a character device but not a tty — /dev/null, a
            # GPIO chip — lands here rather than at the open.
            raise OSError(
                f"{self.path} is not a serial port (termios: {exc}). "
                f"{_ports_hint()}"
            ) from exc
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        # 8N1, no flow control, receiver on, ignore modem control lines.
        cflag = (cflag & ~termios.CSIZE) | termios.CS8
        cflag &= ~(termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
        cflag |= termios.CREAD | termios.CLOCAL
        # Raw: no canonical mode, no echo, no signal characters, no translation
        # of CR/LF — NMEA and MAVLink both care about exact bytes.
        lflag &= ~(termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG)
        iflag &= ~(termios.IXON | termios.IXOFF | termios.IXANY | termios.ICRNL
                   | termios.INLCR | termios.IGNCR)
        oflag &= ~termios.OPOST
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(
            self._fd, termios.TCSANOW,
            [iflag, oflag, cflag, lflag, speed, speed, cc],
        )

    def read(self, size: int = 4096) -> bytes:
        try:
            return os.read(self._fd, size)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            # A USB-UART that has been unplugged reads EIO for ever. Say so once
            # and let the driver report the device as gone.
            log.warning("Serial read on %s failed: %s", self.path, exc)
            raise

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


class FileByteSource:
    """A recorded capture, replayed: the sane way to reproduce a field fault —
    capture the port with `cat /dev/ttyUSB0 > capture.bin` and replay it here.

    Nothing imports this. Kept because a recorded capture is the only way to
    debug a serial device you do not have on your desk, and writing it again
    under pressure is worse than carrying it. Its docstring used to claim the
    tests used it, which was false and is the kind of claim that stops anyone
    checking.
    """

    def __init__(self, path: str | Path, chunk: int = 512, loop: bool = False) -> None:
        self._file = open(path, "rb")
        self._chunk = chunk
        self._loop = loop

    def read(self) -> bytes:
        data = self._file.read(self._chunk)
        if not data and self._loop:
            self._file.seek(0)
            data = self._file.read(self._chunk)
        return data

    def close(self) -> None:
        self._file.close()


class LineAssembler:
    """Complete lines out of arbitrary chunks, holding the partial tail.

    A serial read lands mid-sentence far more often than not, and a reader that
    drops partial lines silently loses roughly half its data.
    """

    def __init__(self, limit: int = 8192) -> None:
        self._buffer = bytearray()
        self._limit = limit

    def feed(self, data: bytes) -> list[str]:
        self._buffer.extend(data)
        if len(self._buffer) > self._limit:
            # Nothing that looks like a line in 8 kB is noise, not a sentence.
            del self._buffer[:-self._limit]
        lines: list[str] = []
        while b"\n" in self._buffer:
            line, _, rest = bytes(self._buffer).partition(b"\n")
            self._buffer = bytearray(rest)
            text = line.decode("ascii", "ignore").strip()
            if text:
                lines.append(text)
        return lines
