"""The setup GUI, and the four controls that stop it being the weakest surface
on the system.

This is the page an installer uses with a laptop and no terminal, which means it
is also the page an attacker finds if any of `gsu/setup_access.py`'s controls
regress. Everything here is a control that has to keep working, written as the
failure it prevents rather than as the code it exercises.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from gsu.agent import Agent
from gsu.config import AgentConfig
from gsu.console import SLOT_LABELS, Console, _host_is_addressable
from gsu.setup_access import (
    COOKIE_NAME,
    Gate,
    classify,
    hash_password,
    is_loopback_host,
    verify_password,
)


class PeerClassificationTests(unittest.TestCase):
    """Which source addresses count as "somebody standing next to the box"."""

    def test_loopback_and_the_private_ranges_are_local(self):
        self.assertEqual(classify("127.0.0.1"), "loopback")
        self.assertEqual(classify("::1"), "loopback")
        for address in ("10.1.2.3", "172.20.0.5", "192.168.1.50", "169.254.9.9"):
            self.assertEqual(classify(address), "local", address)
        self.assertEqual(classify("fe80::1"), "local")
        self.assertEqual(classify("fd00::5"), "local")

    def test_carrier_grade_nat_is_not_local(self):
        # The one that a stdlib `is_private` check gets wrong. 100.64/10 is the
        # carrier's shared range: on a Starlink site every other subscriber is
        # in it, and none of them is standing next to this box.
        self.assertEqual(classify("100.64.0.1"), "public")
        self.assertEqual(classify("100.127.255.254"), "public")

    def test_the_public_internet_is_public(self):
        for address in ("8.8.8.8", "203.0.113.7", "2001:db8::1"):
            self.assertEqual(classify(address), "public", address)

    def test_an_unreadable_peer_is_treated_as_public(self):
        # Fail closed. An address this cannot parse is not one to trust.
        self.assertEqual(classify(""), "public")
        self.assertEqual(classify(None), "public")
        self.assertEqual(classify("not-an-address"), "public")

    def test_a_v4_peer_on_a_dual_stack_socket_is_judged_as_v4(self):
        # What a bind to :: produces. Judged wrong, a LAN laptop would be
        # refused and a public peer admitted.
        self.assertEqual(classify("::ffff:192.168.1.50"), "local")
        self.assertEqual(classify("::ffff:8.8.8.8"), "public")

    def test_binding_to_every_interface_is_not_loopback(self):
        # The mistake this catches: treating 0.0.0.0 as "local" and skipping
        # the password requirement, which is the whole exposure.
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("::"))
        self.assertFalse(is_loopback_host(""))
        self.assertFalse(is_loopback_host("192.168.1.50"))


class PasswordTests(unittest.TestCase):
    def test_a_hash_verifies_its_own_password_and_nothing_else(self):
        spec = hash_password("correct horse battery", iterations=1000)
        self.assertTrue(verify_password(spec, "correct horse battery"))
        self.assertFalse(verify_password(spec, "correct horse batter"))
        self.assertFalse(verify_password(spec, ""))

    def test_a_plain_value_is_accepted_but_is_not_the_hash_format(self):
        self.assertTrue(verify_password("hunter2hunter2", "hunter2hunter2"))
        self.assertFalse(verify_password("hunter2hunter2", "something else"))

    def test_a_malformed_hash_fails_closed(self):
        # A typo in the environment file must lock the page, not open it.
        self.assertFalse(verify_password("pbkdf2_sha256$notanumber$aa$bb", "x"))
        self.assertFalse(verify_password("pbkdf2_sha256$1000$zz$bb", "x"))

    def test_no_password_configured_never_verifies(self):
        self.assertFalse(verify_password(None, "anything"))
        self.assertFalse(verify_password("", "anything"))


class GateTests(unittest.TestCase):
    """The rules, without an HTTP server in the way."""

    def gate(self, **kwargs) -> Gate:
        kwargs.setdefault("password", hash_password("a-good-password", iterations=1000))
        kwargs.setdefault("enrolled", lambda: True)
        return Gate(**kwargs)

    def test_a_public_peer_is_refused_whatever_it_sends(self):
        gate = self.gate()
        self.assertFalse(gate.authorise("8.8.8.8", None).allow)
        self.assertEqual(gate.authorise("8.8.8.8", None).status, 403)
        # And it cannot even try the password.
        self.assertFalse(gate.login("8.8.8.8", "a-good-password").allow)

    def test_loopback_needs_no_password_but_still_gets_a_session(self):
        # It arrived over SSH, which has already authenticated it. The session
        # exists so that the page has a CSRF token — an SSH tunnel puts this
        # page inside an ordinary browser where other tabs can post to it.
        decision = self.gate().authorise("127.0.0.1", None)
        self.assertTrue(decision.allow)
        self.assertTrue(decision.set_cookie)
        self.assertIsNotNone(decision.session)

    def test_a_lan_peer_with_no_cookie_is_sent_to_the_login_form(self):
        decision = self.gate().authorise("192.168.1.50", None)
        self.assertFalse(decision.allow)
        self.assertTrue(decision.login)
        self.assertEqual(decision.status, 401)

    def test_a_lan_peer_gets_in_with_the_password_and_not_without(self):
        gate = self.gate()
        self.assertFalse(gate.login("192.168.1.50", "wrong").allow)
        decision = gate.login("192.168.1.50", "a-good-password")
        self.assertTrue(decision.allow)
        cookie = f"{COOKIE_NAME}={decision.session.token}"
        self.assertTrue(gate.authorise("192.168.1.50", cookie).allow)

    def test_a_loopback_cookie_does_not_authorise_a_lan_peer(self):
        # Otherwise an SSH tunnel session, or a cookie lifted from one, would
        # be a LAN credential.
        gate = self.gate()
        loopback = gate.authorise("127.0.0.1", None).session
        cookie = f"{COOKIE_NAME}={loopback.token}"
        self.assertFalse(gate.authorise("192.168.1.50", cookie).allow)

    def test_guessing_locks_the_peer_out(self):
        gate = self.gate()
        for _ in range(5):
            gate.login("192.168.1.50", "wrong")
        decision = gate.login("192.168.1.50", "a-good-password")
        self.assertFalse(decision.allow, "the correct password after a lockout")
        self.assertEqual(decision.status, 429)

    def test_a_csrf_token_is_bound_to_its_own_session(self):
        gate = self.gate()
        one = gate.authorise("127.0.0.1", None).session
        two = gate._new_session("loopback", "127.0.0.1", True)
        self.assertTrue(gate.check_csrf(one, gate.csrf_token(one)))
        self.assertFalse(gate.check_csrf(one, gate.csrf_token(two)))
        self.assertFalse(gate.check_csrf(one, ""))
        self.assertFalse(gate.check_csrf(None, gate.csrf_token(one)))

    def test_the_window_does_not_run_down_before_the_station_is_enrolled(self):
        gate = self.gate(window_minutes=30.0, enrolled=lambda: False)
        gate._deadline = 0.0            # long expired
        self.assertTrue(gate.window_open())
        self.assertIsNone(gate.seconds_left())

    def test_the_window_closes_once_enrolled_and_idle(self):
        gate = self.gate(window_minutes=30.0, enrolled=lambda: True)
        self.assertTrue(gate.window_open())
        gate._deadline = 0.0
        self.assertFalse(gate.window_open())
        self.assertFalse(gate.authorise("192.168.1.50", None).allow)

    def test_the_marker_file_reopens_the_window_once_and_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "setup-open"
            gate = self.gate(window_minutes=30.0, reopen_path=marker)
            gate._deadline = 0.0
            self.assertFalse(gate.window_open())
            marker.touch()
            self.assertTrue(gate.window_open())
            self.assertFalse(marker.exists(), "the marker must not persist")

    def test_an_authenticated_request_holds_the_window_open(self):
        gate = self.gate(window_minutes=30.0)
        decision = gate.login("192.168.1.50", "a-good-password")
        cookie = f"{COOKIE_NAME}={decision.session.token}"
        gate._deadline = gate._deadline - 1700     # nearly closed, not closed
        self.assertTrue(gate.authorise("192.168.1.50", cookie).allow)
        self.assertGreater(gate.seconds_left(), 1700)

    def test_a_cookie_cannot_reopen_a_window_that_has_already_closed(self):
        """Closed is closed. Reopening is a deliberate act, not a stale tab.

        Only reachable in the seconds between the deadline passing and the
        watcher taking the socket away — but if a cookie could reopen it, a
        laptop left connected on the bench would hold the door open for ever
        by refreshing, which is exactly the permanent back door this design
        exists to refuse.
        """
        gate = self.gate(window_minutes=30.0)
        decision = gate.login("192.168.1.50", "a-good-password")
        cookie = f"{COOKIE_NAME}={decision.session.token}"
        gate._deadline = 0.0
        self.assertFalse(gate.authorise("192.168.1.50", cookie).allow)
        self.assertFalse(gate.window_open())

    def test_an_unauthenticated_poll_does_not_hold_the_window_open(self):
        # Otherwise anything on the LAN could keep the door open indefinitely
        # by asking for the login page in a loop.
        gate = self.gate(window_minutes=30.0)
        gate._deadline = 0.0
        gate.authorise("192.168.1.50", None)
        self.assertFalse(gate.window_open())
        gate.authorise("192.168.1.50", f"{COOKIE_NAME}=made-up-token")
        self.assertFalse(gate.window_open())


class HostHeaderTests(unittest.TestCase):
    """DNS rebinding: a public page pointing its own name at this box."""

    def test_addresses_and_link_local_names_are_accepted(self):
        for host in ("127.0.0.1:8088", "192.168.1.50", "localhost:8088",
                     "[::1]:8088", "gsu-01.local:8088"):
            self.assertTrue(_host_is_addressable(host), host)

    def test_a_public_name_is_refused(self):
        # Rebinding needs a name, because it works by changing what a name
        # resolves to. Refusing names that are not link-local removes it.
        for host in ("evil.example.com", "attacker.test:8088", ""):
            self.assertFalse(_host_is_addressable(host), host)


class BindingTests(unittest.TestCase):
    """Where the socket is allowed to be. The safety property everything else
    rests on: no password, no listener on a routable interface — ever."""

    def console(self, **kwargs) -> Console:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        agent = Agent(AgentConfig(
            home=Path(directory.name), setup_enabled=False, single_instance=False,
        ))
        self.addCleanup(agent.shutdown)
        kwargs.setdefault("port", 0)
        return Console(agent, **kwargs)

    def test_a_routable_host_with_no_password_is_demoted_to_loopback(self):
        console = self.console(host="0.0.0.0")
        host, reason = console._target_host()
        self.assertEqual(host, "127.0.0.1")
        self.assertIn("GSU_SETUP_PASSWORD_HASH", reason)

    def test_a_routable_host_with_a_password_is_honoured(self):
        console = self.console(host="0.0.0.0", password="a-good-password")
        host, reason = console._target_host()
        self.assertEqual(host, "0.0.0.0")
        self.assertEqual(reason, "")

    def test_a_closed_window_takes_the_routable_listener_away(self):
        console = self.console(host="0.0.0.0", password="a-good-password")
        console.gate.enrolled = lambda: True
        console.gate._deadline = 0.0
        host, reason = console._target_host()
        self.assertEqual(host, "127.0.0.1")
        self.assertIn("setup window has closed", reason)

    def test_loopback_is_never_demoted_so_the_ssh_path_always_works(self):
        console = self.console(host="127.0.0.1")
        console.gate.enrolled = lambda: True
        console.gate._deadline = 0.0
        self.assertEqual(console._target_host(), ("127.0.0.1", ""))


class WindowLifecycleTests(unittest.TestCase):
    """The socket really moves. Not "starts returning 403" — moves.

    A port that answers is a port somebody enumerates, so the whole point of
    the window is that the listener stops existing on the LAN. That is a
    property of sockets rather than of handler logic, so it is tested against
    a real one.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.agent = Agent(AgentConfig(
            home=self.home, setup_enabled=False, single_instance=False,
        ))
        self.addCleanup(self.agent.shutdown)
        # The watcher's own period, shortened so this takes a second rather
        # than half a minute.
        patcher = mock.patch("gsu.console.WATCH_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def settle(self, console, expected: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if console.bound_host == expected:
                return console.bound_host
            time.sleep(0.02)
        return console.bound_host

    def test_the_listener_moves_off_the_lan_and_back_on_the_marker(self):
        marker = self.home / "setup-open"
        console = Console(
            self.agent, "0.0.0.0", 0,
            password=hash_password("a-good-password", iterations=1000),
            window_minutes=30.0, reopen_path=marker,
        )
        console.gate.enrolled = lambda: True
        console.start()
        self.addCleanup(console.stop)
        self.assertEqual(console.bound_host, "0.0.0.0")

        console.gate._deadline = 0.0                      # the window expires
        self.assertEqual(self.settle(console, "127.0.0.1"), "127.0.0.1")
        # Loopback is still there: the SSH path and the update gate must not
        # go away with the window.
        self.assertIsNotNone(console._server)

        marker.touch()                                    # the deliberate act
        self.assertEqual(self.settle(console, "0.0.0.0"), "0.0.0.0")
        self.assertFalse(marker.exists())

    def test_sessions_do_not_survive_the_window_closing(self):
        # A laptop left connected on the bench must not walk back in when
        # somebody reopens the page six months later.
        console = Console(
            self.agent, "0.0.0.0", 0,
            password=hash_password("a-good-password", iterations=1000),
            window_minutes=30.0,
        )
        console.gate.enrolled = lambda: True
        console.start()
        self.addCleanup(console.stop)
        token = console.gate.login("192.168.1.50", "a-good-password").session.token
        console.gate._deadline = 0.0
        self.settle(console, "127.0.0.1")
        self.assertNotIn(token, console.gate._sessions)

    def test_stop_leaves_nothing_listening(self):
        console = Console(
            self.agent, "0.0.0.0", 0,
            password=hash_password("a-good-password", iterations=1000),
        )
        console.start()
        port = console._server.server_address[1]
        console.stop()
        self.assertIsNone(console._server)
        # And the watcher does not race the shutdown back into existence.
        time.sleep(0.3)
        self.assertIsNone(console._server)
        probe = socket.socket()
        probe.settimeout(1.0)
        with self.assertRaises(OSError):
            probe.connect(("127.0.0.1", port))
        probe.close()


class ServedPageTests(unittest.TestCase):
    """The real handler, over a real socket."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.agent = Agent(AgentConfig(
            home=Path(self.directory.name), setup_enabled=False,
            single_instance=False,
        ))
        self.addCleanup(self.agent.shutdown)
        self.console = Console(
            self.agent, "127.0.0.1", 0,
            password=hash_password("a-good-password", iterations=1000),
        )
        self.console.start()
        self.addCleanup(self.console.stop)
        self.port = self.console._server.server_address[1]

    # --- helpers ---

    def request(self, method: str, path: str, body: str | None = None,
                headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        payload = response.read().decode()
        response.close()
        connection.close()
        return response, payload

    def page(self, path: str = "/devices"):
        """A page and its session. /devices by default: it always carries a
        form, so it is where a CSRF token can be scraped from."""
        response, body = self.request("GET", path)
        cookie = response.getheader("Set-Cookie") or ""
        token = cookie.split(";")[0]
        csrf = body.split("name=csrf value='")[1].split("'")[0]
        return token, csrf, body

    def every_page(self):
        from gsu.console import PAGES

        return {path: self.request("GET", path)[1] for path in PAGES}

    # --- what a technician on an SSH tunnel sees ---

    def test_the_devices_page_offers_every_slot_as_a_sub_tab(self):
        from gsu.devices import registry

        _, _, body = self.page("/devices")
        for slot in registry.SLOTS:
            self.assertIn(f"href='/devices?slot={slot}'", body)
        # An unknown slot lands on the first tab rather than erroring.
        response, body = self.request("GET", "/devices?slot=../etc")
        self.assertEqual(response.status, 200)
        self.assertIn(f"<strong>{SLOT_LABELS[registry.SLOTS[0]]}</strong>", body)

    def test_each_slot_tab_offers_its_own_devices_from_the_registry(self):
        from gsu.devices import registry

        for slot in registry.SLOTS:
            _, body = self.request("GET", f"/devices?slot={slot}")
            self.assertIn(f"<strong>{SLOT_LABELS[slot]}</strong>", body)
            self.assertIn(f"href='/devices?slot={slot}' class=active", body)
            # Driven from the registry, not from a second list in the template.
            for device in registry.by_slot(slot):
                self.assertIn(f"value='{device.id}'", body, f"{slot}: {device.id}")

    def test_every_slot_tab_carries_its_live_element(self):
        # The datastream field everywhere it is the sensor's own voice; on the
        # camera tab a frame preview instead — the camera's raw tap is capture
        # statistics, and a picture answers the question actually being asked.
        from gsu.devices import registry

        for slot in registry.SLOTS:
            _, body = self.request("GET", f"/devices?slot={slot}")
            if slot == "camera":
                self.assertIn("id=preview-wrap", body)
                self.assertNotIn(f"data-slot='{slot}'", body)
            else:
                self.assertIn(f"data-slot='{slot}'", body, slot)

    def test_the_devices_script_is_admitted_by_the_responses_own_nonce(self):
        # One inline script, one nonce, minted per response: the header and
        # the tag must agree, and two responses must not share one.
        response, body = self.request("GET", "/devices")
        csp = response.getheader("Content-Security-Policy") or ""
        self.assertIn("script-src 'nonce-", csp)
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f"<script nonce='{nonce}'>", body)
        second, _ = self.request("GET", "/devices")
        self.assertNotEqual(
            csp, second.getheader("Content-Security-Policy"),
            "a reused nonce is a nonce an injected payload can learn",
        )
        # Pages without the script advertise no script source at all.
        response, _ = self.request("GET", "/")
        self.assertNotIn("script-src",
                         response.getheader("Content-Security-Policy") or "")

    def test_without_javascript_the_save_button_is_simply_enabled(self):
        # The no-JS path is acceptable degradation, which means the rendered
        # button must not be disabled — only the script may disable it.
        _, _, body = self.page("/devices")
        self.assertIn("<button type=submit>Save</button>", body)
        self.assertNotIn("<button type=submit disabled", body)

    def test_status_json_carries_the_raw_samples_the_script_polls(self):
        from gsu.devices import registry

        self.agent.step(1.0, weather_due=True)   # give the sensors a tick
        _, body = self.request("GET", "/status.json")
        samples = json.loads(body)["raw_samples"]
        self.assertEqual(set(samples), set(registry.SLOTS))
        self.assertTrue(samples["weather"], "a connected sensor shows its data")
        for slot in registry.SLOTS:
            for line in samples[slot]:
                self.assertLessEqual(len(line), 200)

    def test_the_camera_tab_carries_the_camera_section(self):
        _, body = self.request("GET", "/devices?slot=camera")
        self.assertIn("Camera</h2>", body)
        _, body = self.request("GET", "/devices?slot=weather")
        self.assertNotIn("Camera</h2>", body)

    def test_the_light_tab_offers_the_current_sense_fields(self):
        _, body = self.request("GET", "/devices?slot=light")
        for name in ("p_sense_source", "p_sense_threshold_a", "p_state_source"):
            self.assertIn(name, body, name)
        # The state source offers exactly the two witnesses there are.
        self.assertIn(">relay</option>", body)
        self.assertIn(">current</option>", body)

    # --- the camera preview ---

    def test_the_camera_tab_shows_the_preview_and_then_the_frame(self):
        # Before anything has been captured: an empty state, not a broken
        # image, and the CSS-only zoom mechanism is already in place.
        _, body = self.request("GET", "/devices?slot=camera")
        self.assertIn("no frame yet", body)
        self.assertIn("zoom-toggle", body)
        self.assertNotIn("src='/frame.jpg'", body)
        # After a capture: the picture and its age.
        self.agent.video.cycle()
        _, body = self.request("GET", "/devices?slot=camera")
        self.assertIn("src='/frame.jpg'", body)
        self.assertIn("s old", body)
        self.assertNotIn("no frame yet", body)
        # And status.json carries what the refresher script needs.
        _, status = self.request("GET", "/status.json")
        video = json.loads(status)["video"]
        self.assertTrue(video["has_frame"])
        self.assertIsInstance(video["frame_age_s"], (int, float))

    def test_the_frame_endpoint_serves_the_cached_frame_with_its_age(self):
        response, _ = self.request("GET", "/frame.jpg")
        self.assertEqual(response.status, 404, "no frame yet is a 404, not a page")
        self.agent.video.cycle()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/frame.jpg",
                           headers={"Host": f"127.0.0.1:{self.port}"})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        self.assertEqual(response.getheader("Cache-Control"), "no-store",
                         "the newest frame is the only one worth anything")
        self.assertGreaterEqual(float(response.getheader("X-Frame-Age")), 0.0)
        self.assertEqual(data, self.agent.video.last_frame.jpeg)

    def test_the_frame_endpoint_never_captures(self):
        # It serves what the publisher took. A camera held by the live stream
        # must not be touched by a page poll — so the endpoint must have no
        # capture path at all.
        self.agent.video.cycle()

        class Exploding:
            def capture(self):
                raise AssertionError("the frame endpoint captured on demand")

        self.agent.camera = Exploding()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/frame.jpg",
                           headers={"Host": f"127.0.0.1:{self.port}"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 200)

    def test_the_summary_page_has_a_line_per_slot(self):
        from gsu.devices import registry

        _, body = self.request("GET", "/")
        for slot in registry.SLOTS:
            # Named for people here too, not just on the Devices page.
            self.assertIn(f"<span class=k>{SLOT_LABELS[slot]}</span>", body)
            self.assertNotIn(f"<span class=k>{slot}</span>", body)

    def test_the_enrolment_field_is_there_before_the_station_is_enrolled(self):
        _, body = self.request("GET", "/connection")
        self.assertIn("XXXX-XXXX-XXXX", body)
        # And the summary page points at it rather than duplicating it.
        _, summary = self.request("GET", "/")
        self.assertIn("/connection", summary)

    def test_the_platform_address_is_shown_and_is_not_editable(self):
        """There is one platform. An installer confirms it; nobody retypes it."""
        pages = self.every_page()
        self.assertIn(self.agent.config.platform_url, pages["/connection"])
        # Every editable control on every page, by name. None of them is the
        # platform or the broker address.
        import re

        names = set()
        for body in pages.values():
            names |= set(
                re.findall(r"<(?:input|select)[^>]*\bname=[\'\"]?([\w.-]+)", body)
            )
        self.assertTrue(names, "the pages have no form controls at all")
        for forbidden in ("platform", "platform_url", "broker", "broker_url", "url"):
            self.assertNotIn(forbidden, names)

    def test_the_camera_backend_reason_reaches_the_page(self):
        # "Why is the camera slow" answered without an SSH session. Rendered
        # from the driver's own words: the specific fault is a venv built
        # without --system-site-packages, which looks like slow hardware.
        state = self.agent.snapshot()
        self.assertIn("backend_reason", state["video"]["camera"])
        state["video"]["camera"] = {
            "backend": "cli",
            "backend_reason": "picamera2 is not importable from this virtual "
                              "environment, which was created without "
                              "--system-site-packages",
        }
        section = self.console._section_camera(state)
        self.assertIn("Capture path", section)
        self.assertIn("--system-site-packages", section)
        self.assertIn("cli", section)

    def test_every_response_carries_the_headers_that_matter(self):
        response, _ = self.request("GET", "/")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        # same-origin, not no-referrer: Chrome redacts the Origin header to
        # "null" under no-referrer even on same-origin form posts, which made
        # _same_origin refuse every real browser's login while curl passed.
        self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")
        self.assertIn("default-src 'none'",
                      response.getheader("Content-Security-Policy") or "")
        cookie = response.getheader("Set-Cookie") or ""
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_the_update_gate_can_still_read_status_json_over_loopback(self):
        # deploy/gsu-update.sh polls this to decide whether to keep a new
        # image. Breaking it means every update rolls back.
        response, body = self.request("GET", "/status.json")
        self.assertEqual(response.status, 200)
        self.assertIn("enrolled", json.loads(body))

    def test_an_unknown_path_is_a_404_and_not_the_page(self):
        response, _ = self.request("GET", "/wp-login.php")
        self.assertEqual(response.status, 404)

    # --- the four pages and the router that serves them ---

    def test_each_page_renders_its_own_content(self):
        markers = {
            "/": "Slots",
            "/connection": "Where this box talks",
            "/devices": "What is fitted",
            "/logging": "Recent events",
        }
        for path, marker in markers.items():
            response, body = self.request("GET", path)
            self.assertEqual(response.status, 200, path)
            self.assertIn(marker, body, path)
            # The strip knows which page it is on.
            self.assertIn(f"href='{path}' class=active", body, path)

    def test_the_old_aliases_land_on_the_summary(self):
        # /index.html and /login predate the split; a bookmark of either must
        # keep working, and both are the summary.
        for path in ("/index.html", "/login"):
            response, body = self.request("GET", path)
            self.assertEqual(response.status, 200, path)
            self.assertIn("— Summary</title>", body, path)

    def test_each_post_redirects_back_to_the_page_its_form_lives_on(self):
        token, csrf, _ = self.page()
        response, _ = self.request(
            "POST", "/device", f"slot=weather&type_id=airmar-110wx&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 303)
        # And to the sub-tab the form lives on, not the page's first tab.
        self.assertEqual(response.getheader("Location"), "/devices?slot=weather")
        token, csrf, _ = self.page()
        response, _ = self.request(
            "POST", "/enrol", f"token=&csrf={csrf}", {"Cookie": token},
        )
        self.assertEqual(response.getheader("Location"), "/connection")
        token, csrf, _ = self.page()
        response, _ = self.request(
            "POST", "/logout", f"csrf={csrf}", {"Cookie": token},
        )
        self.assertEqual(response.getheader("Location"), "/")

    def test_a_stale_csrf_post_still_lands_back_on_the_same_page(self):
        # The refusal and the message about it must appear where the person
        # is, not on a page they were never reading.
        token, _, _ = self.page()
        response, _ = self.request(
            "POST", "/device", "slot=weather&type_id=&csrf=stale",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/devices")

    def test_an_unknown_post_action_is_a_404(self):
        token, csrf, _ = self.page()
        response, _ = self.request(
            "POST", "/definitely-not", f"csrf={csrf}", {"Cookie": token},
        )
        self.assertEqual(response.status, 404)

    def test_the_logging_page_lists_events_newest_first_in_local_time(self):
        self.agent.store.record_event("test.first", "info", "one")
        self.agent.store.record_event("test.second", "warning", "two")
        _, body = self.request("GET", "/logging")
        self.assertIn("test.first", body)
        self.assertIn("test.second", body)
        # Newest first: the second event appears above the first.
        self.assertLess(body.index("test.second"), body.index("test.first"))
        # The zone the timestamps are in is named on the page.
        zone = datetime.now().astimezone().tzname() or "local time"
        self.assertIn(zone, body)

    # --- CSRF ---

    def test_a_post_without_a_csrf_token_changes_nothing(self):
        token, _, _ = self.page()
        before = dict(self.agent.inventory.fitted)
        response, _ = self.request(
            "POST", "/device", "slot=weather&type_id=airmar-110wx",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(
            self.agent.inventory.fitted["weather"].type_id,
            before["weather"].type_id,
        )

    def test_a_post_with_the_right_csrf_token_is_applied(self):
        token, csrf, _ = self.page()
        response, _ = self.request(
            "POST", "/device", f"slot=weather&type_id=airmar-110wx&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(
            self.agent.inventory.fitted["weather"].type_id, "airmar-110wx"
        )

    def test_another_sessions_csrf_token_does_not_work(self):
        token, _, _ = self.page()
        _, other_csrf, _ = self.page()          # a different session's token
        response, _ = self.request(
            "POST", "/device",
            f"slot=weather&type_id=airmar-110wx&csrf={other_csrf}",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 303)
        self.assertNotEqual(
            self.agent.inventory.fitted["weather"].type_id, "airmar-110wx"
        )

    def test_a_cross_origin_post_is_refused_outright(self):
        token, csrf, _ = self.page()
        response, _ = self.request(
            "POST", "/device", f"slot=weather&type_id=airmar-110wx&csrf={csrf}",
            {"Cookie": token, "Origin": "http://evil.example.com"},
        )
        self.assertEqual(response.status, 403)

    # --- input handling ---

    def test_a_rebinding_host_header_is_refused(self):
        response, _ = self.request("GET", "/", headers={"Host": "evil.example.com"})
        self.assertEqual(response.status, 400)

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        # Content-Length is attacker-controlled and this box has 1 GB of RAM.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.putrequest("POST", "/device")
        connection.putheader("Host", f"127.0.0.1:{self.port}")
        connection.putheader("Content-Length", str(500 * 1024 * 1024))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()

    def test_an_unknown_slot_or_device_is_rejected(self):
        token, csrf, _ = self.page()
        self.request("POST", "/device", f"slot=../../etc&type_id=&csrf={csrf}",
                     {"Cookie": token})
        self.assertNotIn("../../etc", self.agent.inventory.fitted)
        self.request(
            "POST", "/device", f"slot=weather&type_id=made-up&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertNotEqual(
            self.agent.inventory.fitted["weather"].type_id, "made-up"
        )

    # --- secrets ---

    def test_a_stored_camera_password_is_never_rendered_back(self):
        token, csrf, _ = self.page()
        self.request(
            "POST", "/device",
            "slot=camera&type_id=onvif-network-camera&p_address=192.168.1.9"
            f"&p_username=admin&p_password=s3cr3t-camera-pw&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(
            self.agent.inventory.fitted["camera"].params["password"],
            "s3cr3t-camera-pw",
        )
        _, _, body = self.page("/devices?slot=camera")
        self.assertNotIn("s3cr3t-camera-pw", body)
        self.assertIn("Stored. Blank keeps it.", body)
        response, status = self.request("GET", "/status.json")
        self.assertNotIn("s3cr3t-camera-pw", status)

    def test_saving_again_with_a_blank_password_keeps_the_stored_one(self):
        # The consequence of never rendering it: a blank box must mean
        # "unchanged", or every unrelated save wipes a working camera.
        token, csrf, _ = self.page()
        self.request(
            "POST", "/device",
            "slot=camera&type_id=onvif-network-camera&p_address=192.168.1.9"
            f"&p_password=s3cr3t-camera-pw&csrf={csrf}",
            {"Cookie": token},
        )
        token, csrf, _ = self.page()
        self.request(
            "POST", "/device",
            "slot=camera&type_id=onvif-network-camera&p_address=192.168.1.10"
            f"&csrf={csrf}",
            {"Cookie": token},
        )
        fitted = self.agent.inventory.fitted["camera"]
        self.assertEqual(fitted.params["password"], "s3cr3t-camera-pw")
        self.assertEqual(fitted.params["address"], "192.168.1.10")

    def test_a_url_pasted_with_credentials_is_split_and_never_echoed(self):
        # Vendors hand installers the whole rtsp://user:pass@host line. The
        # URL is stored without its userinfo and the credentials move into
        # the fields that are stored once and never rendered — otherwise the
        # address box, which IS echoed back, would carry the password.
        token, csrf, _ = self.page()
        self.request(
            "POST", "/device",
            "slot=camera&type_id=onvif-network-camera"
            "&p_address=rtsp://admin:url-secret-pw@192.168.1.9/Streaming/1"
            f"&csrf={csrf}",
            {"Cookie": token},
        )
        fitted = self.agent.inventory.fitted["camera"]
        self.assertEqual(fitted.params["address"], "rtsp://192.168.1.9/Streaming/1")
        self.assertEqual(fitted.params["username"], "admin")
        self.assertEqual(fitted.params["password"], "url-secret-pw")
        _, _, body = self.page("/devices?slot=camera")
        self.assertNotIn("url-secret-pw", body)
        self.assertIn("value='rtsp://192.168.1.9/Streaming/1'", body)
        self.assertIn("Stored. Blank keeps it.", body)
        _, status = self.request("GET", "/status.json")
        self.assertNotIn("url-secret-pw", status)

    def test_the_save_message_says_the_credentials_moved(self):
        token, csrf, _ = self.page()
        self.request(
            "POST", "/device",
            "slot=camera&type_id=onvif-network-camera"
            f"&p_address=rtsp://admin:pw@192.168.1.9/ch1&csrf={csrf}",
            {"Cookie": token},
        )
        _, _, body = self.page("/devices?slot=camera")
        self.assertIn("credentials moved into the username", body)

    def test_a_typed_password_beats_the_one_in_the_url(self):
        # Both arrived on the same save, from the same person; the separate
        # field is the more deliberate act.
        token, csrf, _ = self.page()
        self.request(
            "POST", "/device",
            "slot=camera&type_id=onvif-network-camera"
            "&p_address=rtsp://admin:from-the-url@192.168.1.9/ch1"
            f"&p_password=typed-pw&csrf={csrf}",
            {"Cookie": token},
        )
        fitted = self.agent.inventory.fitted["camera"]
        self.assertEqual(fitted.params["password"], "typed-pw")
        self.assertEqual(fitted.params["username"], "admin")
        self.assertEqual(fitted.params["address"], "rtsp://192.168.1.9/ch1")

    def test_a_freshly_pasted_url_password_replaces_a_stored_one(self):
        token, csrf, _ = self.page()
        self.request(
            "POST", "/device",
            "slot=camera&type_id=onvif-network-camera&p_address=192.168.1.9"
            f"&p_password=old-stored-pw&csrf={csrf}",
            {"Cookie": token},
        )
        token, csrf, _ = self.page()
        self.request(
            "POST", "/device",
            "slot=camera&type_id=onvif-network-camera"
            f"&p_address=rtsp://admin:new-url-pw@192.168.1.9/ch1&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(
            self.agent.inventory.fitted["camera"].params["password"], "new-url-pw",
        )

    def test_the_enrolment_code_is_not_echoed_back_into_the_page(self):
        token, csrf, _ = self.page()
        self.agent.enrol = mock.Mock(side_effect=RuntimeError("the platform refused"))
        self.request("POST", "/enrol", f"token=ABCD-EFGH-IJKL&csrf={csrf}",
                     {"Cookie": token})
        _, _, body = self.page()
        self.assertNotIn("ABCD-EFGH-IJKL", body)
        self.assertIn("the platform refused", body)

    # --- where the station is -------------------------------------------

    def location(self, body: str, csrf: str, cookie: str):
        """POST the location form and come back with the page it lands on."""
        self.request("POST", "/location", f"{body}&csrf={csrf}", {"Cookie": cookie})
        return self.request("GET", "/connection", None, {"Cookie": cookie})[1]

    def test_the_connection_page_states_the_position_and_does_not_ask_for_it(self):
        # Position is settled at enrolment and frozen. The page reports what
        # this box was issued; it offers no way to type a different one.
        _, _, body = self.page("/connection")
        self.assertIn("Position", body)
        self.assertNotIn("name='latitude'", body)
        self.assertNotIn("name='longitude'", body)

    def test_the_elevation_survives_a_reload_and_reaches_the_correction(self):
        # Elevation stays editable and is not an inconsistency: it is measured
        # at the mast rather than issued, and exists for the barometric
        # correction computed on this box.
        token, csrf, _ = self.page("/connection")
        self.request("POST", "/location", f"elevation_m=120&csrf={csrf}",
                     {"Cookie": token})
        self.assertEqual(self.agent.site.elevation_m, 120.0)
        from gsu.config import SiteConfig
        reloaded = SiteConfig.load(self.agent.config.site_config_path)
        self.assertEqual(reloaded.elevation_m, 120.0)

    def test_garbage_in_an_elevation_is_a_sentence_and_not_a_traceback(self):
        token, csrf, _ = self.page("/connection")
        self.request("POST", "/location", f"elevation_m=up+a+bit&csrf={csrf}",
                     {"Cookie": token})
        _, body = self.request("GET", "/connection", None, {"Cookie": token})
        self.assertNotIn("Traceback", body)
        self.assertIn("msg bad", body)

    def test_an_elevation_outside_its_range_is_refused(self):
        token, csrf, _ = self.page("/connection")
        self.request("POST", "/location", f"elevation_m=999999&csrf={csrf}",
                     {"Cookie": token})
        self.assertIsNone(self.agent.site.elevation_m)

    def test_saving_an_elevation_does_not_clear_an_older_boxs_position(self):
        # A station enrolled before position moved to enrolment has its own
        # coordinates and nothing else. An elevation form must not take its
        # range and bearing away as a side effect.
        self.agent.set_location(-42.4004, 173.68, None)
        token, csrf, _ = self.page("/connection")
        self.request("POST", "/location", f"elevation_m=80&csrf={csrf}",
                     {"Cookie": token})
        self.assertEqual(
            (self.agent.site.latitude, self.agent.site.longitude),
            (-42.4004, 173.68),
        )

    def test_the_location_post_lands_back_on_connection(self):
        token, csrf, _ = self.page("/connection")
        response, _ = self.request(
            "POST", "/location", f"latitude=&longitude=&elevation_m=&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 303)
        # From the fixed map, never from the request.
        self.assertEqual(response.getheader("Location"), "/connection")

    def test_a_location_post_without_a_csrf_token_changes_nothing(self):
        token, _, _ = self.page("/connection")
        self.request("POST", "/location",
                     "latitude=-42.4&longitude=173.7&elevation_m=&csrf=stale",
                     {"Cookie": token})
        self.assertIsNone(self.agent.site.latitude)

    # --- the location editor, which must work with script blocked --------
    #
    # The section shows the position read-only and keeps the three inputs in a
    # dialog behind an Edit link. The dialog is CSS only — `:target`, so the
    # open state is the URL — because an installer's phone with scripts
    # disabled has to be able to set a position, and because the server can
    # reopen a URL-driven dialog after a refused save and can do nothing at all
    # to a hidden checkbox. Everything below is that path with no script in it.

    def test_the_position_is_stated_and_not_editable(self):
        # Settled at enrolment and frozen. The card reports it; nothing on the
        # page offers a way to type a different one.
        _, _, body = self.page("/connection")
        card = body.split("<h2>Where this box is</h2>", 1)[1]
        self.assertIn("Position", card)
        self.assertNotIn("name='latitude'", body)
        self.assertNotIn("name='longitude'", body)

    def test_the_local_settings_are_inline_and_need_no_dialog(self):
        # There was a :target dialog here, and it was right while this card
        # held three coordinates that duplicated the rows above it. With the
        # position frozen it would hold a number and a checkbox, which is less
        # than the machinery of an overlay costs.
        _, _, body = self.page("/connection")
        self.assertNotIn("class=modal", body)
        card = body.split("<h2>Where this box is</h2>", 1)[1]
        self.assertIn("name='elevation_m'", card)
        self.assertIn("name='adsb_baro_correction'", card)

    def test_the_connection_page_carries_no_script_at_all(self):
        # The only script this page ever had closed the dialog on Escape. No
        # dialog, no script, and so no script-src in its policy — the strongest
        # form of "this page works with scripts blocked".
        response, body = self.request("GET", "/connection")
        self.assertNotIn("<script", body)
        self.assertNotIn("script-src", response.getheader("Content-Security-Policy"))

    def test_saving_lands_back_on_connection_and_says_so(self):
        token, csrf, _ = self.page("/connection")
        response, _ = self.request(
            "POST", "/location", f"elevation_m=120&csrf={csrf}", {"Cookie": token},
        )
        self.assertEqual(response.status, 303)
        # From the fixed map, never from the request.
        self.assertEqual(response.getheader("Location"), "/connection")
        _, body = self.request("GET", "/connection", None, {"Cookie": token})
        self.assertIn("Saved.", body)
        # The field holds it; there is no separate read-only row saying the
        # same number back, which is the duplication this card started with.
        card = body.split("<h2>Where this box is</h2>", 1)[1]
        self.assertIn("name='elevation_m'", card)
        self.assertIn("value='120'", card)

    def test_a_refused_save_says_why_on_the_page(self):
        # No dialog to reopen, so the reason goes where every other refusal on
        # this page goes.
        token, csrf, _ = self.page("/connection")
        self.request("POST", "/location", f"elevation_m=999999&csrf={csrf}",
                     {"Cookie": token})
        _, body = self.request("GET", "/connection", None, {"Cookie": token})
        self.assertEqual(body.count("msg bad"), 1, "said twice is said wrong")
        self.assertIn("Elevation must be between", body)

    def test_the_local_settings_use_the_shared_field_grid(self):
        _, _, body = self.page("/connection")
        card = body.split("<h2>Where this box is</h2>", 1)[1]
        # Elevation, the correction checkbox, and the save row.
        self.assertEqual(card.count("<div class=field>"), 3)
        self.assertNotIn("grid-template-columns", card)
        self.assertIn("--label-w:9.5rem", body)


    # --- the two strips, and the one alignment --------------------------

    def test_both_strips_are_pinned_and_the_slot_strip_sits_below_the_other(self):
        """CSS, asserted because it is a correctness property and not a taste.

        The failure it prevents is the slot strip pinning *over* the page
        strip, which on a phone hides the only way off the Devices page.
        """
        from gsu.console import STYLE

        # The top strip is the whole header bar — mark, title and tabs — since
        # the title moved into it. The tabs themselves are static inside it;
        # what has to be pinned, and what --nav-h has to describe, is the bar.
        page_strip = STYLE.split(".topbar {")[1].split("}")[0]
        slot_strip = STYLE.split(".subtabs {")[1].split("}")[0]
        for name, block in (("topbar", page_strip), ("subtabs", slot_strip)):
            self.assertIn("position: sticky", block, name)
        self.assertIn("top: 0", page_strip)
        # The offset is the page strip's own height, and that strip is given
        # that height rather than being allowed to size to its content.
        self.assertIn("top: var(--nav-h)", slot_strip)
        self.assertIn("height: var(--nav-h)", page_strip)
        # Whichever way a browser rounds, the page strip wins the overlap.
        self.assertGreater(
            int(page_strip.split("z-index:")[1].split(";")[0]),
            int(slot_strip.split("z-index:")[1].split(";")[0]),
        )

    def test_the_page_strip_is_marked_so_only_it_is_pinned_to_the_top(self):
        _, _, body = self.page("/devices")
        self.assertIn("<header class=topbar>", body)
        self.assertIn("<nav class='tabs pagetabs'>", body)
        self.assertIn("<nav class='tabs subtabs'>", body)

    def test_the_header_bar_carries_the_mark_and_the_title(self):
        body = self.every_page()["/"]
        header = body.split("<header class=topbar>")[1].split("</header>")[0]
        self.assertIn("topbar-mark", header)
        self.assertIn("Ground station", header)
        # And the title is no longer a heading that scrolls away underneath it.
        self.assertNotIn("<h1>Ground station</h1>", body)

    def test_every_page_carries_a_favicon(self):
        # Self-contained like the logo: a data: URI, so a station on an
        # isolated network still has an icon in the tab.
        for path, body in self.every_page().items():
            self.assertIn("rel=icon", body, path)
            self.assertIn("data:image", body.split("rel=icon")[1][:40], path)

    def test_slot_tabs_are_named_for_people_not_for_the_wire(self):
        _, _, body = self.page("/devices")
        strip = body.split("<nav class='tabs subtabs'>")[1].split("</nav>")[0]
        self.assertIn(">ADS-B<", strip)
        self.assertIn(">Weather<", strip)
        # The raw slot key must not reach a tab.
        self.assertNotIn(">adsb<", strip)
        self.assertNotIn(">weather<", strip)

    def test_every_form_row_on_every_page_is_the_same_two_columns(self):
        """The owner's complaint: controls started wherever their label ended.

        Asserted structurally — every label/control pair on every page is a
        `.field`, so one grid rule aligns all of them. A form that renders its
        own row shape is the regression this catches.
        """
        from gsu.console import STYLE

        field = STYLE.split("\n .field {")[1].split("}")[0]
        self.assertIn("display: grid", field)
        self.assertIn("grid-template-columns: var(--label-w)", field)
        # And it collapses rather than overflowing on a phone.
        self.assertIn("@media (max-width: 34rem)", STYLE)

        for path in ("/connection", "/devices"):
            _, _, body = self.page(path)
            # No labelled control outside a .field: the enrolment box used to
            # be a label, a <br> and an input, aligned with nothing.
            self.assertNotIn("</label><br>", body, path)
            self.assertIn("<div class=field>", body, path)

    def test_the_save_buttons_sit_in_the_control_column(self):
        # Left flush under indented controls is exactly the raggedness the
        # grid was added to remove.
        _, _, body = self.page("/devices")
        self.assertIn("<div class=field><button type=submit>Save</button></div>", body)


class StationPositionTests(unittest.TestCase):
    """Who owns the station's position, and what leaves the box.

    The owner's decision is that a position is set on the station and nowhere
    else — the platform must stop offering the field (CONTRACT-QUESTIONS.md
    16). These are the properties that decision turns into: the station's own
    value always wins, the platform's is a labelled fallback for boxes enrolled
    before this page had the fields, and nothing plausible is ever invented.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.agent = Agent(AgentConfig(
            home=Path(self.directory.name), setup_enabled=False,
            single_instance=False,
        ))
        self.addCleanup(self.agent.shutdown)

    def enrol_with(self, latitude, longitude):
        """An enrolment carrying the platform's idea of where this box is."""
        from datetime import UTC, timedelta

        from gsu.credentials import Enrolment

        now = datetime.now(UTC)
        self.agent.enrolment = Enrolment.from_response({
            "station_id": "11111111-2222-3333-4444-555555555555",
            "credential": {
                "type": "bearer", "secret": "s",
                "expires_at": (now + timedelta(hours=48)).isoformat(),
                "renew_after": (now + timedelta(hours=24)).isoformat(),
            },
            "broker": {
                "url": "redis://broker:6379/0", "username": "gsu:x",
                "telemetry_topic": "gsu/x/telemetry", "audio_topic": "gsu/x/audio",
                "command_topic": "cmd/gsu/x",
            },
            "station": {
                "name": "Test", "timezone": "Pacific/Auckland",
                "latitude": latitude, "longitude": longitude,
            },
            "config_version": 3,
        })

    def test_with_nothing_set_the_station_claims_no_position_at_all(self):
        self.assertEqual(self.agent.effective_position(), (None, None, ""))
        self.assertIsNone(self.agent.reported_position())
        self.assertNotIn("position", self.agent.health_payload())

    def test_the_stations_own_position_beats_the_platforms(self):
        self.enrol_with(-43.5, 172.6)
        self.agent.set_location(-42.4004, 173.68, 120.0)
        self.assertEqual(
            self.agent.effective_position(), (-42.4004, 173.68, "station")
        )
        # And it is the station's that goes out, not the platform's echo.
        self.assertEqual(self.agent.reported_position()["latitude"], -42.4004)

    def test_the_enrolment_settles_the_position(self):
        # Position is decided when the code is redeemed and frozen afterwards:
        # a station that needs a different one has physically moved, and a box
        # that has moved is recommissioned rather than edited.
        self.enrol_with(-43.5, 172.6)
        self.assertEqual(self.agent.effective_position(), (-43.5, 172.6, "enrolment"))
        # And it is not echoed back up as though the box had confirmed it —
        # the platform already knows what it issued.
        self.assertIsNone(self.agent.reported_position())

    def test_a_platform_with_no_position_leaves_the_station_with_none(self):
        self.enrol_with(None, None)
        self.assertEqual(self.agent.effective_position(), (None, None, ""))

    def test_what_is_reported_is_omitted_rather_than_defaulted(self):
        # Never 0, 0 — an unvisited station must look unvisited on a fleet map.
        payload = self.agent.health_payload()
        self.assertNotIn("position", payload)
        self.agent.set_location(-42.4004, 173.68, None)
        position = self.agent.health_payload()["position"]
        self.assertEqual(position["latitude"], -42.4004)
        self.assertEqual(position["source"], "configured")
        # Elevation is omitted when unset rather than sent as zero: this
        # station's own rule about unsourced values (DECISIONS.md item 16).
        self.assertNotIn("elevation_m", position)

    def test_the_page_marks_the_platforms_position_as_not_its_own(self):
        """Whichever answer is in use, the page says which one it is.

        Both pages that show it: the Summary row an installer scans before
        leaving, and the Connection section beside the enrolment.
        """
        console = Console(self.agent, "127.0.0.1", 0)
        self.enrol_with(-43.5, 172.6)
        for page in ("/", "/connection"):
            body = console.render(None, page)
            self.assertIn("-43.5, 172.6", body, page)
            # Settled at enrolment is the normal case and reads as an answer,
            # not as a caveat.
            self.assertNotIn("from the platform", body, page)
        # A position configured locally, which only boxes enrolled before this
        # changed will have, is stated plainly too. Which mechanism supplied it
        # is not something a person at the site can act on.
        self.agent.set_location(-42.4004, 173.68, None)
        for page in ("/", "/connection"):
            body = console.render(None, page)
            self.assertIn("-42.4004, 173.68", body, page)
            self.assertNotIn("set on this box", body, page)

    def test_an_unset_position_is_a_warning_on_the_summary(self):
        # It is a fault an installer can still fix while on site, and every
        # range and bearing this station reports depends on it.
        console = Console(self.agent, "127.0.0.1", 0)
        body = console.render(None, "/")
        self.assertIn("Location", body)
        self.assertIn("<span class='warn'>not set</span>", body)

    def test_the_position_reaches_the_drivers_that_compute_from_it(self):
        # An installer who corrects a position expects range and bearing to
        # change without restarting anything.
        seen = []

        class Driver:
            def set_site(self, latitude, longitude):
                seen.append((latitude, longitude))

        self.agent.adsb = Driver()
        self.agent.weather = Driver()
        self.agent.set_location(-42.4004, 173.68, None)
        self.assertEqual(seen, [(-42.4004, 173.68), (-42.4004, 173.68)])

    def test_a_stored_position_survives_a_restart(self):
        """The bug this is really for: SiteConfig.apply coerces with the type
        of the *current* value, and for a nullable field that type is NoneType
        while it is unset. Without the NULLABLE path, load() would drop the
        position it had just been asked to restore."""
        from gsu.config import SiteConfig

        self.agent.set_location(-42.4004, 173.68, 120.0)
        path = Path(self.directory.name) / "site-config.json"
        restored = SiteConfig.load(path)
        self.assertEqual(restored.latitude, -42.4004)
        self.assertEqual(restored.longitude, 173.68)
        self.assertEqual(restored.elevation_m, 120.0)

    def test_config_set_can_carry_a_position_and_refuses_a_bad_one(self):
        from gsu.config import SiteConfig

        config = SiteConfig()
        self.assertEqual(
            sorted(config.apply({"latitude": -42.4, "longitude": 173.7})),
            ["latitude", "longitude"],
        )
        # Unusable values are dropped like any other, never stored and never
        # a traceback on the command channel.
        self.assertEqual(config.apply({"latitude": 900}), [])
        self.assertEqual(config.latitude, -42.4)
        # An explicit null clears, which is how a position is retracted.
        self.assertEqual(config.apply({"latitude": None}), ["latitude"])
        self.assertIsNone(config.latitude)


class CoordinateParsingTests(unittest.TestCase):
    """The bounds, and the refusals a person has to be able to act on."""

    def test_the_ranges_are_the_ones_the_page_states(self):
        from gsu.config import parse_latitude, parse_longitude

        for value in (-90, 0, 90, -42.4004):
            self.assertEqual(parse_latitude(value), float(value))
        for value in (-180, 0, 180, 173.68):
            self.assertEqual(parse_longitude(value), float(value))
        for bad in (90.1, -90.1, 1000):
            self.assertRaises(ValueError, parse_latitude, bad)
        for bad in (180.1, -180.1):
            self.assertRaises(ValueError, parse_longitude, bad)

    def test_nan_and_infinity_are_refused_rather_than_stored(self):
        # Both survive float() and then silently poison every range and
        # bearing computed from them.
        from gsu.config import parse_latitude

        for bad in ("nan", "inf", "-inf", float("nan"), float("inf")):
            self.assertRaises(ValueError, parse_latitude, bad)

    def test_the_refusal_names_the_field_and_its_range(self):
        from gsu.config import parse_elevation_m, parse_longitude

        with self.assertRaises(ValueError) as caught:
            parse_longitude("east")
        self.assertEqual(
            str(caught.exception), "Longitude must be a number between -180 and 180."
        )
        with self.assertRaises(ValueError) as caught:
            parse_elevation_m("120 m")
        self.assertIn("Elevation must be a number", str(caught.exception))


class LanPeerTests(unittest.TestCase):
    """The same server, judged as though the request came off the LAN.

    The peer address is patched rather than faked at the socket, because there
    is no portable way to originate a 192.168 connection in a unit test — and
    the thing under test is the decision, not the routing.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.agent = Agent(AgentConfig(
            home=Path(self.directory.name), setup_enabled=False,
            single_instance=False,
        ))
        self.addCleanup(self.agent.shutdown)
        patcher = mock.patch(
            "gsu.setup_access.classify",
            side_effect=lambda address: "local",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.console = Console(
            self.agent, "127.0.0.1", 0,
            password=hash_password("a-good-password", iterations=1000),
        )
        self.console.start()
        self.addCleanup(self.console.stop)
        self.port = self.console._server.server_address[1]

    def request(self, method: str, path: str, body: str | None = None,
                headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        payload = response.read().decode()
        response.close()
        connection.close()
        return response, payload

    def test_the_login_form_is_all_an_unauthenticated_laptop_gets(self):
        # Every page identically: the router grew paths and none of them may
        # answer with content before the password has been shown.
        from gsu.console import PAGES

        for path in PAGES:
            response, body = self.request("GET", path)
            self.assertEqual(response.status, 401, path)
            self.assertIn("Setup password", body, path)
            # And it says nothing about the station: not the site, not what
            # is fitted, not whether it is enrolled.
            for leak in ("weather", "adsb", "Enrolled", "Platform API",
                         "broker", "Recent events"):
                self.assertNotIn(leak, body, f"{path} leaks {leak}")

    def test_status_json_is_not_readable_without_the_password(self):
        response, body = self.request("GET", "/status.json")
        self.assertEqual(response.status, 401)
        self.assertNotIn("station_id", body)

    def test_the_frame_is_not_viewable_without_the_password(self):
        # The preview endpoint is a page like any other: a camera pointed at
        # a site is exactly the thing an unauthenticated LAN peer must not see.
        self.agent.video.cycle()
        response, body = self.request("GET", "/frame.jpg")
        self.assertEqual(response.status, 401)
        self.assertNotIn("JFIF", body)
        self.assertIn("Setup password", body)

    def test_a_post_is_refused_without_the_password(self):
        before = self.agent.inventory.fitted["weather"].type_id
        response, _ = self.request(
            "POST", "/device", "slot=weather&type_id=airmar-110wx",
        )
        self.assertIn(response.status, (401, 403))
        self.assertEqual(self.agent.inventory.fitted["weather"].type_id, before)

    def test_the_right_password_opens_the_page(self):
        response, _ = self.request(
            "POST", "/login", "password=a-good-password",
        )
        self.assertEqual(response.status, 303)
        cookie = (response.getheader("Set-Cookie") or "").split(";")[0]
        self.assertTrue(cookie.startswith(COOKIE_NAME))
        response, body = self.request("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(response.status, 200)
        self.assertIn("Slots", body)
        # And the same cookie opens every other page, not just the landing one.
        response, body = self.request("GET", "/devices", headers={"Cookie": cookie})
        self.assertEqual(response.status, 200)
        self.assertIn("What is fitted", body)

    def test_the_wrong_password_does_not(self):
        response, body = self.request("POST", "/login", "password=nope")
        self.assertEqual(response.status, 401)
        self.assertIn("wrong password", body)
        self.assertIsNone(response.getheader("Set-Cookie"))

    def test_a_first_view_shows_no_error_because_there_is_none(self):
        # "password required" in a red box before anyone has typed anything
        # reads as a fault. Reasons belong to attempts.
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 401)
        self.assertNotIn("msg bad", body)
        self.assertNotIn("password required", body)
        # An attempt earns one.
        _, body = self.request("POST", "/login", "password=nope")
        self.assertIn("msg bad", body)

    def test_the_helper_line_says_where_the_password_lives(self):
        _, body = self.request("GET", "/")
        self.assertIn(
            "The login password can be found on this box's label, or with "
            "whoever provisioned it.", body,
        )

    def test_the_login_page_wears_the_mark_and_stays_self_contained(self):
        from gsu.brand import LOGO_DATA_URI

        response, body = self.request("GET", "/")
        self.assertIn("data:image/png;base64,", body)
        self.assertIn(LOGO_DATA_URI, body)
        self.assertIn("PERCEPTA", body)
        # Self-contained means self-contained: nothing fetched from anywhere.
        self.assertNotIn("http://", body.replace("http://127.0.0.1", ""))
        self.assertNotIn("https://", body)
        csp = response.getheader("Content-Security-Policy") or ""
        self.assertIn("img-src 'self' data:", csp)


if __name__ == "__main__":
    unittest.main()

class HashSeparatorTests(unittest.TestCase):
    """The hash must survive docker compose's env_file interpolation.

    compose expands `$VAR` inside env_file values, so a `$`-separated hash
    whose hex salt begins with a letter lost that whole segment on the way
    into the container - the login then refused every password while the file
    on disk was perfectly correct. Systemd stations never saw it.
    """

    def test_hash_contains_no_dollar(self):
        from gsu.setup_access import hash_password
        for _ in range(20):
            self.assertNotIn("$", hash_password("correct horse battery"))

    def test_new_and_legacy_hashes_both_verify(self):
        import hashlib, secrets
        from gsu.setup_access import ITERATIONS, hash_password, verify_password
        password = "correct horse battery"
        self.assertTrue(verify_password(hash_password(password), password))
        self.assertFalse(verify_password(hash_password(password), "wrong"))

        # A legacy `$` hash with a letter-leading salt: the exact shape that
        # broke, which must still verify for boxes already carrying one.
        salt = bytes.fromhex("b91b7ad79c5a6dfd779142e298553b35")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
        legacy = f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"
        self.assertTrue(verify_password(legacy, password))
        self.assertFalse(verify_password(legacy, "wrong"))

    def test_a_salt_eaten_by_interpolation_does_not_verify(self):
        """The corrupted value must fail closed, not match something."""
        from gsu.setup_access import ITERATIONS, verify_password
        mangled = f"pbkdf2_sha256${ITERATIONS}$$deadbeef"
        self.assertFalse(verify_password(mangled, "correct horse battery"))
        self.assertFalse(verify_password(mangled, ""))



class HonestVerdictTests(unittest.TestCase):
    """Facts the station does not know must not be shown as facts it does.

    Every case here was found on a live station reporting a fault it had no
    basis for. The shared shape is a three-valued world — yes, no, could not
    find out — rendered through a two-valued `if`, so "could not find out" came
    out looking exactly like "no". On the page an installer reads to decide
    whether to drive to a site, that difference is the point of the page.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parents[1]
                      / "gsu" / "console.py").read_text()

    def test_an_undetermined_clock_is_not_reported_as_unsynchronised(self):
        # A container cannot see the host's timesyncd: `/run` inside it is its
        # own tmpfs and neither chronyc nor timedatectl is in the image, so
        # every probe returns nothing. The Pi 5's clock was correctly
        # disciplined the whole time the page said otherwise.
        state = {"synchronised": None, "source": "rtc-only", "rtc_present": True}
        wording = Console._clock_wording(state)
        self.assertIn("cannot tell", wording)
        self.assertNotIn("not synced", wording)
        self.assertEqual(Console._clock_class(state), "unknown")

    def test_a_clock_nobody_is_keeping_is_still_a_warning(self):
        # The fix must not go the other way and swallow the real fault.
        state = {"synchronised": False, "source": "none", "rtc_present": False}
        self.assertEqual(Console._clock_class(state), "warn")
        self.assertIn("guess", Console._clock_wording(state))

    def test_a_disciplined_clock_reads_as_good(self):
        state = {"synchronised": True, "source": "ntp", "rtc_present": True}
        self.assertEqual(Console._clock_class(state), "ok")
        self.assertEqual(Console._clock_wording(state), "NTP")

    def test_unknown_is_styled_as_a_note_not_a_fault(self):
        # Muted, not amber. Amber on a station that is probably fine is how a
        # page trains people to stop reading amber at all.
        self.assertIn(".unknown { color: var(--muted); }", self.source)

    def test_a_supported_camera_backend_is_not_a_fault(self):
        # `rpicam` is the CSI camera and `ffmpeg` is a network camera. Testing
        # for `rpicam` alone put a permanent amber on every correctly working
        # RTSP camera — a configuration this station has because an owner asked
        # for it. Only "no capture tool at all" is a fault.
        self.assertIn('"warn" if camera["backend"] == "none" else "ok"',
                      self.source)
        self.assertNotIn('"ok" if camera.get("backend") == "rpicam" else "warn"',
                         self.source)
        self.assertIn(
            '"warn" if camera.get("backend") in (None, "none") else "muted"',
            self.source,
        )

    def test_a_held_camera_is_normal_operation(self):
        # A held sensor is what a working live stream looks like; the lease in
        # camera/ownership.py exists precisely so that one reader holds it.
        self.assertNotIn('("Camera held by", holds, "warn" if holder else "ok")',
                         self.source)


class BarometricCorrectionSettingTests(unittest.TestCase):
    """The altitude correction, switched on where its input is typed.

    It re-references reported pressure altitudes to this station's own
    barometer and is computed from the site elevation, so the setup page edits
    the two together rather than putting the switch a screen away from the
    number it depends on.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.agent = Agent(AgentConfig(
            home=Path(self.directory.name), setup_enabled=False,
            single_instance=False,
        ))
        self.addCleanup(self.agent.shutdown)
        self.console = Console(self.agent)

    def submit(self, **fields):
        return self.console._set_location(
            {name: [value] for name, value in fields.items()}
        )

    def test_it_is_off_until_somebody_turns_it_on(self):
        # It applies one sensor's reading to another sensor's data. That is a
        # decision an operator makes, never a default.
        self.assertFalse(self.agent.site.adsb_baro_correction)

    def test_ticking_it_with_an_elevation_switches_it_on(self):
        self.submit(latitude="-42.4004", longitude="173.68",
                    elevation_m="120", adsb_baro_correction="1")
        self.assertTrue(self.agent.site.adsb_baro_correction)
        self.assertEqual(self.agent.site.elevation_m, 120.0)

    def test_ticking_it_without_an_elevation_is_refused_not_accepted_idle(self):
        # A checkbox that stays ticked while nothing happens is how somebody
        # comes to trust a number that was never computed.
        with self.assertRaises(ValueError) as caught:
            self.submit(latitude="-42.4004", longitude="173.68",
                        elevation_m="", adsb_baro_correction="1")
        self.assertIn("Elevation", str(caught.exception))
        self.assertFalse(self.agent.site.adsb_baro_correction)

    def test_an_unticked_box_turns_it_off(self):
        # An unchecked checkbox sends nothing, and on this form that absence is
        # a real "off" because the input is always rendered inside it.
        self.submit(latitude="-42.4004", longitude="173.68",
                    elevation_m="120", adsb_baro_correction="1")
        self.submit(latitude="-42.4004", longitude="173.68", elevation_m="120")
        self.assertFalse(self.agent.site.adsb_baro_correction)

    def test_clearing_the_location_takes_the_correction_with_it(self):
        # Leaving it on would leave a station switched on to correct altitudes
        # it can no longer correct.
        self.submit(latitude="-42.4004", longitude="173.68",
                    elevation_m="120", adsb_baro_correction="1")
        self.submit(latitude="", longitude="", elevation_m="")
        self.assertFalse(self.agent.site.adsb_baro_correction)
        self.assertIsNone(self.agent.site.elevation_m)

    def test_a_caller_that_does_not_mention_it_does_not_change_it(self):
        # The switch also arrives by config.set from the platform. Saving a
        # coordinate must not silently undo that.
        self.agent.site.adsb_baro_correction = True
        self.agent.set_location(-42.4004, 173.68, 120.0)
        self.assertTrue(self.agent.site.adsb_baro_correction)

    def test_it_survives_a_reload_of_the_site_file(self):
        self.submit(latitude="-42.4004", longitude="173.68",
                    elevation_m="120", adsb_baro_correction="1")
        from gsu.config import SiteConfig
        reloaded = SiteConfig.load(self.agent.config.site_config_path)
        self.assertTrue(reloaded.adsb_baro_correction)

    def test_the_checkbox_is_rendered_inside_the_location_form(self):
        # The "absence means off" reading in _set_location is only sound while
        # the input is inside this form and always present.
        state = {"position": self.agent.position_state()}
        markup = self.console._section_location(state, "tok")
        form = markup[markup.index("<form"):markup.index("</form>")]
        self.assertIn("name='adsb_baro_correction'", form)
        self.assertIn("type=checkbox", form)

class DeviceStateVocabularyTests(unittest.TestCase):
    """Three states on the setup page, an owner requirement.

    Not fitted, Connected, Disconnected. They answer the three questions an
    installer actually has, and the four-state internal vocabulary answers a
    different one.
    """

    def test_it_offers_exactly_three_words(self):
        from gsu.console import STATUS_PILL
        self.assertEqual(
            {wording for _, wording in STATUS_PILL.values()},
            {"Not fitted", "Connected", "Disconnected"},
        )

    def test_gone_quiet_and_never_answered_both_read_as_disconnected(self):
        # Genuinely different to a driver and identical to a person standing at
        # the site: both mean "selected, and you are not getting data", and
        # both send them to the same cable. The `found:` line keeps the
        # distinction for whoever needs it.
        from gsu.console import STATUS_PILL
        self.assertEqual(STATUS_PILL["stalled"][1], "Disconnected")
        self.assertEqual(STATUS_PILL["configured_absent"][1], "Disconnected")

    def test_an_empty_slot_is_not_a_fault(self):
        # A station with no floodlight is a complete station.
        from gsu.console import STATUS_PILL
        css, wording = STATUS_PILL["not_fitted"]
        self.assertEqual(wording, "Not fitted")
        self.assertEqual(css, "off")

    def test_disconnected_is_not_red(self):
        # It is the normal state during commissioning — you select a device
        # before you plug it in — and a red pill on every unwired slot teaches
        # people that red means nothing.
        from gsu.console import STATUS_PILL
        self.assertEqual(STATUS_PILL["configured_absent"][0], "warn")
        self.assertEqual(STATUS_PILL["present"][0], "ok")


class PinnedWindowSessionTests(unittest.TestCase):
    """A pinned-open access window must not shorten the login.

    `GSU_SETUP_WINDOW_MINUTES=0` means the page stays reachable for as long as
    the station runs. The idle limit read that as `max(0, 1.0)` — sixty
    seconds — so the boxes configured never to close were the ones that logged
    you out fastest, mid-task, repeatedly.
    """

    def gate(self, window_minutes):
        return Gate(window_minutes=window_minutes)

    def test_pinned_open_gets_a_working_session_not_a_minute(self):
        pinned = self.gate(0)._idle_limit_s()
        self.assertGreaterEqual(pinned, 20 * 60)
        self.assertGreater(pinned, self.gate(1)._idle_limit_s())

    def test_a_real_window_still_governs_its_own_session(self):
        self.assertEqual(self.gate(30)._idle_limit_s(), 30 * 60)

    def test_a_tiny_window_keeps_its_floor(self):
        # The floor exists so a two-second window does not make the page
        # unusable; it was only ever wrong when applied to zero.
        self.assertEqual(self.gate(0.25)._idle_limit_s(), 60.0)

    def test_pinned_open_is_not_unlimited(self):
        # A browser left open on a bench is still a way in. The pin is about
        # the socket staying bound, not about never logging in again.
        self.assertLess(self.gate(0)._idle_limit_s(), 24 * 60 * 60)


class SummaryPageTests(unittest.TestCase):
    """What the landing page says, and what it has stopped saying twice."""

    def test_device_conditions_are_not_restated_above_the_slots(self):
        source = (Path(__file__).resolve().parents[1]
                  / "gsu" / "console.py").read_text()
        # The Slots table on this same page names every selected device and
        # whether it is answering. These two conditions say exactly that.
        self.assertIn("DUPLICATED_BY_SLOTS", source)
        self.assertIn('"devices.absent"', source)
        self.assertIn('"telemetry.unsourced"', source)

    def test_the_conditions_with_nowhere_else_to_go_are_kept(self):
        # A credential failing to renew is invisible until it expires and then
        # costs a site visit. No slot pill will ever show it.
        source = (Path(__file__).resolve().parents[1]
                  / "gsu" / "console.py").read_text()
        summary = source[source.index("def _page_summary"):
                         source.index("def _section_enrol")]
        self.assertIn("Needs attention", summary)
        for condition in ("uplink.refused", "clock.implausible", "setup.demoted"):
            self.assertNotIn(condition, summary)

    def test_no_navigation_prose_under_the_slots(self):
        source = (Path(__file__).resolve().parents[1]
                  / "gsu" / "console.py").read_text()
        self.assertNotIn("Selection and parameters are on the", source)
