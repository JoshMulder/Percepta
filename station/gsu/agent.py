"""The loop. Everything else in this package is something it drives.

The shape is dictated by one sentence in `station/README.md`: *nothing the
station needs to do correctly may require the platform to be reachable.* So the
loop runs whether or not the box is enrolled, whether or not the broker answers,
and whether or not anything it publishes is heard. Sensing, recording, local
alerting and duty cycling all happen above the transport and never ask it a
question. Publishing is the last thing each tick does, and its failure is a
counter, not an exception.

Cadence is `contract/transport.md`: adsb, power, radio and light at 1 Hz,
weather at 0.2 Hz, audio only while the squelch is open. Nothing is queued —
telemetry is current state, and a frame that missed its moment is worth less
than the one a second behind it.

**A stream with no working device is not published at all.** Not an empty
array, not a zero. An empty ADS-B frame means "clear airspace" and a weather
frame full of defaults means "measured"; both are lies a console cannot detect.
What is missing, and why, goes out in the health frame and shows on the local
console instead.
"""

from __future__ import annotations

import base64
import logging
import signal
import threading
import time
from datetime import UTC, datetime

from . import AGENT_VERSION, clock, tls
from .commands import CommandRouter, build_handlers
from .config import AgentConfig, SiteConfig
from .credentials import CredentialStore, Enrolment
from .devices.inventory import Inventory
from .enrolment import EnrolmentClient, Renewer
from .health import Health
from .radio.receiver import RadioController
from .store import LocalStore
from .transport import Transport, build_transport, redact_url

log = logging.getLogger("gsu.agent")

#: Hardware inventory sent at enrolment. Explicitly not trust — nothing here
#: influences what the station may do — but it is what an admin sees in the
#: fleet list, so it says plainly what this box is.
HARDWARE = {
    "model": "percepta-gsu-agent",
    "os": "linux",
    "agent_version": AGENT_VERSION,
}

PRUNE_EVERY_SECONDS = 300.0

#: How a stream with no source is described to an operator. Short, in their
#: terms, and never a parser's business — the structured version of the same
#: fact is in the health payload's device inventory.
NO_SOURCE = {
    "adsb": "no ADS-B receiver connected",
    "radio": "no airband receiver connected",
    "weather": "no weather station connected",
    "power": "no charge controller connected",
    "light": "no floodlight fitted",
}

#: `unavailable_reason` is capped by the schema.
REASON_LIMIT = 200

#: How often the device set is rebuilt when something is missing. A USB-UART
#: that was unplugged at boot and plugged in afterwards should come good on its
#: own: nobody is there to restart anything.
REDISCOVER_SECONDS = 30.0


class Agent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        config.ensure_home()

        self.health = Health()
        self.site = SiteConfig.load(config.site_config_path)
        self.store = LocalStore(config.store_path, config.recordings_dir)
        self.credentials = CredentialStore(config.credential_path)
        self.ca = tls.CaStore(config.ca_path)
        # Two roots, deliberately. The broker is pinned to a private CA; the
        # API is verified against the system bundle unless told otherwise,
        # because it is expected behind a proxy with a public certificate.
        self.trust = self._resolve_broker_trust()
        self.api_trust = self._resolve_api_trust()
        self.client = EnrolmentClient(config.platform_url, trust=self.api_trust)
        self.inventory = Inventory(config.devices_path)

        self.enrolment: Enrolment | None = None
        self.transport: Transport | None = None
        self.router: CommandRouter | None = None
        self.renewer: Renewer | None = None

        # Devices exist before enrolment does. A box waiting for a technician to
        # type a code is still a box on a hillside with sensors on it.
        self.adsb = None
        self.weather = None
        self.power = None
        self.light = None
        self.radio: RadioController | None = None
        self._last_discovery = 0.0
        self.build_devices()

        self._attach_lock = threading.Lock()
        self._stop = threading.Event()
        self._lock_handle = None

        # Alert edge state. Alerts are edge-triggered because an operator wants
        # to know that something happened, not to be told 3600 times an hour
        # that it is still happening.
        self._alerting_icao: set[str] = set()
        self._battery_state = "ok"
        self._link_up: bool | None = None
        self._offline_since: float | None = None
        self._last_prune = 0.0
        self._published = 0
        self._started = time.monotonic()
        self._credential_mtime: float | None = None

    # --- trust ----------------------------------------------------------

    def _resolve_broker_trust(self) -> tls.Trust:
        """What the broker is verified against. Always a pinned private CA.

        A trust root that cannot be read is a fault to *report*, never a reason
        to proceed without one: the fallback is "no CA", which refuses every
        TLS URL, and never "no verification". The station keeps sensing and
        recording either way — that is the whole design — it simply does not
        talk to anything it cannot identify.
        """
        try:
            trust = tls.resolve_broker(
                self.ca,
                installed=self.config.ca_file,
                require_tls=self.config.require_tls,
            )
        except tls.Refusal as exc:
            self.health.raise_condition("tls.broker_trust_unusable", "critical", str(exc))
            log.error("%s", exc)
            return tls.Trust(require_tls=self.config.require_tls, purpose="broker")
        log.info("Broker TLS trust: %s.", trust.describe())
        return trust

    def _resolve_api_trust(self) -> tls.Trust:
        """What the platform API is verified against.

        The system CA bundle by default — the API is expected behind a
        TLS-terminating proxy with a public certificate, and the public trust
        store is the right tool for one. Pinned only when `GSU_API_CA_FILE`
        says so, which is the correct setting while the platform serves its own
        certificate.
        """
        try:
            trust = tls.resolve_api(
                installed=self.config.api_ca_file,
                require_tls=self.config.require_tls,
            )
        except tls.Refusal as exc:
            # Deliberately not a silent fall back to the system store: the
            # operator asked for pinning and got a broken file, and quietly
            # doing something weaker than they asked for is the whole failure
            # mode this module exists to prevent.
            self.health.raise_condition("tls.api_trust_unusable", "critical", str(exc))
            log.error("%s", exc)
            return tls.Trust(mode=tls.TRUST_PINNED, require_tls=self.config.require_tls,
                             purpose="api")
        log.info("Platform API TLS trust: %s.", trust.describe())
        return trust

    def _persist_ca(self, enrolment: Enrolment) -> None:
        """Keep the **broker's** CA from the enrolment response, and pin to it.

        `contract/enrolment.md` §4 calls `broker.ca_pem` pinned, which only
        means anything if it is stored: a CA re-fetched over an unverified
        channel every boot is pinned to nothing. A CA that *changes* is either a
        planned rotation or somebody else's certificate, and from here those
        look identical — so it is accepted (the response that carried it was
        itself verified) and said out loud.

        This never touches the API's trust root. That one is configured locally
        and is not something the platform gets to change by sending a field.
        """
        pem = enrolment.broker.ca_pem
        if not pem:
            return
        # Persisted even when an installed CA is present and takes precedence:
        # if the installed file is ever removed, the box should still be pinned
        # to something rather than falling back to trusting anything.
        try:
            changed = self.ca.save(pem)
        except (ValueError, OSError) as exc:
            self.health.raise_condition(
                "tls.ca_unwritable", "critical",
                f"The platform sent a CA that could not be stored at "
                f"{self.config.ca_path}: {exc}",
            )
            return
        self.health.clear("tls.ca_unwritable")
        if changed:
            log.warning(
                "The platform's CA changed (now SHA-256 %s). Pinning to the new "
                "one; if this was not a planned rotation, it is worth asking why.",
                tls.fingerprint(pem),
            )
            self.store.record_event(
                "tls.ca_rotated", "warning",
                f"Pinned CA replaced; SHA-256 {tls.fingerprint(pem)}.",
            )
        # Re-resolve so the next transport uses it. The API client keeps its own
        # trust: one CA arriving in a response must not silently become the root
        # for the channel that delivered it.
        self.trust = self._resolve_broker_trust()

    # --- devices --------------------------------------------------------

    def device_context(self) -> dict:
        site = self.enrolment.site if self.enrolment else None
        return {
            "latitude": site.latitude if site and site.latitude is not None else -43.5,
            "longitude": site.longitude if site and site.longitude is not None else 172.6,
            "timezone": site.timezone if site else "UTC",
            "alert_range_km": self.site.alert_range_km,
            "alert_altitude_m": self.site.alert_altitude_m,
            "traffic": self.config.airband_traffic,
        }

    def build_devices(self) -> None:
        """Construct whatever the inventory says is fitted, and record why
        anything else is missing. Never substitutes a simulation for hardware
        that did not answer."""
        context = self.device_context()
        self._last_discovery = time.monotonic()

        if self.radio is not None:
            try:
                self.radio.shutdown()
            except Exception:  # noqa: BLE001
                pass
        for slot in ("adsb", "weather", "power", "light"):
            driver = self.inventory.drivers.get(slot)
            close = getattr(driver, "close", None)
            if close:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

        self.adsb = self.inventory.build("adsb", context)
        self.weather = self.inventory.build("weather", context)
        self.power = self.inventory.build("power", context)
        self.light = self.inventory.build("light", context)
        front_end = self.inventory.build("radio", context)
        self.radio = (
            RadioController(front_end, state_path=self.config.receiver_state_path)
            if front_end is not None else None
        )
        self.inventory.build("camera", context)

        if self.router is not None:
            self.router.handlers = build_handlers(self.radio, self.light, self._apply_config)

        self._report_capabilities()

    def _report_capabilities(self) -> None:
        missing = [
            report for report in self.inventory.report()
            if report.configured and report.status != "present"
        ]
        unconfigured = [
            report for report in self.inventory.report() if not report.configured
        ]
        if missing:
            self.health.raise_condition(
                "devices.absent", "warning",
                "; ".join(f"{report.slot}: {report.detail}" for report in missing),
            )
        else:
            self.health.clear("devices.absent")

        unsourced = self.inventory.unsourced_streams()
        if unsourced:
            self.health.raise_condition(
                "telemetry.unsourced", "warning",
                "No source for: " + ", ".join(sorted(unsourced))
                + ". Those streams are not published at all rather than "
                  "published empty.",
            )
        else:
            self.health.clear("telemetry.unsourced")

        conflicts = self.inventory.conflicts()
        if conflicts:
            self.health.raise_condition("devices.conflict", "critical", "; ".join(conflicts))
            for conflict in conflicts:
                log.error("Device allocation: %s", conflict)
        else:
            self.health.clear("devices.conflict")

        for report in unconfigured:
            log.info("Slot %s: nothing fitted.", report.slot)

    # --- enrolment ------------------------------------------------------

    def load_enrolment(self) -> Enrolment | None:
        try:
            enrolment = self.credentials.load()
        except ValueError as exc:
            self.health.raise_condition("enrolment.unreadable", "critical", str(exc))
            log.error("%s", exc)
            return None
        if enrolment is not None:
            self._attach(enrolment)
        return enrolment

    def enrol(self, token: str) -> Enrolment:
        """Claim a code. Raises with a message meant for a technician.

        Resumable by construction: this can be called again after a dropped
        connection, and the platform re-issues rather than refusing
        (`contract/enrolment.md` §11) — the failure that matters is a technician
        stuck on a hillside with a used code.
        """
        enrolment = self.client.claim(token, HARDWARE)
        self.credentials.save(enrolment)
        self.health.clear("enrolment.missing")
        self.health.clear("enrolment.unreadable")
        self.store.record_event(
            "enrolment.claimed", "info",
            f"Enrolled as {enrolment.site.name} ({enrolment.station_id}).",
        )
        self._attach(enrolment)
        return enrolment

    def _attach(self, enrolment: Enrolment) -> None:
        """Take up an identity: connect, subscribe, and start renewing."""
        with self._attach_lock:
            if self.transport is not None:
                self.transport.stop()
            self.enrolment = enrolment

            self._persist_ca(enrolment)

            # The platform states its own broker address, which on a development
            # stack is frequently only routable from inside it. The override
            # exists for that; the username and topics still come from
            # enrolment, because those are identity rather than deployment.
            url = self.config.broker_url or enrolment.broker.url
            try:
                self.transport = build_transport(
                    url,
                    username=enrolment.broker.username,
                    password=enrolment.credential.secret,
                    trust=self.trust,
                )
            except tls.Refusal as exc:
                # Refusing to publish is the correct outcome, and everything
                # below still happens: sensing, recording, local alerting and
                # credential renewal are unaffected, and `_publish` already
                # returns False when there is no transport. The one thing that
                # must not happen is connecting anyway.
                self.transport = None
                self.health.raise_condition("uplink.refused", "critical", str(exc))
                self.store.record_event("uplink.refused", "critical", str(exc))
                log.error(
                    "NOT PUBLISHING. %s The station is still sensing, recording "
                    "and alerting locally.", exc,
                )
            else:
                self.health.clear("uplink.refused")
                self.transport.start()

            handlers = build_handlers(self.radio, self.light, self._apply_config)
            self.router = CommandRouter(enrolment.broker.command_topic, handlers)
            if self.transport is not None:
                self.transport.subscribe(enrolment.broker.command_topic, self._on_command)

            # The site's own details are things the station needs while the
            # platform is unreachable, so they come from the stored enrolment
            # rather than from a live call.
            for driver in (self.adsb, self.weather):
                set_site = getattr(driver, "set_site", None)
                if set_site and enrolment.site.latitude is not None:
                    set_site(enrolment.site.latitude, enrolment.site.longitude)
                set_timezone = getattr(driver, "set_timezone", None)
                if set_timezone:
                    set_timezone(enrolment.site.timezone)
            if self.site.version == 0:
                self.site.version = enrolment.config_version
                self.site.save(self.config.site_config_path)

            self._credential_mtime = self.credentials.mtime()

            if self.renewer is not None:
                self.renewer.stop()
            self.renewer = Renewer(
                self.client, self.credentials, enrolment, self.health,
                on_renewed=self._on_renewed,
            )
            self.renewer.start()

            log.info(
                "Station %s (%s) attached: publishing to %s, listening on %s as %s.",
                enrolment.site.name, enrolment.station_id,
                enrolment.broker.telemetry_topic, enrolment.broker.command_topic,
                enrolment.broker.username,
            )

    def reload_credential_if_changed(self) -> bool:
        """Pick up a credential this process did not issue itself.

        Re-enrolment can happen from three places: the local console (which
        reattaches directly), `gsu enrol` in another process, or an image
        rewriting the file. Only the first tells the running agent. Without
        this, a box that has been correctly re-enrolled over SSH sits with a
        dead secret and an `uplink.down` alarm until somebody restarts it —
        which on an unattended site means somebody who is hours away.

        Checked only while the uplink is down: a healthy station has no reason
        to re-read its own identity.
        """
        mtime = self.credentials.mtime()
        if mtime is None or mtime == self._credential_mtime:
            return False
        try:
            enrolment = self.credentials.load()
        except ValueError as exc:
            self.health.raise_condition("enrolment.unreadable", "critical", str(exc))
            return False
        if enrolment is None:
            return False
        log.info("The stored credential changed on disk; re-attaching.")
        self.store.record_event(
            "credential.reloaded", "info",
            "Picked up a credential issued by another process.",
        )
        self.health.clear("enrolment.missing")
        self.health.clear("credential.revoked")
        self._attach(enrolment)
        return True

    def _on_renewed(self, enrolment: Enrolment) -> None:
        self.enrolment = enrolment
        # A renewal returns the whole response, CA included, so this is where a
        # rotated CA arrives on a station that never re-enrols.
        self._persist_ca(enrolment)
        if self.transport is not None:
            self.transport.set_credentials(
                enrolment.broker.username, enrolment.credential.secret
            )
        self.store.record_event(
            "credential.renewed", "info",
            f"Credential renewed; expires {enrolment.credential.expires_at.isoformat()}.",
        )

    # --- commands -------------------------------------------------------

    def _on_command(self, channel: str, payload: dict) -> None:
        if self.router is not None:
            self.router.dispatch(channel, payload)

    def _apply_config(self, payload: dict) -> str:
        """`config.set`: apply, persist, and report the new version.

        The platform never assumes the change took — same rule as every other
        command — so the version goes out in the next health frame.
        """
        version = payload.get("version", payload.get("config_version"))
        changed = self.site.apply(payload.get("config") or payload, version)
        self.site.save(self.config.site_config_path)
        for driver in (self.adsb,):
            set_thresholds = getattr(driver, "set_thresholds", None)
            if set_thresholds:
                set_thresholds(self.site.alert_range_km, self.site.alert_altitude_m)
        return f"version {self.site.version}, changed {changed or 'nothing'}"

    # --- the loop -------------------------------------------------------

    def run(self) -> int:
        if not self._take_lock():
            return 1
        self._install_signals()

        reason = clock.implausible_reason()
        if reason is not None:
            # Not fatal to running: sensing and recording do not need a correct
            # clock. Fatal to enrolling, which is where it strands a site.
            self.health.raise_condition("clock.implausible", "critical", reason)
            log.error("Clock is implausible: %s", reason)

        if self.load_enrolment() is None:
            self.health.raise_condition(
                "enrolment.missing", "warning",
                "Not enrolled. Enter an enrolment code on the setup page.",
            )
            log.warning(
                "Not enrolled: sensing and recording locally, publishing nothing. "
                "Enter a code on the setup page or set GSU_ENROL_TOKEN."
            )
            if self.config.enrol_token:
                try:
                    self.enrol(self.config.enrol_token)
                except Exception as exc:  # noqa: BLE001 - shown, not raised
                    log.error("Enrolment with the supplied code failed: %s", exc)

        console = None
        if self.config.setup_enabled:
            from .console import Console

            console = Console(self, self.config.setup_host, self.config.setup_port)
            console.start()

        tick = self.config.tick_seconds
        weather_due = 0.0
        health_due = 0.0
        next_tick = time.monotonic()
        log.info("Station agent %s running at %.1f Hz.", AGENT_VERSION, 1 / tick)

        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self.step(tick, weather_due <= 0, health_due <= 0)
                except Exception:  # noqa: BLE001 - a loop that dies is a dead site
                    log.exception("Tick failed; continuing.")
                weather_due = (
                    self.site.weather_period_s if weather_due <= 0 else weather_due - tick
                )
                health_due = (
                    self.site.health_period_s if health_due <= 0 else health_due - tick
                )
                if started - self._last_prune > PRUNE_EVERY_SECONDS:
                    self._last_prune = started
                    self.store.prune(
                        self.site.audio_retention_hours,
                        self.site.audio_retention_mb,
                        self.site.event_retention_days,
                    )
                if (
                    started - self._last_discovery > REDISCOVER_SECONDS
                    and self._anything_missing()
                ):
                    self.build_devices()
                if self.transport is None or not self.transport.connected:
                    # Also checked when there is no transport at all: a refused
                    # uplink is exactly the case a technician fixes by
                    # re-enrolling, which brings a new CA with it.
                    self.reload_credential_if_changed()
                # Absolute schedule rather than sleep(tick): a slow tick must
                # not make the cadence drift away from 1 Hz for ever.
                next_tick += tick
                delay = next_tick - time.monotonic()
                if delay < -tick:
                    next_tick = time.monotonic()
                    delay = 0
                self._stop.wait(max(0.0, delay))
        finally:
            if console is not None:
                console.stop()
            self.shutdown()
        return 0

    def _anything_missing(self) -> bool:
        return any(
            report.configured and report.driver_available and report.status != "present"
            for report in self.inventory.report()
        )

    def step(self, dt: float, weather_due: bool = False, health_due: bool = False) -> None:
        """One tick. Sensing first, publishing last, and no step in between
        cares whether the link is up."""
        light_load = getattr(self.light, "load_w", 0.0) if self.light else 0.0

        reading = None
        if self.power is not None:
            reading = self.power.read(dt, extra_load_w=light_load)
            # Duty cycling: the station sheds its own load rather than waiting
            # for a command that may never arrive.
            if (
                self.light is not None
                and reading.soc_pct < self.site.shed_light_below_soc_pct
                and self.light.on
            ):
                log.warning(
                    "Shedding the floodlight at %.0f%% state of charge.", reading.soc_pct
                )
                self.store.record_event(
                    "power.shed", "warning",
                    f"Floodlight shed at {reading.soc_pct:.0f}% state of charge.",
                )
                self.light.request(False)

        if self.light is not None:
            self.light.step(dt)

        contacts = self.adsb.poll(dt) if self.adsb is not None else None

        radio_payload = audio_payload = None
        if self.radio is not None:
            radio_payload, audio_payload = self.radio.tick(dt)

        if audio_payload is not None:
            # Recorded whether or not it can be sent. A transmission during an
            # outage is not simply gone.
            self.store.write_audio(
                base64.b64decode(audio_payload["pcm"]),
                audio_payload["rate"],
                label=f"{self.radio.freq_hz // 1000}kHz",
            )

        self._evaluate_alerts(contacts, reading)

        # --- publishing, which is allowed to fail -----------------------
        # Every stream reports on its own cadence whether or not it has a
        # source. A stream with none says so explicitly; going quiet is what a
        # failed station looks like, and the console cannot tell the two apart.
        reports = self._reports()
        if contacts is not None:
            # An empty list here is a real statement: the receiver is alive and
            # the sky is clear. It is only ever sent when that is true.
            self._publish_telemetry(
                {"kind": "adsb", "aircraft": [c.to_payload() for c in contacts]}
            )
        else:
            self._publish_telemetry(self.unavailable_payload("adsb", reports))
        self._publish_telemetry(
            reading.to_payload() if reading is not None
            else self.unavailable_payload("power", reports)
        )
        self._publish_telemetry(
            radio_payload if radio_payload is not None
            else self.unavailable_payload("radio", reports)
        )
        self._publish_telemetry(
            {"kind": "light", "on": self.light.on} if self.light is not None
            else self.unavailable_payload("light", reports)
        )
        if weather_due:
            weather = (
                self.weather.read(self.site.weather_period_s)
                if self.weather is not None else None
            )
            self._publish_telemetry(
                weather.to_payload() if weather is not None
                else self.unavailable_payload("weather", reports)
            )
        if health_due:
            self._publish_telemetry(self.health_payload())
        if audio_payload is not None:
            self._publish(
                self.enrolment.broker.audio_topic if self.enrolment else None, audio_payload
            )

        self._update_link_state()

    def unavailable_payload(self, kind: str, reports: dict | None = None) -> dict:
        """Declare a stream the station has no source for.

        `available: false` says *there is nothing behind this stream*, which is
        a different statement from a field the instrument does not measure —
        that one is simply omitted (humidity on a weather head with no RH
        module). Reaching for this when the other is meant would tell an
        operator the weather station is missing when it is working.

        Sent on the stream's normal cadence, never in place of going quiet: a
        station that stops publishing has failed, and "I have no receiver" is
        something that has to keep being said.
        """
        report = (self._reports() if reports is None else reports).get(kind)
        if report is None or not report.configured:
            reason = NO_SOURCE.get(kind, f"no source for {kind}")
        elif report.status == "stalled":
            # Fitted and it has stopped, which is a fault rather than an
            # absence, and an operator acts differently on the two.
            reason = f"{report.label} stopped responding"
        elif report.detail.startswith(report.label):
            # The detail already names the device; repeating the label would
            # spend a third of the 200 characters saying it twice.
            reason = report.detail
        else:
            reason = f"{report.label} configured but not detected"
            if report.detail:
                reason = f"{reason}: {report.detail}"
        return {
            "kind": kind,
            "available": False,
            "unavailable_reason": reason[:REASON_LIMIT],
        }

    def _reports(self) -> dict:
        return {
            report.telemetry_kind: report
            for report in self.inventory.report()
            if report.telemetry_kind
        }

    def _publish_telemetry(self, payload: dict) -> bool:
        topic = self.enrolment.broker.telemetry_topic if self.enrolment else None
        return self._publish(topic, payload)

    def _publish(self, topic: str | None, payload: dict) -> bool:
        if topic is None or self.transport is None:
            return False
        sent = self.transport.publish(topic, payload)
        if sent:
            self._published += 1
        return sent

    # --- alerting, which happens with or without a link -----------------

    def _evaluate_alerts(self, contacts, power) -> None:
        if contacts is not None:
            alerting = {c.icao for c in contacts if c.alert}
            for contact in contacts:
                if contact.alert and contact.icao not in self._alerting_icao:
                    self.store.record_event(
                        "adsb.proximity", "warning",
                        f"{contact.callsign or contact.icao} at {contact.range_km:.1f} km, "
                        f"{(contact.altitude or 0):.0f} m.",
                    )
            self._alerting_icao = alerting

        if power is None:
            return
        # Hysteresis on the way back up, or a battery sitting on the threshold
        # writes an event a second.
        state = "ok"
        if power.soc_pct < self.site.critical_battery_pct:
            state = "critical"
        elif power.soc_pct < self.site.low_battery_pct:
            state = "low"
        if state != self._battery_state:
            recovering = state == "ok" and power.soc_pct < self.site.low_battery_pct + 2
            if not recovering:
                if state == "ok":
                    self.health.clear("power.battery")
                    self.store.record_event(
                        "power.recovered", "info",
                        f"Battery recovered to {power.soc_pct:.0f}%.",
                    )
                else:
                    self.health.raise_condition(
                        "power.battery",
                        "critical" if state == "critical" else "warning",
                        f"Battery at {power.soc_pct:.0f}%.",
                    )
                    self.store.record_event(
                        "power.battery", "critical" if state == "critical" else "warning",
                        f"Battery {state} at {power.soc_pct:.0f}%.",
                    )
                self._battery_state = state

    def security(self) -> dict:
        """How this station's link is protected, as a fact rather than a hope.

        Rendered on the local console and carried in the health frame, because
        "am I actually on TLS, and against which CA" is not a question anyone
        should have to answer by reading source or a packet capture.
        """
        url = None
        if self.transport is not None:
            url = self.transport.url
        elif self.enrolment is not None:
            url = self.config.broker_url or self.enrolment.broker.url
        return {
            # Redacted: the local console has no authentication and this frame
            # goes over the wire. Neither is a place for a pasted password.
            "broker_url": redact_url(url),
            "broker_tls": tls.is_tls(url) if url else None,
            "platform_tls": tls.is_tls(self.config.platform_url),
            # Two roots, reported separately. "Which CA is this box trusting"
            # has two answers and merging them into one is what produced the
            # arrangement this replaced.
            "trust": self.trust.to_dict(),
            "api_trust": self.api_trust.to_dict(),
            "publishing": self.transport is not None,
            "tls_failed": bool(getattr(self.transport, "tls_failed", False)),
        }

    def _update_link_state(self) -> None:
        up = bool(self.transport and self.transport.connected)
        # A certificate the station will not accept is a different fault from a
        # link that is down, and an operator acts differently on the two.
        if getattr(self.transport, "tls_failed", False):
            self.health.raise_condition(
                "uplink.tls_failed", "critical",
                f"The broker's certificate did not verify against "
                f"{self.trust.describe()}. Nothing is being published, and this "
                "station will not connect without verifying. "
                f"Last error: {self.transport.last_error}",
            )
        elif up:
            self.health.clear("uplink.tls_failed")
        if self._link_up is None:
            self._link_up = up
            return
        if up == self._link_up:
            return
        self._link_up = up
        if up:
            offline = time.monotonic() - (self._offline_since or time.monotonic())
            self._offline_since = None
            self.health.clear("uplink.down")
            self.store.record_event(
                "uplink.up", "info", f"Uplink restored after {offline:.0f}s.",
            )
        else:
            self._offline_since = time.monotonic()
            self.health.raise_condition(
                "uplink.down", "warning",
                "No route to the broker; telemetry is being dropped and events "
                "are being recorded locally.",
            )
            self.store.record_event("uplink.down", "warning", "Uplink lost.")

    # --- health ---------------------------------------------------------

    def health_payload(self) -> dict:
        """The `health` telemetry kind — in the contract, and consumed.

        Proposed from this side and since adopted: it is in
        `contract/schemas/telemetry.schema.json` `$defs/health` and in the
        platform ingest's `KNOWN_KINDS`, and `devices[].simulated` is what
        drives the console's DEMO badge. **Validate changes here against the
        schema** — `tests/test_station.py` does, in both the enrolled and
        unenrolled states, because this payload is the one whose shape varies
        most with what is wrong at the time.

        It carries the things there is otherwise no way to say: the config
        version the station is running (`contract/enrolment.md` §7 requires it
        be reported in telemetry), the devices it actually found against the
        ones it was told to expect, **which telemetry streams have no source at
        all**, and whether the credential is renewing.

        `security`, `clock` and `resources` are not in the schema yet. The
        schema allows additional properties, so they are valid rather than
        merely tolerated — they are proposed properly in CONTRACT-QUESTIONS.
        """
        # Re-evaluated here rather than only at build time: a device that was
        # absent at boot and has since started talking must stop being reported
        # as missing without anyone restarting anything.
        self._report_capabilities()
        self._check_clock()
        credential = self.enrolment.credential if self.enrolment else None
        transport = self.transport
        payload = {
            "kind": "health",
            "agent_version": AGENT_VERSION,
            "config_version": self.site.version,
            # The contract's summary vocabulary (ok | degraded | failing), which
            # is deliberately not the per-condition severity vocabulary
            # (info | warning | critical) carried in `conditions` below. They
            # answer different questions; see health.Health.SUMMARY.
            "status": self.health.summary(),
            "conditions": self.health.to_list(),
            "uplink": {
                "connected": bool(transport and transport.connected),
                "dropped_frames": transport.dropped if transport else 0,
                "offline_seconds": round(
                    time.monotonic() - self._offline_since, 1
                ) if self._offline_since else 0.0,
            },
            # Two things a remote box cannot be asked in person: whether its
            # link is verified, and whether its clock is disciplined by
            # anything. Both are cheap to state and expensive to guess.
            "security": self.security(),
            "clock": clock.discipline().to_dict(),
            "devices": [report.to_dict() for report in self.inventory.report()],
            # The console's reason to render "no receiver" rather than an empty
            # panel that looks like quiet airspace.
            "unsourced_streams": sorted(self.inventory.unsourced_streams()),
            "unsourced_fields": self._unsourced_fields(),
            "resources": [resource.to_dict() for resource in self.inventory.resources()],
            "storage": self.store.stats(),
            "uptime_s": round(time.monotonic() - self._started, 1),
        }
        # Renewal health, and only when there is a credential to have any. The
        # schema types `expires_at` as a string; a null would be this station
        # breaking its own rule that an unsourced value is omitted rather than
        # defaulted (DECISIONS.md item 16). A station with no credential has no
        # renewal health — that fact is `enrolment.missing` in `conditions`.
        if credential is not None:
            payload["credential"] = {
                "expires_at": credential.expires_at.isoformat(),
                "renewal_failures": self.renewer.failures if self.renewer else 0,
            }
        return payload

    def _check_clock(self) -> None:
        """Whether anything is keeping this clock honest.

        `contract/enrolment.md` §6 is about a clock that is *wrong*; this is the
        condition that precedes it. A Pi has no battery-backed clock, so between
        boot and the first NTP exchange its time is whatever the filesystem
        suggested, and a box that never syncs at all is one credential lifetime
        away from a site visit. Reported rather than acted on: sensing and
        recording do not need a correct clock, and enrolling does — which is
        already refused separately.
        """
        state = clock.discipline()
        if state.synchronised is False:
            self.health.raise_condition(
                "clock.unsynchronised", "warning",
                f"The clock is not disciplined by anything ({state.detail}). "
                "This box has no battery-backed clock, so its time is only as "
                "good as its last sync. Check NTP reachability; fit an RTC or a "
                "GPS time source (HARDWARE.md §4).",
            )
        else:
            self.health.clear("clock.unsynchronised")

    def _unsourced_fields(self) -> dict:
        """Fields the console renders for which this station has no sensor."""
        out: dict[str, list[str]] = {}
        for report in self.inventory.report():
            if report.absent and report.telemetry_kind:
                out[report.telemetry_kind] = list(report.absent)
        return out

    def snapshot(self) -> dict:
        """What the local console shows, in the installer's terms."""
        self._report_capabilities()
        return {
            "enrolled": self.enrolment is not None,
            "station": self.enrolment.site.name if self.enrolment else None,
            "station_id": self.enrolment.station_id if self.enrolment else None,
            "telemetry_topic": self.enrolment.broker.telemetry_topic if self.enrolment else None,
            "broker": redact_url(self.config.broker_url or self.enrolment.broker.url)
            if self.enrolment else None,
            "platform": self.config.platform_url,
            "link": bool(self.transport and self.transport.connected),
            "published": self._published,
            "dropped": self.transport.dropped if self.transport else 0,
            "radio": {
                "fitted": self.radio is not None,
                "freq_mhz": round(self.radio.freq_hz / 1e6, 3) if self.radio else None,
                "squelch_open": self.radio.squelch_open if self.radio else False,
                "auto": self.radio.auto_squelch if self.radio else False,
                "threshold_db": round(self.radio.last_threshold_db, 1) if self.radio else None,
            },
            "health": self.health.to_list(),
            "devices": [report.to_dict() for report in self.inventory.report()],
            "resources": [resource.to_dict() for resource in self.inventory.resources()],
            "conflicts": self.inventory.conflicts(),
            "unsourced_streams": sorted(self.inventory.unsourced_streams()),
            "unsourced_fields": self._unsourced_fields(),
            "events": [event.to_dict() for event in self.store.recent_events(15)],
            "storage": self.store.stats(),
            "clock": datetime.now(UTC).isoformat(),
            "clock_source": clock.discipline().to_dict(),
            "security": self.security(),
            "serial_ports": [port.to_dict() for port in self.inventory.serial_ports()],
            "config_version": self.site.version,
        }

    # --- lifecycle ------------------------------------------------------

    def _take_lock(self) -> bool:
        if not self.config.single_instance:
            return True
        import fcntl

        # Two agents on one station publish two independent worlds onto the same
        # channel; the console alternates between them and aircraft teleport.
        # Easy to do by accident and hard to recognise from the outside.
        self._lock_handle = open(self.config.lock_path, "w")
        try:
            fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error(
                "Another agent is already running (%s is locked). Stop it first.",
                self.config.lock_path,
            )
            return False
        self._lock_handle.write(str(time.time()))
        self._lock_handle.flush()
        return True

    def _install_signals(self) -> None:
        def handle(signum, _frame):
            log.info("Signal %s: shutting down.", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except ValueError:
                pass  # not the main thread; the caller owns signals

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        # Ordered so nothing is left half-written and, above all, so the radio
        # is stopped gracefully: a dongle killed mid-transfer needs a physical
        # replug, which on an unattended site is a truck
        # (server/docs/05-radio-integration.md obligation 2).
        if self.radio is not None:
            try:
                self.radio.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Receiver shutdown failed.")
        for driver in self.inventory.drivers.values():
            close = getattr(driver, "close", None)
            if close:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        if self.renewer is not None:
            self.renewer.stop()
        if self.transport is not None:
            self.transport.stop()
        self.store.close()
        if self._lock_handle is not None:
            try:
                self._lock_handle.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("Stopped.")
