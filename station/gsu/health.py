"""What is wrong with this station, and for how long.

Conditions are raised and cleared by whichever component knows — the renewer,
the transport, a sensor adapter — and read by two consumers: the health
telemetry the agent publishes, and the local setup page a technician looks at
when there is no link at all.

`since` matters more than the message. "Renewal has been failing for four
hours" is a different call-out from "renewal failed once", and the difference is
invisible if each report is a fresh snapshot.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from . import clock

log = logging.getLogger("gsu.health")

SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class Condition:
    id: str
    severity: str
    detail: str
    since: datetime

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "severity": self.severity,
            "detail": self.detail,
            "since": self.since.isoformat(),
        }


class Health:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conditions: dict[str, Condition] = {}

    def raise_condition(self, id: str, severity: str, detail: str = "") -> None:
        if severity not in SEVERITIES:
            severity = "warning"
        with self._lock:
            existing = self._conditions.get(id)
            # Keep the original `since` while the condition persists, even if
            # it worsens: the age of the problem is the useful number.
            since = existing.since if existing else clock.now()
            changed = (
                existing is None
                or existing.severity != severity
                or existing.detail != detail
            )
            self._conditions[id] = Condition(id, severity, detail, since)
        if changed:
            log.warning("health %s [%s] %s", id, severity, detail)

    def clear(self, id: str) -> None:
        with self._lock:
            existing = self._conditions.pop(id, None)
        if existing is not None:
            log.info("health %s cleared after %s", id, clock.now() - existing.since)

    def active(self) -> list[Condition]:
        with self._lock:
            return sorted(
                self._conditions.values(),
                key=lambda c: (SEVERITIES.index(c.severity), c.id),
                reverse=True,
            )

    def worst(self) -> str:
        conditions = self.active()
        if not conditions:
            return "ok"
        return conditions[0].severity

    def to_list(self) -> list[dict]:
        return [c.to_dict() for c in self.active()]
