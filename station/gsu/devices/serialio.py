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
"""

from __future__ import annotations

import errno
import logging
import os
import termios
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
        if not Path(path).exists():
            raise FileNotFoundError(f"no such serial port: {path}")
        self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._configure()

    def _configure(self) -> None:
        speed = BAUD.get(self.baud)
        if speed is None:
            raise ValueError(f"unsupported baud {self.baud}")
        attrs = termios.tcgetattr(self._fd)
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
    """A recorded capture, replayed. Used by the tests, and the sane way to
    reproduce a field fault: capture the port, replay it here."""

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
