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
import socket
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from gsu.agent import Agent
from gsu.config import AgentConfig
from gsu.console import Console, _host_is_addressable
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

    def test_the_devices_page_offers_every_slot_from_the_registry(self):
        from gsu.devices import registry

        _, _, body = self.page("/devices")
        for slot in registry.SLOTS:
            self.assertIn(f"<strong>{slot}</strong>", body)
        # Driven from the registry, not from a second list in the template.
        for device in registry.REGISTRY:
            self.assertIn(f"value='{device.id}'", body)

    def test_the_summary_page_has_a_line_per_slot(self):
        from gsu.devices import registry

        _, body = self.request("GET", "/")
        for slot in registry.SLOTS:
            self.assertIn(f"<span class=k>{slot}</span>", body)

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
        self.assertIn("GSU_PLATFORM_URL", pages["/connection"])
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
        self.assertEqual(response.getheader("Location"), "/devices")
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
        _, _, body = self.page()
        self.assertNotIn("s3cr3t-camera-pw", body)
        self.assertIn("A password is stored", body)
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

    def test_the_enrolment_code_is_not_echoed_back_into_the_page(self):
        token, csrf, _ = self.page()
        self.agent.enrol = mock.Mock(side_effect=RuntimeError("the platform refused"))
        self.request("POST", "/enrol", f"token=ABCD-EFGH-IJKL&csrf={csrf}",
                     {"Cookie": token})
        _, _, body = self.page()
        self.assertNotIn("ABCD-EFGH-IJKL", body)
        self.assertIn("the platform refused", body)


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


if __name__ == "__main__":
    unittest.main()
