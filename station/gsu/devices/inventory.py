"""What an installer said is fitted, what the station actually found, and the
drivers that follow from both.

`contract/enrolment.md` §7: *the station owns the truth about what is attached.*
So there are two records here and they are never merged:

    fitted     intent. "An Airmar 110WX should be on /dev/ttyUSB0, with the
               humidity module." Chosen by a person, persisted, survives reboots
               and outages.
    detected   fact. "There is no /dev/ttyUSB0." Recomputed every time anyone
               asks, and never written back over the intent.

A camera that has failed and a camera that was never fitted look identical in a
database and completely different at the site; keeping intent and detection
separate is what makes the difference visible.

Resources — SDR tuners today — are allocated rather than assumed. A tuner serves
one job: 108–137 MHz airband and 1090 MHz ADS-B cannot be received at the same
time by the same dongle. Allocation is keyed on **serial number**, because two
identical dongles enumerate in an order that changes between boots.

Nothing here ever silently substitutes a simulation for hardware that is
missing. A slot whose driver cannot be built has no driver, publishes nothing,
and says why.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import registry
from .serialio import SerialPort

log = logging.getLogger("gsu.inventory")

#: Realtek RTL2832U, which is what an RTL2838 dongle enumerates as.
RTLSDR_IDS = {("0bda", "2838"), ("0bda", "2832")}


@dataclass
class Fitted:
    """One slot's intent."""

    type_id: str = ""
    params: dict = field(default_factory=dict)
    #: Which physical resource this device uses, by resource id (serial-keyed).
    resource: str | None = None


@dataclass(frozen=True)
class Resource:
    """A physical thing a device consumes. Keyed on serial, never on index."""

    id: str
    kind: str
    serial: str
    model: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SlotReport:
    slot: str
    type_id: str
    label: str
    connection: str
    configured: bool
    detected: bool
    driver_available: bool
    status: str
    detail: str
    simulated: bool
    provides: tuple[str, ...]
    absent: tuple[str, ...]
    telemetry_kind: str | None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["provides"] = list(self.provides)
        data["absent"] = list(self.absent)
        return data


def scan_rtlsdr() -> list[Resource]:
    """Find RTL-SDR dongles from sysfs, with their serial numbers.

    sysfs rather than `lsusb` so it works on a box with no usbutils installed,
    which a minimal image will not have.
    """
    found: list[Resource] = []
    root = Path("/sys/bus/usb/devices")
    if not root.exists():
        return found
    for device in sorted(root.iterdir()):
        try:
            vendor = (device / "idVendor").read_text().strip().lower()
            product = (device / "idProduct").read_text().strip().lower()
        except (OSError, ValueError):
            continue
        if (vendor, product) not in RTLSDR_IDS:
            continue
        try:
            serial = (device / "serial").read_text().strip()
        except OSError:
            serial = ""
        try:
            name = (device / "product").read_text().strip()
        except OSError:
            name = "RTL2832U"
        # A dongle with no serial programmed cannot be told from another one;
        # say so rather than falling back to the bus path, which moves.
        identity = serial or f"unprogrammed@{device.name}"
        found.append(
            Resource(
                id=f"rtlsdr:{identity}", kind="rtlsdr", serial=serial,
                model=name,
                detail="" if serial else
                       "no serial programmed — indistinguishable from another "
                       "identical dongle; program one with rtl_eeprom before "
                       "fitting a second",
            )
        )
    return found


class Inventory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fitted: dict[str, Fitted] = {
            slot: Fitted(type_id=type_id)
            for slot, type_id in registry.default_fitted().items()
        }
        self.drivers: dict[str, object] = {}
        self.reasons: dict[str, str] = {}
        self.load()

    # --- persistence ----------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            log.warning("Device inventory unreadable; using defaults.")
            return
        for slot, entry in (data.get("fitted") or {}).items():
            if slot in registry.SLOTS:
                self.fitted[slot] = Fitted(
                    type_id=entry.get("type_id", ""),
                    params=entry.get("params") or {},
                    resource=entry.get("resource"),
                )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fitted": {slot: asdict(entry) for slot, entry in self.fitted.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def set_device(self, slot: str, type_id: str, params: dict | None = None,
                   resource: str | None = None) -> None:
        if slot not in registry.SLOTS:
            raise ValueError(f"unknown slot {slot!r}")
        if type_id and registry.get(type_id) is None:
            raise ValueError(f"unknown device type {type_id!r}")
        self.fitted[slot] = Fitted(type_id=type_id, params=params or {}, resource=resource)
        self.save()

    # --- resources ------------------------------------------------------

    def resources(self) -> list[Resource]:
        return scan_rtlsdr()

    def allocations(self) -> dict[str, list[str]]:
        """Which slots claim each resource id."""
        claims: dict[str, list[str]] = {}
        for slot, entry in self.fitted.items():
            if entry.resource:
                claims.setdefault(entry.resource, []).append(slot)
        return claims

    def conflicts(self) -> list[str]:
        """Allocation problems, in words a person can act on."""
        problems: list[str] = []
        present = {resource.id for resource in self.resources()}
        for resource_id, slots in self.allocations().items():
            if len(slots) > 1:
                problems.append(
                    f"{resource_id} is assigned to {' and '.join(sorted(slots))}. "
                    "One tuner receives one band at a time: airband and 1090 MHz "
                    "cannot share a dongle. Fit a second receiver or choose one."
                )
            if resource_id not in present:
                problems.append(
                    f"{resource_id} is assigned to {slots[0]} but is not plugged in."
                )
        for slot, entry in self.fitted.items():
            device = registry.get(entry.type_id) if entry.type_id else None
            if device and device.resource and not entry.resource:
                problems.append(
                    f"{slot}: {device.label} needs a {device.resource} assigned to it."
                )
        return problems

    # --- drivers --------------------------------------------------------

    def build(self, slot: str, context: dict) -> object | None:
        """Construct the driver for a slot, or None with a reason recorded."""
        entry = self.fitted.get(slot) or Fitted()
        self.drivers.pop(slot, None)
        if not entry.type_id:
            self.reasons[slot] = "nothing fitted"
            return None
        device = registry.get(entry.type_id)
        if device is None:
            self.reasons[slot] = f"unknown device type {entry.type_id!r}"
            return None
        if device.driver is None:
            # Phrased for whoever reads it on the console or in telemetry, not
            # for whoever wrote it: they need to know the box cannot read this
            # device, not how the code is organised.
            self.reasons[slot] = f"{device.label}: not supported by this software build"
            return None

        params = {**{p.name: p.default for p in device.parameters}, **entry.params}
        source = None
        if device.connection == "serial":
            try:
                source = SerialPort(str(params.get("port")), int(params.get("baud", 4800)))
            except (FileNotFoundError, ValueError, OSError) as exc:
                self.reasons[slot] = f"{device.label}: {exc}"
                return None
        if device.resource:
            present = {resource.id for resource in self.resources()}
            if not entry.resource or entry.resource not in present:
                self.reasons[slot] = (
                    f"{device.label} needs a {device.resource}; none assigned or "
                    "the assigned one is not plugged in"
                )
                return None

        candidates = {**context, **params}
        if source is not None:
            candidates["source"] = source
        try:
            driver = _instantiate(device.driver, candidates)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            log.exception("Could not construct %s for %s.", device.driver, slot)
            self.reasons[slot] = f"{device.label}: {exc}"
            if source is not None:
                source.close()
            return None
        self.reasons.pop(slot, None)
        self.drivers[slot] = driver
        return driver

    # --- reporting ------------------------------------------------------

    def report(self) -> list[SlotReport]:
        """Intent against fact, per slot. This is what goes out in telemetry and
        what the local console renders."""
        reports: list[SlotReport] = []
        for slot in registry.SLOTS:
            entry = self.fitted.get(slot) or Fitted()
            device = registry.get(entry.type_id) if entry.type_id else None
            driver = self.drivers.get(slot)
            status = "not_fitted"
            detail = self.reasons.get(slot, "")
            detected = False
            if device is not None:
                if driver is None:
                    status = "configured_absent"
                    detail = detail or "configured, not reachable"
                else:
                    described = getattr(driver, "describe", None)
                    device_status = getattr(driver, "status", None)
                    detected = bool(described and described().present)
                    if device_status in ("absent", "failed"):
                        status = "configured_absent"
                    elif device_status == "stalled":
                        status = "stalled"
                    else:
                        status = "present"
                        detected = True
                    detail = described().detail if described else device.label
            reports.append(
                SlotReport(
                    slot=slot,
                    type_id=entry.type_id,
                    label=device.label if device else "not fitted",
                    connection=device.connection if device else "none",
                    configured=bool(entry.type_id),
                    detected=detected,
                    driver_available=bool(device and device.driver),
                    status=status,
                    detail=detail,
                    simulated=bool(device and device.simulated),
                    provides=device.provides if device else (),
                    absent=device.absent if device else (),
                    telemetry_kind=registry.SLOT_TELEMETRY.get(slot),
                )
            )
        return reports

    def unsourced_streams(self) -> list[str]:
        """Telemetry kinds this station has no working source for.

        The console needs this to render "no receiver" rather than a plausible
        nothing — an empty ADS-B map is indistinguishable from clear airspace
        unless somebody says which it is.
        """
        return [
            report.telemetry_kind
            for report in self.report()
            if report.telemetry_kind and report.status != "present"
        ]


def _instantiate(dotted: str, candidates: dict):
    """`module:Class` with whichever of `candidates` the constructor accepts.

    Filtering by signature is what keeps the registry declarative: a new device
    is a row and a driver, not a new branch in a factory.
    """
    module_name, _, attribute = dotted.partition(":")
    module = importlib.import_module(module_name)
    target = getattr(module, attribute)
    signature = inspect.signature(target)
    accepted = {
        name: value for name, value in candidates.items()
        if name in signature.parameters
    }
    return target(**accepted)
