"""Configuration, in two halves that change on completely different clocks.

**Agent configuration** is how this process finds the world: where its state
lives, which platform to enrol against, which broker to use if the enrolment
response cannot be believed. It comes from the environment, is read once at
start, and is the sort of thing a technician or an image sets.

**Site configuration** is everything in `contract/enrolment.md` §7: thresholds,
retention, duty cycling, cadence. It is versioned, persisted, delivered by the
platform on the command channel as `config.set`, and reported back in telemetry.
The station owns the persisted copy — a station that forgets its thresholds when
the link drops is a station whose alerting depends on the platform, which is the
one thing the design forbids.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

#: Where state lives by default. Deliberately inside the checkout while there is
#: no answer to "which compute platform" (contract/enrolment.md §9.1) — see
#: DECISIONS.md. On real hardware this is a persistent partition, not a repo.
DEFAULT_HOME = Path(__file__).resolve().parent.parent / "var"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class AgentConfig:
    """Process-level settings. Read once; changing them means a restart."""

    home: Path = DEFAULT_HOME

    #: Where /api/enrol lives. Only ever used for enrolment, renewal and status;
    #: no telemetry path goes near it.
    platform_url: str = "http://localhost:8000"

    #: Overrides the broker URL the enrolment response carries. Needed whenever
    #: the platform reports an address that is only routable from inside its own
    #: network (a container hostname, say) — the station is by definition
    #: somewhere else. The username and topics from enrolment are still used.
    broker_url: str | None = None

    #: One-shot enrolment code, for a headless first boot. The normal path is a
    #: technician typing it into the setup page (contract/enrolment.md §5).
    enrol_token: str | None = None

    tick_seconds: float = 1.0

    #: Bind the setup page to loopback by default. On real hardware it belongs
    #: on the box's own setup network, which is decision §9.1/§9.2 territory.
    setup_host: str = "127.0.0.1"
    setup_port: int = 8088
    setup_enabled: bool = True

    #: How busy the simulated airband channel is: "off", "low" or "busy".
    #: A rural airband channel is silent the vast majority of the time and the
    #: default reflects that; "busy" is for exercising the audio path.
    airband_traffic: str = "low"

    #: Refuse to start a second agent for the same station. Two instances
    #: publishing independent worlds onto one channel makes the console flicker
    #: between them, which looks like a platform bug rather than an operator
    #: mistake.
    single_instance: bool = True

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            home=Path(_env("GSU_HOME", str(DEFAULT_HOME))),
            platform_url=_env("GSU_PLATFORM_URL", "http://localhost:8000"),
            broker_url=_env("GSU_BROKER_URL"),
            enrol_token=_env("GSU_ENROL_TOKEN"),
            tick_seconds=float(_env("GSU_TICK_SECONDS", "1.0")),
            setup_host=_env("GSU_SETUP_HOST", "127.0.0.1"),
            setup_port=int(_env("GSU_SETUP_PORT", "8088")),
            setup_enabled=_env("GSU_SETUP", "1") not in ("0", "false", "no"),
            airband_traffic=_env("GSU_AIRBAND_TRAFFIC", "low"),
            single_instance=_env("GSU_SINGLE_INSTANCE", "1") not in ("0", "false"),
        )

    # --- paths ----------------------------------------------------------

    def ensure_home(self) -> Path:
        # 0700: the credential lives in here. See credentials.py.
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)
        except OSError:
            pass
        return self.home

    @property
    def credential_path(self) -> Path:
        return self.home / "credential.json"

    @property
    def site_config_path(self) -> Path:
        return self.home / "site-config.json"

    @property
    def receiver_state_path(self) -> Path:
        # Obligation 3 of server/docs/05-radio-integration.md: gain, ppm and
        # frequency survive a restart. Remote-Radio keeps its own state.json;
        # this is ours.
        return self.home / "receiver.json"

    @property
    def devices_path(self) -> Path:
        # What an installer said is fitted. Intent, not detection — the two are
        # deliberately different records (devices/inventory.py).
        return self.home / "devices.json"

    @property
    def store_path(self) -> Path:
        return self.home / "station.db"

    @property
    def recordings_dir(self) -> Path:
        return self.home / "recordings"

    @property
    def lock_path(self) -> Path:
        return self.home / "agent.lock"


@dataclass
class SiteConfig:
    """Versioned site policy. Persisted locally, updated by `config.set`.

    Defaults are deliberately conservative and deliberately *local*: every
    threshold here is one the station must be able to act on with the platform
    unreachable.
    """

    version: int = 0

    #: Proximity alert. The station decides, because the threshold belongs with
    #: the site (contract/schemas/telemetry.schema.json, aircraft.alert).
    alert_range_km: float = 12.0
    alert_altitude_m: float = 1500.0

    low_battery_pct: float = 20.0
    critical_battery_pct: float = 10.0
    #: Duty cycling: the floodlight is the first load shed, and it is shed by
    #: the station itself rather than by a command that may never arrive.
    shed_light_below_soc_pct: float = 12.0

    wind_alarm_kt: float = 45.0

    #: Local recording retention. Bounded by both, whichever bites first.
    audio_retention_hours: float = 24.0
    audio_retention_mb: float = 200.0
    event_retention_days: float = 30.0

    #: Cadence, from contract/transport.md. A site may need to differ.
    weather_period_s: float = 5.0
    health_period_s: float = 30.0

    def apply(self, patch: dict, version: int | None = None) -> list[str]:
        """Merge a `config.set` payload. Unknown keys are ignored, not rejected:
        a newer platform must be able to talk to an older station."""
        known = {f.name for f in fields(self)} - {"version"}
        changed: list[str] = []
        for key, value in (patch or {}).items():
            if key not in known:
                continue
            current = getattr(self, key)
            try:
                coerced = type(current)(value)
            except (TypeError, ValueError):
                continue
            if coerced != current:
                setattr(self, key, coerced)
                changed.append(key)
        if version is not None:
            self.version = int(version)
        return changed

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> SiteConfig:
        config = cls()
        if path.exists():
            try:
                config.apply(json.loads(path.read_text()))
                config.version = int(json.loads(path.read_text()).get("version", 0))
            except (ValueError, OSError):
                # A corrupt config file must not stop a remote station booting;
                # defaults are safe and the platform will resend its version.
                pass
        return config
