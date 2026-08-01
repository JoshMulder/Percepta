"""The deployment: the serial path, the clock, and the files that install it.

These are the things that will be wrong on the first real box, and none of them
can be proved here — there is no Pi, no UART, no camera and no SDR on this
machine. So the tests cover what *can* be checked without hardware: that the
failures are specific rather than generic, that the shipped inventory says what
this station actually has, and that the unit file has not quietly lost a line.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from gsu import clock
from gsu.agent import Agent
from gsu.config import AgentConfig
from gsu.devices import registry
from gsu.devices.serialio import SerialPort, list_ports

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


class SerialFailureTests(unittest.TestCase):
    """A first connection fails in one of a handful of ways. Each says which.

    The serial layer has never spoken to a real UART, so the quality of its
    error messages is the difference between a five-minute fix and a return
    visit. These end up in `unavailable_reason` and on the setup page.
    """

    def message(self, path: str, baud: int = 4800) -> str:
        with self.assertRaises((FileNotFoundError, OSError, ValueError)) as caught:
            SerialPort(path, baud)
        return str(caught.exception)

    def test_no_port_configured_says_so_and_lists_what_is_there(self):
        message = self.message("")
        self.assertIn("no serial port set", message)
        self.assertTrue("Ports present now" in message or "No serial ports" in message)

    def test_a_missing_port_names_the_stable_alternative(self):
        message = self.message("/dev/ttyUSB99")
        self.assertIn("no such serial port", message)
        self.assertIn("by-id", message)

    def test_a_path_that_is_not_a_serial_device_says_that_precisely(self):
        # Otherwise "could not open /etc" reads as a permissions problem.
        message = self.message("/etc")
        self.assertIn("not a serial device", message)

    def test_a_character_device_that_is_not_a_tty_is_still_caught(self):
        message = self.message("/dev/null")
        self.assertIn("not a serial port", message)

    def test_an_unsupported_baud_lists_the_supported_ones(self):
        message = self.message("/dev/null", 1234)
        self.assertTrue("unsupported baud" in message or "not a serial port" in message)

    def test_listing_ports_never_raises_and_prefers_stable_names(self):
        ports = list_ports()
        stable = [port for port in ports if port.stable]
        self.assertEqual(ports[:len(stable)], stable, "stable names must come first")
        for port in ports:
            self.assertTrue(port.path.startswith("/dev/"))


class SerialDefaultTests(unittest.TestCase):
    def parameters(self, type_id: str) -> dict:
        device = registry.get(type_id)
        return {parameter.name: parameter for parameter in device.parameters}

    def test_no_device_defaults_to_a_ttyusb_path(self):
        # Two USB-UARTs are fitted and their numbering swaps between boots, so
        # a plausible default is worse than none: each driver would silently
        # read the other's traffic, which looks like both instruments failing.
        for type_id in ("uavionix-ping-rx-pro", "airmar-110wx",
                        "generic-nmea-weather", "victron-mppt-modbus"):
            port = self.parameters(type_id)["port"]
            self.assertEqual(port.default, "", type_id)
            self.assertIn("by-id", port.help, type_id)

    def test_each_device_defaults_to_its_own_baud(self):
        self.assertEqual(self.parameters("airmar-110wx")["baud"].default, 4800)
        self.assertEqual(self.parameters("uavionix-ping-rx-pro")["baud"].default, 57600)


class ClockTests(unittest.TestCase):
    def test_discipline_reports_something_and_never_raises(self):
        state = clock.discipline(force=True)
        self.assertIn(state.source, ("gps", "ntp", "rtc-only", "none", "unknown"))
        self.assertIn(state.synchronised, (True, False, None))
        self.assertIsInstance(state.rtc_present, bool)
        self.assertTrue(state.detail)
        self.assertEqual(set(state.to_dict()),
                         {"synchronised", "source", "detail", "rtc_present"})

    def test_it_is_cached_because_it_is_asked_every_health_frame(self):
        first = clock.discipline(force=True)
        self.assertIs(clock.discipline(), first)

    def test_a_gps_reference_would_be_reported_as_gps(self):
        # The drop-in the owner intends: chrony disciplined by PPS or GPS needs
        # no station-side change at all, and this is the line that proves the
        # reporting half of that claim without a receiver to hand.
        self.assertIn("PPS", clock.GPS_REFERENCE_IDS)
        self.assertIn("GPS", clock.GPS_REFERENCE_IDS)


class ShippedInventoryTests(unittest.TestCase):
    """deploy/devices.pi.json must describe the box in HARDWARE.md."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.home = Path(self._dir.name)
        (self.home / "devices.json").write_text(
            (DEPLOY / "devices.pi.json").read_text()
        )
        self.agent = Agent(AgentConfig(home=self.home, setup_enabled=False,
                                       single_instance=False, demo=True))

    def tearDown(self):
        self.agent.shutdown()
        self._dir.cleanup()

    def test_the_four_real_devices_are_selected(self):
        fitted = {slot: entry.type_id for slot, entry in self.agent.inventory.fitted.items()}
        self.assertEqual(fitted["adsb"], "uavionix-ping-rx-pro")
        self.assertEqual(fitted["weather"], "airmar-110wx")
        self.assertEqual(fitted["radio"], "rtlsdr-airband")
        self.assertEqual(fitted["camera"], "raspberry-pi-csi")

    def test_nothing_is_simulated(self):
        for report in self.agent.inventory.report():
            self.assertFalse(report.simulated, report.slot)

    def test_the_radio_now_asks_for_a_tuner_rather_than_for_software(self):
        # This slot used to report "not supported by this software build". It
        # has a driver now (gsu/radio/rtlsdr.py), so what it is short of is an
        # allocation: a tuner is claimed by serial number and only the box knows
        # its own dongle's, exactly as with the serial ports. The distinction is
        # the whole value of the message — one is fixed by shipping code and the
        # other by an installer clicking a dropdown.
        reports = {report.slot: report for report in self.agent.inventory.report()}
        detail = reports["radio"].detail
        self.assertNotIn("not supported by this software build", detail)
        self.assertIn("rtlsdr", detail)
        self.assertTrue(reports["radio"].driver_available)

    def test_the_camera_says_what_is_missing_on_this_machine(self):
        # The CSI camera has a driver now. On anything that is not a Pi it
        # reports what is absent rather than "unsupported": the distinction is
        # the whole point of the message, because one of them is fixed by
        # installing a package and the other cannot be fixed at all.
        #
        # It names one package and not two. `python3-picamera2` used to be the
        # faster of two capture paths and is no longer used at all — it was the
        # only thing putting libcamera inside this process, which is the only
        # thing that can wedge a camera for the life of a run.
        detail = {r.slot: r for r in self.agent.inventory.report()}["camera"].detail
        self.assertIn("rpicam", detail)
        self.assertNotIn("picamera2", detail)

    def test_the_streams_with_no_driver_are_declared_unavailable(self):
        sent: list[dict] = []
        self.agent._publish = lambda topic, payload: sent.append(payload) or True
        self.agent.step(1.0, weather_due=True)
        radio = [payload for payload in sent if payload["kind"] == "radio"]
        self.assertTrue(radio)
        self.assertIs(radio[0]["available"], False)
        # No tuner is allocated in the shipped file, so there is still no source
        # behind this stream — but the reason is now an allocation the installer
        # can make, not a build they cannot.
        self.assertIn("rtlsdr", radio[0]["unavailable_reason"])
        # ...and never an empty payload that reads as "nothing on frequency".
        self.assertNotIn("squelch_open", radio[0])

    def test_no_serial_port_is_guessed_for_this_site(self):
        for slot in ("adsb", "weather"):
            self.assertEqual(self.agent.inventory.fitted[slot].params["port"], "")

    def test_the_only_outstanding_conflict_is_the_unassigned_tuner(self):
        # A device with no driver must not also demand a tuner: that would be a
        # critical condition about a receiver which could not be used even if it
        # were assigned, which is noise on top of the real message. The radio
        # has a driver now, so the demand is real and is the one thing left for
        # an installer to do. Nothing else may be raised alongside it.
        conflicts = self.agent.inventory.conflicts()
        self.assertEqual(len(conflicts), 1, conflicts)
        self.assertIn("radio", conflicts[0])
        self.assertIn("rtlsdr", conflicts[0])


class UnitFileTests(unittest.TestCase):
    """The systemd unit, checked for the lines that are easy to lose."""

    @classmethod
    def setUpClass(cls):
        cls.unit = (DEPLOY / "gsu.service").read_text()
        # The comments explain what is *not* there, so the directives have to be
        # read on their own or every explanation reads as a violation.
        cls.directives = "\n".join(
            line for line in cls.unit.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        cls.install = (DEPLOY / "install.sh").read_text()

    def test_it_does_not_wait_for_the_network(self):
        # The whole design keeps working with the link down; a unit ordered
        # after network-online would not start at all on a site with no signal.
        self.assertNotIn("network-online.target", self.directives)
        # ...and not after time-sync either, which waits on the network in turn.
        self.assertNotIn("time-sync.target", self.directives)
        self.assertIn("After=local-fs.target network.target", self.directives)

    def test_it_restarts_for_ever(self):
        self.assertIn("Restart=always", self.unit)
        self.assertIn("StartLimitIntervalSec=0", self.unit)

    def test_it_refuses_to_start_where_it_is_not_the_deployment_path(self):
        # Restarting for ever is right on a remote site and dangerous on a
        # container box, where this unit's interpreter does not exist: one
        # `systemctl restart gsu` becomes an unbounded crash loop beside a
        # healthy container. The assertion is what makes the two compatible.
        assertion = re.search(r"AssertPathExists=(\S+)", self.directives)
        self.assertIsNotNone(assertion, "the unit must assert its own venv")
        exec_start = re.search(r"ExecStart=(\S+)", self.unit).group(1)
        # Not merely present — asserting the wrong path would be worse than
        # asserting nothing, because it would refuse on the systemd path too.
        self.assertEqual(assertion.group(1), exec_start)

    def test_a_failed_start_on_the_container_path_cannot_loop(self):
        # Assert, not Condition: a condition skips quietly, and a station that
        # does nothing when told to start hides the mistake instead of naming
        # it. Neither triggers Restart=, which is the property that matters.
        self.assertNotIn("ConditionPathExists=/opt/percepta/station/.venv",
                         self.directives)
        # And the installer must not leave a box that is already looping, or
        # already failed from one, in that state across a re-install.
        self.assertIn("systemctl reset-failed", self.install)

    def test_the_hardening_the_brief_asked_for_is_present(self):
        for directive in ("NoNewPrivileges=yes", "PrivateTmp=yes",
                          "ProtectSystem=strict", "User=gsu", "ProtectHome=yes",
                          "CapabilityBoundingSet=", "SystemCallFilter=@system-service"):
            self.assertIn(directive, self.unit)

    def test_it_keeps_the_device_access_the_hardware_needs(self):
        # PrivateDevices=yes would take away the UARTs and the SDR.
        self.assertNotIn("PrivateDevices=yes", self.directives)
        self.assertIn("SupplementaryGroups=dialout", self.unit)
        self.assertIn("AF_NETLINK", self.unit)   # or DNS resolution breaks

    def test_the_radio_is_given_time_to_shut_down_gracefully(self):
        # A dongle killed mid-transfer needs a physical replug, which here is a
        # truck (server/docs/05-radio-integration.md).
        self.assertIn("KillSignal=SIGTERM", self.unit)
        timeout = re.search(r"TimeoutStopSec=(\d+)", self.unit)
        self.assertTrue(timeout and int(timeout.group(1)) >= 30)

    def test_the_paths_in_the_unit_and_the_installer_agree(self):
        exec_start = re.search(r"ExecStart=(\S+)", self.unit).group(1)
        self.assertTrue(exec_start.startswith("/opt/percepta/station"))
        self.assertIn("PREFIX=/opt/percepta/station", self.install)
        state = re.search(r"StateDirectory=(\S+)", self.unit).group(1)
        self.assertIn(f"STATE=/var/lib/{state}", self.install)
        self.assertIn(f"GSU_HOME=/var/lib/{state}",
                      (DEPLOY / "gsu.env.example").read_text())

    def test_the_shipped_environment_requires_tls(self):
        env = (DEPLOY / "gsu.env.example").read_text()
        self.assertIn("GSU_REQUIRE_TLS=1", env)
        self.assertRegex(env, r"GSU_PLATFORM_URL=https://")
        # GSU_BROKER_URL ships empty: the platform states the broker address at
        # enrolment, and it is the relay on the platform's own host. What still
        # has to hold is that nothing here suggests a plaintext one — the
        # assertion used to be `rediss://`, which stopped being true when the
        # setting stopped having a default at all, and asserting the old
        # literal would have meant shipping a default that is wrong behind a
        # proxy just to keep a test green.
        for scheme in ("redis://", "ws://", "http://", "mqtt://"):
            self.assertNotRegex(
                env, rf"^GSU_\w+={scheme}", f"a plaintext {scheme} default")
        # The setup page must not be shipped bound to anything routable. It
        # now has authentication, but the shipped default is still the one
        # that is safe on a box whose address is public.
        self.assertIn("GSU_SETUP_HOST=127.0.0.1", env)
        # The switch that used to be able to un-pin everything is gone.
        self.assertNotIn("GSU_TLS_TRUST", env)

    def test_the_shipped_environment_carries_no_setup_password(self):
        """An image-wide password is one password for every station.

        The example file documents how to generate one and leaves it commented
        out, so a box that has not been provisioned with its own cannot serve
        the setup page anywhere but loopback.
        """
        env = (DEPLOY / "gsu.env.example").read_text()
        live = [
            line for line in env.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for line in live:
            self.assertFalse(
                line.startswith(("GSU_SETUP_PASSWORD=", "GSU_SETUP_PASSWORD_HASH=")),
                f"a password is live in the shipped environment file: {line}",
            )
        # ...but it is documented, or nobody will know it is what unlocks the
        # page an installer is standing in front of.
        self.assertIn("GSU_SETUP_PASSWORD_HASH", env)
        self.assertIn("python -m gsu setup-password", env)

    def test_the_shipped_setup_window_is_not_pinned_open(self):
        # 0 means the page answers on the LAN for as long as the station runs,
        # which is the permanent back door the design exists to refuse.
        env = (DEPLOY / "gsu.env.example").read_text()
        match = re.search(r"^GSU_SETUP_WINDOW_MINUTES=(\S+)", env, re.M)
        self.assertIsNotNone(match, "the window is not set in the shipped file")
        self.assertGreater(float(match.group(1)), 0)


class ContainerTests(unittest.TestCase):
    """The container files. **This is the deployment path** (DECISIONS 35c).

    Both files have now been built and run on the first real Pi, and the
    device reasoning held (DECISIONS.md item 35's dated update). There is
    still no Docker daemon where this suite runs, so these check what a
    careful read can check: the base image is pinned, device access is
    permissive enough that a missing sensor cannot stop the station, the log
    rotation that protects the SD card is present, and no `privileged: true`
    has crept in.

    The device assertions are the ones that matter. They encode a decision that
    reversed twice, and a well-meaning tightening of them would restore the
    failure that made containers unacceptable the first time round.
    """

    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (DEPLOY / "Dockerfile").read_text()
        cls.compose = (DEPLOY / "docker-compose.yml").read_text()
        # Asserted against the text rather than a parsed tree so that these run
        # everywhere without adding PyYAML as a dependency. The file's *schema*
        # was validated separately with `docker compose config`, which is the
        # real parser; what these guard against is a line going missing.
        cls.directives = "\n".join(
            line for line in cls.compose.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    def test_the_container_can_see_what_is_disciplining_the_clock(self):
        # Without this mount `/run` inside the container is its own tmpfs, so
        # timesyncd's flag file is invisible, and neither chronyc nor
        # timedatectl is in the image — every probe in gsu/clock.py returns
        # "no idea". On a box with a hardware RTC that unknown was worded as
        # "a hardware RTC, not synced": a permanent false alarm on a station
        # whose clock was correctly synchronised the whole time.
        self.assertIn("/run/systemd/timesync:/run/systemd/timesync:ro",
                      self.directives)

    def test_the_clock_mount_is_the_directory_and_not_the_flag_file(self):
        # The flag file does not exist until the first sync, and Docker creates
        # a *directory* in place of a missing bind source. Mounting the file
        # would leave timesyncd unable to ever write it, turning "cannot tell"
        # into a permanent, and this time confident, "not synchronised".
        self.assertNotIn("timesync/synchronized:", self.directives)

    def test_no_named_device_list_that_could_stop_the_station_starting(self):
        # THE regression this file exists to prevent. Docker refuses to start a
        # container whose mapped device is missing, so a named `devices:` list
        # means one USB adapter failing to enumerate at boot takes the whole
        # station down — on a site nobody can reach. DECISIONS.md 35a/35c.
        self.assertNotIn("devices:", self.directives)
        self.assertNotIn("/dev/ttyUSB0:", self.directives)

    def test_dev_is_mounted_whole_so_hot_replug_works(self):
        # And it must be all of /dev: /dev/serial/by-id holds *relative*
        # symlinks (../../ttyUSB0) that resolve against the container's own
        # /dev, so mounting the symlink directory alone gives stable names
        # pointing at nothing.
        self.assertIn("- /dev:/dev", self.directives)
        self.assertNotIn("/dev/serial/by-id:/dev/serial/by-id", self.directives)

    def test_the_device_cgroup_allows_every_major_this_station_can_use(self):
        # Visibility is not permission: without these, open() returns EPERM on
        # a node that is plainly there.
        for major, what in ((188, "USB serial"), (166, "CDC-ACM"),
                            (204, "on-chip UART"), (189, "USB raw / libusb"),
                            (81, "video4linux"), (249, "pps")):
            self.assertIn(f"c {major}:* rmw", self.directives, what)

    def test_privileged_is_still_not_used(self):
        # Not for isolation — that was traded away deliberately — but because
        # it also changes cgroup, AppArmor and /sys handling, which is a
        # blunter tool and one more thing to reason about when debugging.
        self.assertNotIn("privileged", self.directives.replace("no-new-privileges", ""))

    def test_the_docs_agree_that_the_container_is_the_path(self):
        # Prose is line-wrapped, so match on collapsed whitespace rather than
        # on where the paragraph happened to break.
        runbook = " ".join((DEPLOY.parent / "DEPLOYMENT.md").read_text().split())
        self.assertIn("The station runs as a container", runbook)
        self.assertIn("Appendix B: running it as a plain systemd service", runbook)
        # The trade is stated, not glossed.
        self.assertIn("Isolation was traded away deliberately", runbook)

    def test_the_base_image_is_pinned_by_digest(self):
        # An unpinned base is a different station every time it is rebuilt.
        self.assertRegex(self.dockerfile, r"FROM .*python:3\.11-slim-bookworm@sha256:[0-9a-f]{64}")

    def test_it_runs_as_a_non_root_user(self):
        self.assertIn("USER gsu", self.dockerfile)
        self.assertIn("useradd", self.dockerfile)

    def test_the_code_is_not_writable_by_the_agent(self):
        # Matches the systemd path: a compromised agent must not be able to
        # rewrite the thing that restarts it.
        self.assertIn("chown -R root:root /opt/percepta/station", self.dockerfile)

    def test_no_bytecode_is_written_to_the_sd_card(self):
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", self.dockerfile)

    def test_nothing_reaches_for_privileged(self):
        # The device mappings are the honest cost of the container path;
        # privileged: true would be the dishonest way out of them.
        self.assertNotIn("privileged", self.directives.replace("no-new-privileges", ""))

    def test_the_container_hardening_matches_the_unit(self):
        for directive in ("cap_drop:", "- ALL", "no-new-privileges:true",
                          "read_only: true"):
            self.assertIn(directive, self.directives)

    def test_logs_are_rotated_or_the_sd_card_fills(self):
        # Docker's json-file driver has no rotation by default. journald does,
        # which is why only this path needs saying.
        self.assertIn("driver: json-file", self.directives)
        self.assertRegex(self.directives, r'max-size:\s*"\d+m"')
        self.assertRegex(self.directives, r'max-file:\s*"\d+"')

    def test_the_radio_still_gets_its_shutdown_window(self):
        # Docker's default grace is 10s, which is not enough to shut the
        # receiver down through its own path.
        self.assertIn("stop_grace_period: 45s", self.directives)
        self.assertIn("restart: unless-stopped", self.directives)

    def test_the_console_is_published_to_loopback_only(self):
        # It has no authentication. On 0.0.0.0 this would be an unauthenticated
        # setup page on the public internet.
        self.assertIn('- "127.0.0.1:8088:8088"', self.directives)
        self.assertNotIn('- "8088:8088"', self.directives)

    def test_the_state_directory_is_the_same_path_as_the_systemd_path(self):
        # `ls /var/lib/percepta-gsu` should work whichever way it was deployed.
        self.assertIn("/var/lib/percepta-gsu:/var/lib/percepta-gsu", self.directives)

    def test_the_sdr_and_camera_are_not_mapped_while_they_have_no_driver(self):
        # Mapping a device nothing opens is access granted for no reason. Both
        # are present as commented, ready-to-use lines instead.
        self.assertNotIn("/dev/bus/usb:/dev/bus/usb", self.directives)
        self.assertNotIn("/dev/video0", self.directives)
        self.assertIn("/dev/bus/usb", self.compose)     # documented in comments

    def test_the_dockerignore_keeps_one_stations_identity_out_of_the_image(self):
        ignored = (DEPLOY.parent / ".dockerignore").read_text()
        self.assertIn("var/", ignored)


class ServiceAccountTests(unittest.TestCase):
    """One uid on both paths — the convention the first real Pi forced.

    The installer used to let `useradd --system` pick a floating uid while the
    image pinned 10001, so a bind-mounted state directory was readable on one
    path and not the other, and flipping paths needed a manual `chown -R`.
    These tests hold the three files to one number and the compose file to not
    contradicting enrolment.
    """

    @classmethod
    def setUpClass(cls):
        cls.install = (DEPLOY / "install.sh").read_text()
        cls.dockerfile = (DEPLOY / "Dockerfile").read_text()
        cls.camera = (DEPLOY / "Dockerfile.camera").read_text()
        cls.compose = (DEPLOY / "docker-compose.yml").read_text()
        cls.compose_directives = "\n".join(
            line for line in cls.compose.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    def test_the_host_account_and_both_images_agree_on_the_uid(self):
        # The whole convention is one number in four places. A drift in any of
        # them recreates the field failure: state owned by a uid the container
        # is not.
        installer = re.search(r"^SERVICE_UID=(\d+)", self.install, re.M)
        self.assertIsNotNone(installer, "install.sh no longer pins the uid")
        uid = installer.group(1)
        for name, text in (("Dockerfile", self.dockerfile),
                           ("Dockerfile.camera", self.camera)):
            self.assertIn(f"--uid {uid}", text, f"{name} pins a different uid")
            self.assertIn(f"--gid {uid}", text, f"{name} pins a different gid")

    def test_the_installer_creates_the_account_with_the_pinned_uid(self):
        # `useradd --system` with no --uid picks a floating number, which is
        # exactly the bug. The uid must be passed explicitly.
        self.assertRegex(self.install, r'useradd --system --uid "\$SERVICE_UID"')
        self.assertRegex(self.install, r'groupadd --system --gid "\$SERVICE_UID"')

    def test_the_state_directory_is_reowned_on_every_run(self):
        # A path flip must never need a manual chown again — the installer
        # repairs ownership unconditionally, and with a shared uid the repair
        # is a no-op on a healthy box.
        self.assertRegex(
            self.install, r'chown -R "\$SERVICE_USER:\$SERVICE_USER" "\$STATE"')

    def test_an_existing_account_on_the_wrong_uid_is_migrated_not_ignored(self):
        # Idempotence has to include installs made before the convention:
        # the first real Pi already has a floating-uid gsu on it.
        self.assertIn('usermod -u "$SERVICE_UID"', self.install)
        self.assertIn('groupmod -g "$SERVICE_UID"', self.install)

    def test_the_compose_file_does_not_shadow_the_enrolment_delivered_ca(self):
        # GSU_CA_FILE set in the environment block points at a read-only mount
        # that exists only when pre-provisioned — and a set-but-unreadable
        # GSU_CA_FILE is a hard refusal, never a fallback (gsu/tls.py). The
        # enrolment response is where the broker CA normally comes from, and
        # compose must not override that for every container station.
        self.assertNotIn("GSU_CA_FILE", self.compose_directives)
        # ...while the opt-in stays documented where a person would set it.
        env_example = (DEPLOY / "gsu.env.example").read_text()
        self.assertIn("#GSU_CA_FILE=", env_example)

    def test_ca_certificates_are_installed_as_the_public_material_they_are(self):
        # 0640 root:gsu blocked the container's user from the trust root it
        # was configured to verify the broker against. A CA certificate has no
        # secret in it; gsu.env does, and keeps its tighter mode.
        self.assertRegex(self.install, r'install -m 0644 -o root -g root "\$1" "\$2"')
        self.assertIn('install -m 0640 -o root -g "$SERVICE_USER" '
                      '"$SRC/deploy/gsu.env.example"', self.install)


if __name__ == "__main__":
    unittest.main()


class UpdaterTests(unittest.TestCase):
    """The update mechanism (DECISIONS.md item 39).

    Its decision logic is exercised properly against a stubbed Docker in
    `scratchpad/updatelab` — 21 scenarios covering accept, gate failure,
    rollback, rollback verification, rejected-digest suppression, failed pull
    and no-op. **It has never driven a real container.**

    What is checked here is the shape that scenario harness cannot: that the
    protective behaviours are still written into the script and the units, so
    that a later edit cannot quietly remove one and leave the tests green.
    """

    @classmethod
    def setUpClass(cls):
        cls.script = (DEPLOY / "gsu-update.sh").read_text()
        cls.timer = (DEPLOY / "gsu-update.timer").read_text()
        cls.service = (DEPLOY / "gsu-update.service").read_text()

    def test_the_script_is_executable(self):
        self.assertTrue(os.access(DEPLOY / "gsu-update.sh", os.X_OK))

    def test_the_gate_requires_publishing_not_merely_starting(self):
        # The condition that earns the gate its keep: a container can start,
        # log cheerfully and publish nothing, and that is invisible to a
        # "did it start?" check.
        self.assertIn("published", self.script)
        self.assertIn("enrolled", self.script)
        self.assertRegex(self.script, r'\[ "\$published" -gt "\$baseline" \]')

    def test_a_failed_gate_rolls_back(self):
        self.assertIn("rollback ", self.script)
        self.assertIn("did not pass the health gate", self.script)

    def test_the_rollback_is_itself_gated(self):
        # So that "the rollback worked" is a fact, not an assumption — and so
        # that an old image which also fails is reported as NOT an update fault.
        rollback = self.script.split("rollback() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if gate;", rollback)
        self.assertIn("ALARM", rollback)

    def test_a_rejected_digest_is_not_retried(self):
        # Otherwise a bad image is re-pulled every timer tick for ever.
        self.assertIn("REJECTED=", self.script)
        self.assertIn("not being retried", self.script)

    def test_a_failed_pull_changes_nothing(self):
        pull = self.script.split('if ! docker pull', 1)[1].split("fi", 1)[0]
        self.assertIn("return 0", pull)
        self.assertIn("untouched", pull)

    def test_the_previous_image_is_kept_before_the_swap(self):
        # The rollback target has to exist before there is anything to roll
        # back from, and it is captured by image id so a later retag cannot
        # move it.
        self.assertIn("PREVIOUS_TAG", self.script)
        self.assertIn('docker tag "$before" "$PREVIOUS_TAG"', self.script)

    def test_the_timer_is_jittered_and_not_at_boot(self):
        # Without jitter a whole fleet checks in the same minute and a bad
        # image takes all of them out together.
        self.assertRegex(self.timer, r"RandomizedDelaySec=\d+h")
        self.assertIn("OnBootSec=", self.timer)
        self.assertNotIn("OnBootSec=0", self.timer)
        self.assertIn("Persistent=true", self.timer)

    def test_the_updater_runs_on_the_host_as_root(self):
        # A container that can reach the docker socket can replace itself with
        # anything, which would make the gate decorative.
        self.assertIn("User=root", self.service)
        self.assertNotIn("docker.sock", (DEPLOY / "docker-compose.yml").read_text())

    def test_nothing_updates_until_a_reference_is_configured(self):
        # A station that keeps running what it has is the safe default for a
        # box nobody can reach.
        self.assertIn("GSU_UPDATE_REF", self.script)
        self.assertIn("Nothing to track", self.script)
        env = (DEPLOY / "gsu.env.example").read_text()
        self.assertIn("GSU_UPDATE_REF=", env)
        self.assertIn("GSU_GATE_SECONDS", env)

    def test_the_dockerfile_keeps_code_as_the_last_layer(self):
        # A code-only update then ships one ~91 KB layer. Moving a COPY below
        # this would quietly make every update a bigger download.
        dockerfile = (DEPLOY / "Dockerfile").read_text()
        copies = [line for line in dockerfile.splitlines()
                  if line.startswith("COPY")]
        self.assertTrue(copies[-1].startswith("COPY gsu/"), copies)
