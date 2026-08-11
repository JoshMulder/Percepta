"""Host device stats for the local Summary page — CPU, memory, temperature and
uptime — read from ``/proc`` and ``/sys``, refreshed once per sensing tick.

The station always runs on a Linux host (a Raspberry Pi in the field), so these
paths are the source rather than a dependency like psutil, and nothing here
shells out: ``/proc`` and ``/sys`` are readable from inside the container,
whereas ``vcgencmd`` and the firmware mailbox are not, so temperature comes from
the thermal zone. Every field is best-effort — on a host missing one (a non-Pi
dev box, a locked-down container) it is simply omitted, so the Summary page
shows what it can and never breaks over an absent sensor.
"""

from __future__ import annotations

import os
from pathlib import Path


class SystemStats:
    """Sampled on the sensing thread (:meth:`sample`), read on the console
    thread (:meth:`read`).

    CPU busy fraction is a delta between samples, so it is taken on the tick
    that already runs every second rather than computed from two reads on the
    console side — that keeps a fresh value ready the moment a page loads,
    instead of the first load showing nothing while it waits for a baseline.
    :meth:`read` just returns the last sampled dict; a single reference
    assignment hands it between threads, so no lock is needed.
    """

    def __init__(self) -> None:
        self._cached: dict = {}
        self._last_cpu: tuple[int, int] | None = None  # (idle, total) jiffies

    def read(self) -> dict:
        """The most recent sample. Empty until the first tick has run, and any
        field the host could not supply is absent rather than null."""
        return self._cached

    def sample(self) -> None:
        stats: dict = {}
        cpu = self._cpu_percent()
        if cpu is not None:
            stats["cpu_percent"] = cpu
        load = _load_1m()
        if load is not None:
            stats["load_1m"] = load
        temp = _temperature_c()
        if temp is not None:
            stats["temperature_c"] = temp
        uptime = _uptime_s()
        if uptime is not None:
            stats["uptime_s"] = uptime
        memory = _memory()
        if memory:
            stats["memory"] = memory
        self._cached = stats

    def _cpu_percent(self) -> float | None:
        """Busy fraction of all cores since the last sample, from the aggregate
        ``cpu`` line of ``/proc/stat``. None on the first sample (no baseline
        yet) and on a host without the file."""
        try:
            line = Path("/proc/stat").read_text().split("\n", 1)[0]
        except OSError:
            return None
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        try:
            values = [int(v) for v in parts[1:]]
        except ValueError:
            return None
        # user nice system idle iowait irq softirq steal ...; idle time is
        # idle + iowait, everything is total.
        idle = values[3] + values[4]
        total = sum(values)
        last, self._last_cpu = self._last_cpu, (idle, total)
        if last is None:
            return None
        busy_total = total - last[1]
        if busy_total <= 0:
            return None
        return round(100.0 * (1.0 - (idle - last[0]) / busy_total), 1)


def _load_1m() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):  # no getloadavg on some platforms
        return None


def _temperature_c() -> float | None:
    """The SoC temperature, in °C, from the thermal zone. ``thermal_zone0`` is
    the CPU package on a Pi; read the raw millidegrees rather than shelling to
    vcgencmd, which needs a firmware mailbox the container does not have."""
    try:
        milli = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
    except (OSError, ValueError):
        return None
    return round(milli / 1000.0, 1)


def _uptime_s() -> float | None:
    """Host uptime in seconds (distinct from the agent's process uptime), from
    ``/proc/uptime``."""
    try:
        return round(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _memory() -> dict | None:
    """Total and used memory in MB, and used as a percentage, from
    ``/proc/meminfo``. ``MemAvailable`` is the kernel's own estimate of what a
    workload could claim, which is the honest 'used' — free-plus-reclaimable,
    not the near-zero MemFree that reads as a box out of memory when it is only
    caching files."""
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    info: dict[str, int] = {}
    for entry in text.splitlines():
        key, _, rest = entry.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            info[key.strip()] = int(fields[0])  # kB
    total = info.get("MemTotal")
    if not total:
        return None
    out: dict = {"total_mb": round(total / 1024)}
    available = info.get("MemAvailable")
    if available is not None:
        used = total - available
        out["used_mb"] = round(used / 1024)
        out["used_percent"] = round(100.0 * used / total, 1)
    return out
