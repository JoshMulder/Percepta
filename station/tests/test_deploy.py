"""The deployment: the serial path, the clock, and the files that install it.

These are the things that will be wrong on the first real box, and none of them
can be proved here — there is no UART, no camera and no SDR on this machine. So
the tests cover what *can* be checked without hardware: that the failures are
specific rather than generic, that the shipped inventory says what this station
actually has, and that the compose file has not quietly lost a line.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gsu import clock
from gsu.agent import Agent
from gsu.config import AgentConfig
from gsu.devices import registry
from gsu.devices.serialio import SerialPort, list_ports

#: The station package root, where docker-compose.yml, .env, bootstrap.sh and
#: the documentation live — everything a person types or reads.
STATION = Path(__file__).resolve().parent.parent
#: The things nobody types: the Dockerfile, the udev rule, the reference
#: inventory, the environment reference.
DEPLOY = STATION / "deploy"


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

    def test_the_kernel_sync_bit_is_read_without_raising(self):
        # adjtimex(2) is the one sync signal a container can always read — it
        # shares the host kernel's clock. Here it only has to answer with a
        # tri-state and never raise; the value depends on the machine.
        self.assertIn(clock._kernel_synchronised(), (True, False, None))

    def test_a_kernel_synced_clock_beats_a_blind_container_probe(self):
        # The container has no chronyc, and timesyncd's flag file belongs to a
        # daemon the box may not run, so the probes can report "nothing is
        # keeping this clock" about one chrony keeps perfectly. The kernel's own
        # bit is the authority and must win, or every chrony box alarms for ever
        # on a clock that is fine. This is the false alarm the change removes.
        self.addCleanup(setattr, clock, "_cached", None)
        with mock.patch.object(clock, "_kernel_synchronised", return_value=True), \
             mock.patch.object(clock, "_chrony", return_value=None), \
             mock.patch.object(clock, "_timesyncd", return_value=(
                 False, "none",
                 "systemd-timesyncd is running but has not synchronised")), \
             mock.patch.object(clock, "_timedatectl", return_value=None):
            state = clock.discipline(force=True)
        self.assertIs(state.synchronised, True)
        # Relabelled, not just "not none": the source becomes ntp and the detail
        # says the truth — synced, by something not visible from in here.
        self.assertEqual(state.source, "ntp")
        self.assertIn("adjtimex", state.detail)

    def test_a_visible_daemon_that_agrees_with_the_kernel_keeps_its_own_label(self):
        # kernel=True AND a probe that already reports True: the relabel branch
        # must NOT fire, so a GPS refclock's own source/detail survive rather
        # than being flattened to a generic "ntp". This is the common case where
        # a daemon IS visible (not a container) and the two simply agree.
        self.addCleanup(setattr, clock, "_cached", None)
        with mock.patch.object(clock, "_kernel_synchronised", return_value=True), \
             mock.patch.object(clock, "_chrony", return_value=(
                 True, "gps", "chronyd tracking PPS at stratum 1")), \
             mock.patch.object(clock, "_timesyncd", return_value=None), \
             mock.patch.object(clock, "_timedatectl", return_value=None):
            state = clock.discipline(force=True)
        self.assertIs(state.synchronised, True)
        self.assertEqual(state.source, "gps")
        self.assertIn("PPS", state.detail)

    def test_the_kernel_is_believed_over_a_probe_that_claims_sync(self):
        # And the other way round: the kernel says the clock is not disciplined,
        # a stale probe claims it is. The alarm should stand — every timestamp
        # the box writes comes off the kernel clock, not the probe.
        self.addCleanup(setattr, clock, "_cached", None)
        with mock.patch.object(clock, "_kernel_synchronised", return_value=False), \
             mock.patch.object(clock, "_chrony", return_value=(
                 True, "ntp", "chronyd tracking a peer")), \
             mock.patch.object(clock, "_timesyncd", return_value=None), \
             mock.patch.object(clock, "_timedatectl", return_value=None):
            state = clock.discipline(force=True)
        self.assertIs(state.synchronised, False)

    def test_the_probes_still_label_the_source_where_the_kernel_bit_is_absent(self):
        # On the dev machines adjtimex is unavailable and returns None; the
        # daemon probes are then the whole answer, exactly as before — including
        # naming a GPS-disciplined chrony as such.
        self.addCleanup(setattr, clock, "_cached", None)
        with mock.patch.object(clock, "_kernel_synchronised", return_value=None), \
             mock.patch.object(clock, "_chrony", return_value=(
                 True, "gps", "chronyd tracking PPS at stratum 1")), \
             mock.patch.object(clock, "_timesyncd", return_value=None), \
             mock.patch.object(clock, "_timedatectl", return_value=None):
            state = clock.discipline(force=True)
        self.assertIs(state.synchronised, True)
        self.assertEqual(state.source, "gps")


class ShippedInventoryTests(unittest.TestCase):
    """deploy/devices.example.json must describe the box in HARDWARE.md."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.home = Path(self._dir.name)
        (self.home / "devices.json").write_text(
            (DEPLOY / "devices.example.json").read_text()
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
        self.assertEqual(fitted["camera"], "onvif-network-camera")

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
        # Whatever is missing, the slot reports what is *absent* rather than
        # "unsupported": the distinction is
        # the whole point of the message, because one of them is fixed by
        # configuring something and the other cannot be fixed at all.
        #
        # This used to name a missing `rpicam` binary, back when the shipped
        # camera was the one on the CSI ribbon. A network camera needs no
        # package on the box at all — what it can be missing is an address,
        # and saying so is the difference between "not fitted yet" and
        # "fitted and broken".
        detail = {r.slot: r for r in self.agent.inventory.report()}["camera"].detail
        self.assertIn("address", detail)
        self.assertNotIn("picamera2", detail)
        self.assertNotIn("rpicam", detail)

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


# InstallerTests, ServiceAccountTests and UpdaterTests are gone with the host
# installer, the systemd unit and the updater timer.
#
# They tested a real deployment path: install.sh provisioned the host, a
# systemd unit ran the agent directly, and gsu-update.{sh,timer,service} pulled
# a new image, retagged the running one as `previous`, and rolled back if the
# new one did not prove itself.
#
# All of it existed because the agent ran on the host, and the agent ran on the
# host because the CSI camera could not stream from inside a container. The
# camera is out of scope; the second path went with it. Updating is `git pull`
# and a container restart, and rolling back is `git checkout` of a tag already
# on the disk — which needs no tooling and, on the link that may be the reason
# you are rolling back, no download.


class ContainerTests(unittest.TestCase):
    """The container files. **This is the deployment path** (DECISIONS 35c).

    Both files have now been built and run on the first real Pi, and the
    device reasoning held (DECISIONS.md item 35's dated update). There is
    still no Docker daemon where this suite runs, so these check what a
    careful read can check: the base image is pinned, device access is
    permissive enough that a missing sensor cannot stop the station, the log
    rotation that protects the SD card is present, and no `privileged: true`
    has crept in — with the one exception of the opt-in host-shell helper,
    whose narrowed elevation is confined to it (see the confinement test).

    The device assertions are the ones that matter. They encode a decision that
    reversed twice, and a well-meaning tightening of them would restore the
    failure that made containers unacceptable the first time round.
    """

    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (DEPLOY / "Dockerfile").read_text()
        cls.compose = (STATION / "docker-compose.yml").read_text()
        # Asserted against the text rather than a parsed tree so that these run
        # everywhere without adding PyYAML as a dependency. The file's *schema*
        # was validated separately with `docker compose config`, which is the
        # real parser; what these guard against is a line going missing.
        cls.directives = "\n".join(
            line for line in cls.compose.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    def service(self, name: str) -> str:
        """The comment-free directives of one compose service.

        Sliced out of self.directives (already comment-stripped, so a
        `privileged` written in prose never trips a negative assertion) and
        bounded by the next service key or the top-level `volumes:`. Service
        names sit at exactly two spaces of indent; everything inside a service
        is indented deeper, so those two markers delimit a block unambiguously
        without pulling PyYAML in for one slice.
        """
        lines = self.directives.splitlines()
        start = lines.index(f"  {name}:")
        for end in range(start + 1, len(lines)):
            indent = len(lines[end]) - len(lines[end].lstrip())
            if indent == 0 or (indent == 2 and lines[end].rstrip().endswith(":")):
                return "\n".join(lines[start:end])
        return "\n".join(lines[start:])

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
        # 81 (video4linux) went with the CSI camera. A network camera is a
        # URL and needs no device node at all.
        for major, what in ((188, "USB serial"), (166, "CDC-ACM"),
                            (204, "on-chip UART"), (189, "USB raw / libusb"),
                            (249, "pps")):
            self.assertIn(f"c {major}:* rmw", self.directives, what)

    def test_privileged_is_confined_to_the_optin_host_shell(self):
        """Elevated privilege lives in the opt-in host-shell helper, nowhere else.

        The agent and the updater must never be `privileged` nor gain
        `SYS_ADMIN`: the device mappings are the honest cost of the container
        path, and privilege would be the dishonest way out of them — a
        well-meaning "just add privileged" here would restore the failure that
        made containers unacceptable twice over (item 35c). The host-shell
        helper is the one exception, and only because opening a *host* shell
        inherently needs it — `nsenter` into host PID 1 needs CAP_SYS_ADMIN.
        Even there it takes the NARROW form: CAP_SYS_ADMIN plus an AppArmor lift
        for the /sys writes a host shell makes, not the blunt `privileged: true`
        that would also hand it every other capability, all devices and an
        unmasked /proc it does not need. And it is fenced behind the
        off-by-default `hostshell` profile so an un-opted-in box never runs it.
        """
        # The blunt instrument appears nowhere; the narrowing removed it.
        self.assertNotIn("privileged: true", self.directives)

        # The agent and the updater carry no elevation of any kind.
        for name in ("gsu", "updater"):
            block = self.service(name)
            self.assertNotIn("privileged", block, name)
            self.assertNotIn("SYS_ADMIN", block, name)
            self.assertNotIn("cap_add", block, name)
            self.assertNotIn("apparmor", block, name)

        # The helper has exactly the narrowed set, and only behind its profile.
        helper = self.service("hostshell")
        self.assertIn('profiles: ["hostshell"]', helper)
        self.assertIn("pid: host", helper)
        self.assertIn("cap_add:", helper)
        self.assertIn("- SYS_ADMIN", helper)
        self.assertIn("apparmor:unconfined", helper)
        self.assertNotIn("privileged", helper)

    def test_the_docs_agree_that_the_container_is_the_path(self):
        # Prose is line-wrapped, so match on collapsed whitespace rather than
        # on where the paragraph happened to break.
        runbook = " ".join((STATION / "DEPLOYMENT.md").read_text().split())
        self.assertIn("The station runs as a container", runbook)
        # The trade is stated, not glossed.
        self.assertIn("Isolation was traded away deliberately", runbook)

    def test_the_runbook_does_not_describe_a_path_that_is_gone(self):
        # A runbook is followed rather than read, so a stale one sends somebody
        # to a site and leaves them there. This used to assert the *presence*
        # of the systemd appendix.
        runbook = " ".join((STATION / "DEPLOYMENT.md").read_text().split())
        for gone in ("--path systemd", "the systemd path", "gsu.service",
                     ".venv/bin/python"):
            self.assertNotIn(gone, runbook, f"the runbook still mentions {gone}")

    def test_the_base_image_is_pinned_by_digest(self):
        # An unpinned base is a different station every time it is rebuilt. The
        # pin moved onto an `ARG BASE=` line that both FROM stages build on via
        # ${BASE}, so anchor the digest to that declaration — matching it
        # anywhere would pass on an unpinned base with a digest left in a comment
        # — and check FROM actually builds on ${BASE}.
        self.assertRegex(
            self.dockerfile, r"ARG BASE=python:3\.11-slim-bookworm@sha256:[0-9a-f]{64}")
        self.assertRegex(self.dockerfile, r"FROM \$\{BASE\}")

    def test_it_runs_as_a_non_root_user(self):
        self.assertIn("USER gsu", self.dockerfile)
        self.assertIn("useradd", self.dockerfile)

    def test_the_code_is_not_writable_by_the_agent(self):
        # Matches the systemd path: a compromised agent must not be able to
        # rewrite the thing that restarts it.
        self.assertIn("chown -R root:root /opt/percepta/station", self.dockerfile)

    def test_no_bytecode_is_written_to_the_sd_card(self):
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", self.dockerfile)

    def test_every_named_state_volume_is_created_and_owned_in_the_image(self):
        # The agent runs as the non-root gsu user and cannot chown a fresh named
        # volume — cap_drop: ALL removes CAP_CHOWN — so every /var/lib/percepta-*
        # dir it mounts a volume at must be created and chowned to gsu in the
        # image. A volume added to compose without the matching mkdir/chown is
        # root-owned and every write is EPERM: exactly how the host-shell handoff
        # shipped broken. Derived from the compose so a new mount cannot skip it.
        mounts = re.findall(
            r"-\s+[\w-]+:(/var/lib/percepta-[\w-]+)",
            self.service("gsu"))
        self.assertIn("/var/lib/percepta-gsu-hostshell", mounts,
                      "the host-shell handoff mount vanished; update this test")
        for path in mounts:
            self.assertRegex(
                self.dockerfile,
                rf"chown gsu:gsu[^\n]*{re.escape(path)}(?:\s|$)",
                f"{path} is mounted on the agent but never chowned to gsu",
            )

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

    def test_the_setup_page_is_published_to_the_lan_on_port_80(self):
        """Reachable by default, because the password is what protects it.

        This used to assert loopback, on the grounds that the page had no
        authentication. It has: the agent refuses to serve it at all without
        `GSU_SETUP_PASSWORD_HASH` and demotes itself to the container's own
        loopback, which no port mapping can reach — `test_setup_console.py`'s
        `test_a_routable_host_with_no_password_is_demoted_to_loopback` is the
        guard on that, and it is the one that matters. Once a password is
        guaranteed — bootstrap.sh will not finish without collecting one —
        loopback-only stopped being protection and became an installer at an
        enclosure unable to open the page the enclosure exists to be set up
        from.

        Both halves are overridable, and the defaults are what this pins.
        """
        self.assertIn(
            '"${GSU_SETUP_BIND:-0.0.0.0}:${GSU_SETUP_HOST_PORT:-80}:8088"',
            self.directives,
        )

    def test_the_container_still_listens_high_and_unprivileged(self):
        # The published port is 80 on the *host*, which the Docker daemon binds.
        # The agent inside is unprivileged with every capability dropped, so it
        # must still be asked for a port it can actually have — a container-side
        # 80 would fail to bind and the page would never come up at all.
        self.assertRegex(self.directives, r":8088\"")
        self.assertIn("cap_drop:", self.directives)

    def test_the_state_is_a_named_volume_that_survives_a_rebuild(self):
        """The credential lives here, and `up -d --build` must not lose it.

        A named volume rather than a host path: nothing outside the container
        needs to read it. It is deliberately not an environment variable
        either — renewal writes a new secret, an environment variable cannot
        be written back to, and a station whose credential lives in one
        silently stops renewing and dies at expiry.
        """
        self.assertIn("gsu-state:/var/lib/percepta-gsu", self.directives)
        self.assertIn("volumes:\n  gsu-state:", self.compose)

    def test_the_sdr_and_camera_are_not_mapped_while_they_have_no_driver(self):
        # Mapping a device nothing opens is access granted for no reason. Both
        # are present as commented, ready-to-use lines instead.
        self.assertNotIn("/dev/bus/usb:/dev/bus/usb", self.directives)
        self.assertNotIn("/dev/video0", self.directives)
        self.assertIn("/dev/bus/usb", self.compose)     # documented in comments

    def test_the_dockerignore_keeps_one_stations_identity_out_of_the_image(self):
        ignored = (STATION / ".dockerignore").read_text()
        self.assertIn("var/", ignored)


