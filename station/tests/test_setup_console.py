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
import os
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
            home=Path(directory.name), setup_enabled=False, single_instance=False, demo=True))
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
            home=self.home, setup_enabled=False, single_instance=False, demo=True))
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
            single_instance=False, demo=True))
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

    def test_the_radio_form_saves_the_transcription_switch(self):
        # The /radio Apply carries the transcription toggle — and its handler
        # existed nowhere until now: the form posted to a missing method.
        token, csrf, _ = self.page()
        self.request("POST", "/radio", f"radio_transcribe=1&csrf={csrf}",
                     {"Cookie": token})
        self.assertTrue(self.agent.site.radio_transcribe)
        # An unchecked box sends nothing, so its absence is a real "off".
        self.request("POST", "/radio", f"csrf={csrf}", {"Cookie": token})
        self.assertFalse(self.agent.site.radio_transcribe)

    def test_the_radio_apply_sets_gain_and_squelch_on_the_live_receiver(self):
        # The one Apply carries the operate commands too: gain in the tuner's own
        # step, and a manual squelch level that turns AUTO off in the same move —
        # the same discrete commands the platform sends.
        self.agent.inventory.set_device("radio", "simulated-airband", {}, None)
        self.agent.build_devices()
        token, csrf, _ = self.page("/devices?slot=radio")
        step = self.agent.radio.available_gains[1]
        self.request(
            "POST", "/radio",
            f"type_id=simulated-airband&gain={step}&squelch=-55&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(self.agent.radio.gain, step)
        self.assertEqual(self.agent.radio.manual_threshold_db, -55.0)
        self.assertFalse(self.agent.radio.auto_squelch)
        # AUTO ticked wins over the slider.
        self.request(
            "POST", "/radio",
            f"type_id=simulated-airband&squelch=-55&auto_squelch=1&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertTrue(self.agent.radio.auto_squelch)

    def test_the_radio_apply_is_instant_over_fetch_without_a_reload(self):
        # The instant-apply path: a control change posts with ajax=1 and gets a
        # small JSON answer, not the 303 the no-script fallback gets — so the
        # page, and the audio being listened to, is never reloaded.
        self.agent.inventory.set_device("radio", "simulated-airband", {}, None)
        self.agent.build_devices()
        token, csrf, _ = self.page("/devices?slot=radio")
        step = self.agent.radio.available_gains[1]
        response, body = self.request(
            "POST", "/radio",
            f"ajax=1&type_id=simulated-airband&gain={step}&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.getheader("Content-Type") or "")
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.agent.radio.gain, step)

    def test_managed_gain_can_be_selected_and_settles_on_a_step(self):
        # Selecting "managed" puts the receiver into the software AGC, which
        # holds a fixed step it reports back — not the literal word on the wire.
        self.agent.inventory.set_device("radio", "simulated-airband", {}, None)
        self.agent.build_devices()
        token, csrf, _ = self.page("/devices?slot=radio")
        self.request(
            "POST", "/radio",
            f"type_id=simulated-airband&gain=managed&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(self.agent.radio.gain, "managed")
        self.assertIn(self.agent.radio.managed_gain_db,
                      self.agent.radio.available_gains)

    def test_a_bad_ajax_apply_answers_with_the_reason_not_a_redirect(self):
        # A refused value comes back as ok:false with the message, for the status
        # line to show — still a 200 JSON, not a redirect the fetch cannot read.
        self.agent.inventory.set_device("radio", "simulated-airband", {}, None)
        self.agent.build_devices()
        token, csrf, _ = self.page("/devices?slot=radio")
        response, body = self.request(
            "POST", "/radio",
            f"ajax=1&type_id=simulated-airband&freq_mhz=notafreq&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("frequency", payload["message"].lower())

    def test_an_operate_only_apply_does_not_rebuild_the_receiver(self):
        # A gain or squelch tweak must not tear the tuner down: only a
        # device-level change (which receiver, bias tee) rebuilds. Same
        # controller object before and after is the proof.
        self.agent.inventory.set_device("radio", "simulated-airband", {}, None)
        self.agent.build_devices()
        token, csrf, _ = self.page("/devices?slot=radio")
        before = id(self.agent.radio)
        step = self.agent.radio.available_gains[1]
        self.request(
            "POST", "/radio",
            f"type_id=simulated-airband&gain={step}&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(id(self.agent.radio), before)

    def test_picking_a_device_marks_the_form_to_commit(self):
        # Picking only re-renders — it stores nothing, so a device's parameters
        # can be filled in before anything is written. The type lives in a hidden
        # field the fresh render leaves equal to every default, so nothing on the
        # form looks changed; without a marker the picked device would sit there
        # uncommitted, which is how the radio once got stuck on a dead receiver.
        # `data-changed` is that marker: the script reads it and commits the pick
        # over fetch, so choosing a device takes with no button to press.
        # Matched on the form tag, not anywhere in the page: the script that
        # reads the attribute also contains its name.
        _, body = self.request("GET", "/devices?slot=radio")
        self.assertIn("data-device>", body)          # nothing picked yet

        from gsu.devices import registry
        other = next(d for d in registry.by_slot("radio"))
        _, body = self.request("GET", f"/devices?slot=radio&type={other.id}")
        self.assertIn("data-device data-changed>", body)

    def test_un_fitting_a_slot_is_selectable_at_all(self):
        # "— not fitted —" posts `type=` with nothing after it. parse_qs drops
        # a blank value unless told otherwise, so the picker used to fall back
        # to the stored device and the option did nothing — silently, which is
        # the worst way for the one control you reach for when a device has
        # died to not work.
        from gsu.devices import registry
        fitted = next(d for d in registry.by_slot("radio"))
        _, body = self.request("GET", f"/devices?slot=radio&type={fitted.id}")
        self.assertIn(f"value='{fitted.id}' selected", body)
        _, body = self.request("GET", "/devices?slot=radio&type=")
        self.assertIn("<option value='' selected>", body)

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

    def test_the_device_form_has_no_save_button(self):
        # There is no Save button on the Devices tab at all — not hidden, not
        # disabled, gone. The script applies each field on change and commits a
        # picked device on load, so nothing is left to press. Unlike the rest of
        # the page this one control needs the script; a bare Save button coming
        # back is the regression, so the absence is asserted rather than assumed.
        _, _, body = self.page("/devices")
        self.assertNotIn(">Save</button>", body)

    def test_the_device_form_is_marked_for_instant_apply(self):
        # The nonce'd script posts each change over fetch (ajax=1), writing the
        # outcome to this status line; a freshly picked device commits itself the
        # same way. The status span is where the script writes, and its presence
        # is also how the script tells a real device form from the radio slot's
        # field-less placeholder — so its absence would strand the whole path.
        _, _, body = self.page("/devices")
        self.assertIn("action='/device' data-device", body)
        self.assertIn("class='muted device-status'", body)

    def test_a_device_change_applies_over_fetch_without_a_reload(self):
        # Choosing or editing a device posts with ajax=1 and gets a small JSON
        # answer — not the 303 the no-script fallback gets — so the page, and
        # any camera preview on it, is never reloaded. This is what makes a
        # device selection take on change with no Save button to press.
        token, csrf, _ = self.page("/devices?slot=weather")
        response, body = self.request(
            "POST", "/device",
            f"ajax=1&slot=weather&type_id=simulated-weather&csrf={csrf}",
            {"Cookie": token},
        )
        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.getheader("Content-Type") or "")
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            self.agent.inventory.fitted["weather"].type_id, "simulated-weather")

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

    def test_the_camera_tab_offers_the_live_stream(self):
        # This page is where somebody aims a camera, so the preview is the live
        # encoder rather than a still: a frame up to two seconds old, fetched
        # every two and a half, is three to five seconds behind the thing being
        # pointed. The element is there before any capture has happened,
        # because it does not depend on one — it opens the stream itself.
        _, body = self.request("GET", "/devices?slot=camera")
        self.assertIn("<video id=preview", body)
        self.assertIn("autoplay muted playsinline", body)
        self.assertIn("zoom-toggle", body)
        # No src in the markup: the script attaches it, so a browser that
        # cannot finish a progressive load does not start one and strand an
        # encoder on this box. See `_preview`.
        self.assertNotIn("<video id=preview src=", body)
        self.assertIn("startLive", body)

    def test_the_live_element_is_never_torn_down_to_show_something_else(self):
        """It is what the stream attaches to.

        Rebuilding the box to swap a still in removed it 2.5 s after every page
        load — before it had decoded its first frame, which is exactly when it
        looks most like it is not working — and took the stream that was about
        to start with it. What was left was a still refreshing every 2.5 s,
        which reads as a picture that freezes every few seconds, on an idle CPU
        because the encoder had been stopped.

        So all three elements are always present and a class picks one.
        """
        from gsu.console import Console

        for video in (
            {"stream": {"state": "streaming"}, "has_frame": True},
            {"stream": {"state": "starting"}},
            {"stream": {"state": "idle"}, "has_frame": True},
            {"stream": {"state": "idle"}, "reason": "no camera fitted"},
        ):
            markup = Console._preview(video)
            self.assertIn("<video id=preview", markup, video)
            self.assertIn("id=preview-still", markup, video)
            self.assertIn("id=preview-empty", markup, video)

    def test_the_station_decides_which_of_the_three_is_shown(self):
        # Not the element's own readyState or error, both of which were guesses
        # about something `status.json` already reports.
        from gsu.console import Console

        def mode(video):
            import re
            return re.search(r'class="preview ([a-z]+)"',
                             Console._preview(video)).group(1)

        self.assertEqual(mode({"stream": {"state": "streaming"}, "has_frame": True}),
                         "live")
        # Starting counts as live: it is the window in which tearing the
        # element down was fatal.
        self.assertEqual(mode({"stream": {"state": "starting"}}), "live")
        self.assertEqual(mode({"stream": {"state": "idle"}, "has_frame": True}),
                         "still")
        self.assertEqual(mode({"stream": {"state": "unavailable"}}), "empty")

    def test_nothing_to_show_says_so_instead_of_showing_a_black_box(self):
        """A `<video>` with nothing attached renders as a black rectangle.

        Which is the least useful thing this page can put where a camera
        should be: it is indistinguishable from a working camera in an unlit
        room, and that is the wrong guess to send an installer off with. The
        empty state is markup rather than an absence, and it carries the
        station's own reason.
        """
        from gsu.console import Console

        markup = Console._preview({"reason": "no camera fitted"})
        self.assertIn("class=\"preview empty\"", markup)
        self.assertIn("id=preview-empty", markup)
        self.assertIn("no camera fitted", markup)

        # And it is gone the moment there is something to look at, or the
        # message would sit under every working picture.
        markup = Console._preview({"has_frame": True, "frame_age_s": 1.0})
        self.assertNotIn("preview empty", markup)

    def test_the_empty_state_never_renders_a_reason_unescaped(self):
        # The reason is the station's own text, but it carries a camera
        # address and ffmpeg's own words, and neither is this page's to trust
        # into markup.
        from gsu.console import Console

        markup = Console._preview({"reason": "<script>alert(1)</script>"})
        self.assertNotIn("<script>alert(1)</script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_the_still_frame_age_appears_once_there_is_one(self):
        # The cached still has not gone away — /frame.jpg still serves it and
        # its age is still stated. It is now a second opinion beside the live
        # picture rather than the picture itself.
        #
        # No assertion that the age is absent beforehand: /status.json is the
        # preview's demand signal, every other test in this class hits it, and
        # the capture thread obliges. That made the negative a statement about
        # test ordering rather than about the page.
        self.agent.video.cycle()
        _, body = self.request("GET", "/devices?slot=camera")
        self.assertIn("s old", body)
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
            # Named for people here too, not just on the Devices page — and the
            # name is the way to the tab that can fix it.
            self.assertIn(
                f"<a class=slot-link href='/devices?slot={slot}'>"
                f"{SLOT_LABELS[slot]}</a>",
                body,
            )
            self.assertNotIn(f"<span class=k>{slot}</span>", body)

    def test_the_enrolment_field_is_there_before_the_station_is_enrolled(self):
        _, body = self.request("GET", "/connection")
        self.assertIn("XXXX-XXXX-XXXX", body)
        # And the summary page points at it rather than duplicating it.
        _, summary = self.request("GET", "/")
        self.assertIn("/connection", summary)

    def _enrol(self, name="Station1", hours=48):
        from datetime import UTC, datetime, timedelta

        from gsu.credentials import Enrolment

        now = datetime.now(UTC)
        self.agent.enrolment = Enrolment.from_response({
            "station_id": "11111111-2222-3333-4444-555555555555",
            "credential": {
                "type": "bearer", "secret": "s",
                "expires_at": (now + timedelta(hours=hours)).isoformat(),
                "renew_after": (now + timedelta(hours=hours / 2)).isoformat(),
            },
            "broker": {
                "url": "redis://broker:6379/0", "username": "gsu:x",
                "telemetry_topic": "gsu/x/telemetry", "audio_topic": "gsu/x/audio",
                "command_topic": "cmd/gsu/x",
            },
            "station": {"name": name, "timezone": "Pacific/Auckland"},
            "config_version": 3,
        })

    def test_a_working_enrolled_station_shows_no_code_field(self):
        # A station is enrolled or it is not. With a credential the platform
        # still honours, there is nothing to type — just where it is enrolled.
        self._enrol()
        _, body = self.request("GET", "/connection")
        self.assertIn("Enrolled as Station1", body)
        self.assertNotIn("XXXX-XXXX-XXXX", body,
                         "an enrolled station offered a code field")

    def test_a_revoked_credential_brings_the_code_field_back(self):
        # The box cannot know the platform revoked it at the moment it happens;
        # it learns when a renewal is refused, which raises this condition. Once
        # it knows, it is effectively not enrolled, and the code field returns —
        # without a factory reset, and without a separate "re-enrol" concept.
        self._enrol()
        self.agent.health.raise_condition(
            "credential.revoked", "critical", "The platform rejected this box.")
        _, body = self.request("GET", "/connection")
        self.assertIn("no longer valid", body.lower())
        self.assertIn("XXXX-XXXX-XXXX", body,
                      "a revoked station had no way to enter a new code")

    def test_an_expired_credential_brings_the_code_field_back(self):
        self._enrol(hours=-1)  # already past its expiry
        _, body = self.request("GET", "/connection")
        self.assertIn("XXXX-XXXX-XXXX", body)

    def test_the_field_is_focused_when_the_station_needs_setting_up(self):
        # Not enrolled (or credential dead): the code is the whole job and the
        # cursor belongs in it.
        _, body = self.request("GET", "/connection")
        self.assertIn("autofocus", body)

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
        # The one endpoint that answers on loopback with no password, and the
        # cheapest proof from a shell that the agent is alive at all. An
        # updater used to gate on it; nothing does now, but a station you
        # cannot ask "are you working" over a tunnel is worse for the same
        # reason it was worth gating on.
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

    def test_the_connection_page_offers_the_position_fields(self):
        # Position is set by whoever is standing at the box — the read-only
        # freeze left an enrolled station with no way to be given one at all.
        _, _, body = self.page("/connection")
        self.assertIn("name=latitude", body)
        self.assertIn("name=longitude", body)

    def test_the_elevation_is_a_field(self):
        # It is part of the position, and it drives the barometric correction,
        # so it is set here with the coordinates rather than issued and frozen.
        _, _, body = self.page("/connection")
        self.assertIn("Elevation", body)
        self.assertIn("name=elevation_m", body)

    def test_the_correction_cannot_be_switched_on_without_one(self):
        # Refused rather than accepted-and-idle. The elevation it needs is now a
        # field on this same form, so the message points there.
        token, csrf, _ = self.page("/connection")
        self.request("POST", "/location",
                     f"adsb_baro_correction=1&csrf={csrf}", {"Cookie": token})
        self.assertFalse(self.agent.site.adsb_baro_correction)
        _, body = self.request("GET", "/connection", None, {"Cookie": token})
        self.assertIn("msg bad", body)
        self.assertIn("elevation", body.lower())

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

    def test_the_position_is_editable_at_the_box(self):
        # The person at the mast knows where the box is; the card gives them the
        # fields to say so, and the station's own position is preferred over
        # whatever the platform issued.
        _, _, body = self.page("/connection")
        card = body.split("<h2>Where this box is</h2>", 1)[1]
        self.assertIn("name=latitude", card)
        self.assertIn("name=longitude", card)

    def test_the_local_settings_are_inline_and_need_no_dialog(self):
        # No :target dialog and no script: an installer's phone with scripts
        # blocked has to be able to set a position, and the whole card is
        # ordinary fields.
        _, _, body = self.page("/connection")
        self.assertNotIn("class=modal", body)
        card = body.split("<h2>Where this box is</h2>", 1)[1]
        self.assertIn("name=elevation_m", card)
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
            "POST", "/location", f"csrf={csrf}", {"Cookie": token},
        )
        self.assertEqual(response.status, 303)
        # From the fixed map, never from the request.
        self.assertEqual(response.getheader("Location"), "/connection")
        _, body = self.request("GET", "/connection", None, {"Cookie": token})
        self.assertIn("Saved.", body)

    def test_a_refused_save_says_why_on_the_page(self):
        # No dialog to reopen, so the reason goes where every other refusal on
        # this page goes.
        # The one refusal left on this form: switching the correction on when
        # the station has no elevation to compute it against.
        token, csrf, _ = self.page("/connection")
        self.request("POST", "/location",
                     f"adsb_baro_correction=1&csrf={csrf}", {"Cookie": token})
        _, body = self.request("GET", "/connection", None, {"Cookie": token})
        self.assertEqual(body.count("msg bad"), 1, "said twice is said wrong")
        self.assertIn("elevation", body.lower())

    def test_the_local_settings_use_the_shared_field_grid(self):
        _, _, body = self.page("/connection")
        # Bounded to this card: the sections below it have fields of their own.
        card = body.split("<h2>Where this box is</h2>", 1)[1].split("<h2>", 1)[0]
        # Latitude, longitude, elevation, the correction checkbox, and the save
        # row — all in the shared .field grid.
        self.assertEqual(card.count("<div class=field>"), 5)
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

    def test_the_device_status_line_sits_in_the_control_column(self):
        # Left flush under indented controls is exactly the raggedness the grid
        # was added to remove. The Devices tab has no Save button now — the fetch
        # writes each change's outcome to this status line, which takes the
        # button's old place in its own label-less .field, in the control column.
        _, _, body = self.page("/devices")
        self.assertIn(
            "<div class=field>"
            "<span class='muted device-status' aria-live=polite></span></div>",
            body,
        )


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
            single_instance=False, demo=True))
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
            single_instance=False, demo=True))
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
            single_instance=False, demo=True))
        self.addCleanup(self.agent.shutdown)
        self.console = Console(self.agent)

    def submit(self, **fields):
        return self.console._set_location(
            {name: [value] for name, value in fields.items()}
        )

    def with_elevation(self, metres):
        """Elevation arrives with the enrolment now, not from this page."""
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
                "latitude": -42.4, "longitude": 173.68, "elevation_m": metres,
            },
            "config_version": 1,
        })

    def test_it_is_off_until_somebody_turns_it_on(self):
        # It applies one sensor's reading to another sensor's data. That is a
        # decision an operator makes, never a default.
        self.assertFalse(self.agent.site.adsb_baro_correction)

    def test_ticking_it_with_an_elevation_switches_it_on(self):
        self.with_elevation(120.0)
        self.submit(adsb_baro_correction="1")
        self.assertTrue(self.agent.site.adsb_baro_correction)
        self.assertEqual(self.agent.effective_elevation_m(), 120.0)

    def test_ticking_it_without_an_elevation_is_refused_not_accepted_idle(self):
        # A checkbox that stays ticked while nothing happens is how somebody
        # comes to trust a number that was never computed. The elevation it
        # needs is now a field on this same form, so the message points there.
        with self.assertRaises(ValueError) as caught:
            self.submit(adsb_baro_correction="1")
        self.assertIn("elevation", str(caught.exception).lower())
        self.assertFalse(self.agent.site.adsb_baro_correction)

    def test_an_unticked_box_turns_it_off(self):
        # An unchecked checkbox sends nothing, and on this form that absence is
        # a real "off" because the input is always rendered inside it.
        self.with_elevation(120.0)
        self.submit(adsb_baro_correction="1")
        self.submit()
        self.assertFalse(self.agent.site.adsb_baro_correction)

    def test_it_cannot_be_left_on_by_a_station_with_no_elevation(self):
        # There is no "clear the location" here any more — the position is the
        # enrolment's. What must not happen is a switch left claiming a
        # correction a box cannot compute.
        self.with_elevation(120.0)
        self.submit(adsb_baro_correction="1")
        self.assertTrue(self.agent.site.adsb_baro_correction)
        self.agent.enrolment = None
        with self.assertRaises(ValueError):
            self.submit(adsb_baro_correction="1")

    def test_a_caller_that_does_not_mention_it_does_not_change_it(self):
        # The switch also arrives by config.set from the platform. Saving a
        # coordinate must not silently undo that.
        self.agent.site.adsb_baro_correction = True
        self.agent.set_location(-42.4004, 173.68, 120.0)
        self.assertTrue(self.agent.site.adsb_baro_correction)

    def test_it_survives_a_reload_of_the_site_file(self):
        self.with_elevation(120.0)
        self.submit(adsb_baro_correction="1")
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


class BrokerSecurityRowTests(unittest.TestCase):
    """The one line that says whether the broker link is safe to leave running.

    The broker moved onto the platform's own 443, behind the same public
    certificate as the API (tls.resolve_broker). So a broker verified against
    the system CA bundle is the deliberate, correct end state on a
    proxy-terminated deployment — not a downgrade to warn about — and the row
    must say so, or it cries wolf on every such box for ever.
    """

    def _row(self, security, trust):
        return Console._security_row(security, trust)

    def test_a_proxy_terminated_broker_is_ok_not_a_warning(self):
        # `mode == "system"` is only ever reached because the platform stated it
        # at enrolment; there is no private CA to pin and none coming. Green, and
        # worded like the API row, not "not pinned" in yellow.
        _, text, css = self._row(
            {"broker_tls": True, "publishing": True, "broker_url": "wss://x/broker"},
            {"mode": "system", "fingerprint": None},
        )
        self.assertEqual(css, "ok")
        self.assertIn("public certificate", text)
        self.assertNotIn("not pinned", text)

    def test_a_pinned_broker_still_shows_its_fingerprint_and_is_ok(self):
        _, text, css = self._row(
            {"broker_tls": True, "publishing": True, "broker_url": "wss://x/broker"},
            {"mode": "pinned", "fingerprint": "abc123def456abc123def456"},
        )
        self.assertEqual(css, "ok")
        self.assertIn("pinned", text)

    def test_the_genuine_failures_this_row_exists_for_are_untouched(self):
        # Greening the public-certificate case must not soften a refused
        # certificate or a plaintext link — the states this line is really for.
        _, _, refused = self._row({"tls_failed": True}, {"mode": "system"})
        self.assertEqual(refused, "bad")
        _, _, plaintext = self._row(
            {"broker_tls": False, "publishing": True, "broker_url": "ws://x"},
            {"mode": "system"},
        )
        self.assertEqual(plaintext, "bad")

class DevicePickerTests(unittest.TestCase):
    """Choosing a device re-renders the form before anything is saved.

    With one form, picking a different device left the previous device's
    parameter fields on screen — they render from the *stored* type — so Save
    posted a serial baud to a network camera and came back with errors about
    fields nobody had been offered. The order was wrong: you cannot fill in a
    device's settings before the page knows which device you mean.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.agent = Agent(AgentConfig(
            home=Path(self.directory.name), setup_enabled=False,
            single_instance=False, demo=True))
        self.addCleanup(self.agent.shutdown)
        self.console = Console(self.agent)

    def section(self, slot, chosen=None):
        return self.console._section_devices(
            self.agent.snapshot(), "tok", slot, chosen,
        )

    def test_the_form_follows_the_choice_before_it_is_saved(self):
        # The Airmar has a serial port and a baud; the demo weather station has
        # neither. Choosing the demo must not leave serial fields on screen.
        self.agent.inventory.set_device("weather", "airmar-110wx", {})
        stored = self.section("weather")
        self.assertIn("p_port", stored)
        previewed = self.section("weather", "simulated-weather")
        self.assertNotIn("p_port", previewed)
        # And nothing was written by looking.
        self.assertEqual(
            self.agent.inventory.fitted["weather"].type_id, "airmar-110wx")

    def test_the_previewed_device_is_what_save_will_store(self):
        self.agent.inventory.set_device("weather", "airmar-110wx", {})
        previewed = self.section("weather", "simulated-weather")
        self.assertIn(
            "<input type=hidden name=type_id value='simulated-weather'>", previewed)

    def test_the_radio_page_renders_on_an_enrolled_station(self):
        # Regression, and a page-down one: once a station is enrolled the main
        # snapshot's `station` key is its NAME — a string — but the radio section
        # read it as the site dict, so `station.get(...)` raised and took the
        # whole setup page with it (ERR_EMPTY_RESPONSE). A demo box is not
        # enrolled, so `state["station"]` was None and every test slipped past
        # it; forcing the string is what reproduces it.
        self.agent.inventory.set_device("radio", "simulated-airband", {}, None)
        self.agent.build_devices()
        snap = self.agent.snapshot()
        snap["station"] = "Bench Station"
        html = self.console._section_devices(snap, "tok", "radio")
        self.assertIn("radio_transcribe", html)

    def _radio_panel(self):
        self.agent.inventory.set_device("radio", "simulated-airband", {}, None)
        self.agent.build_devices()
        return self.console._section_devices(self.agent.snapshot(), "tok", "radio")

    def test_the_radio_panel_has_one_button_not_a_save_and_an_apply(self):
        # The owner requirement that drove the rework: the device Save and the
        # radio Apply were two buttons in two places for one panel. Now there is
        # one — Apply — and no Save on the radio tab. (The picker's own button is
        # "Change", a navigation, and the script hides it.) The Apply button is
        # the no-script fallback; with the script running each control applies on
        # change.
        panel = self._radio_panel()
        self.assertEqual(panel.count(">Apply</button>"), 1)
        self.assertNotIn(">Save</button>", panel)

    def test_the_radio_form_is_marked_for_instant_apply(self):
        # The nonce'd script hides the Apply button and posts each change over
        # fetch; it finds the form by this marker.
        panel = self._radio_panel()
        self.assertIn("action='/radio' data-radio>", panel)
        self.assertIn("id=radio-status", panel)

    def test_the_channel_and_voice_filters_are_offered_on_the_real_receiver(self):
        # Commissioning settings on the real device — surfaced from the registry
        # as dev_<param> fields, minus gain/ppm which have their own controls.
        # Previewed against rtlsdr-airband; the demo receiver has no such params.
        panel = self.console._section_devices(
            self.agent.snapshot(), "tok", "radio", chosen="rtlsdr-airband")
        self.assertIn("dev_voice_filter", panel)
        self.assertIn("dev_channel_bw_hz", panel)
        self.assertIn("dev_bias_tee", panel)
        # The voice filter defaults on (its registry default), so it renders
        # checked before anything is stored.
        self.assertIn("id=dev_voice_filter name=dev_voice_filter value='1' checked",
                      panel)

    def test_setting_the_filters_persists_them_as_device_params(self):
        # A dev_<param> change rebuilds the receiver and is stored with the
        # device. Called directly, so no live hardware is needed — the params
        # land in the inventory whether or not the dongle opens.
        self.console._set_radio({
            "type_id": ["rtlsdr-airband"],
            "dev_channel_bw_hz": ["5000"],
            # dev_voice_filter omitted — an unticked box, so a real "off".
        })
        fitted = self.agent.inventory.fitted["radio"]
        self.assertEqual(fitted.type_id, "rtlsdr-airband")
        self.assertEqual(fitted.params.get("channel_bw_hz"), 5000)
        self.assertFalse(fitted.params.get("voice_filter"))

    def test_the_volume_control_is_outside_the_apply_form(self):
        # Local only: moving the volume must not post a settings change, so it
        # sits after the form (its status line, radio-status, is the form's last
        # element) rather than inside it.
        panel = self._radio_panel()
        self.assertLess(panel.index("id=radio-status"), panel.index("id=volume"))

    def test_the_gain_is_a_stepped_select_of_the_tuners_own_steps(self):
        # "the same steps as the platform": a select of the tuner's gain table,
        # read from the device, not a free number it would snap away from.
        panel = self._radio_panel()
        self.assertIn("<select id=gain name=gain>", panel)
        gains = self.agent.snapshot()["radio"]["gains"]
        self.assertTrue(gains)
        for step in gains:
            self.assertIn(f">{float(step):.1f}</option>", panel)

    def test_the_gain_offers_auto_and_managed_alongside_the_steps(self):
        # AUTO is the tuner's own AGC; Managed is the software one that holds a
        # fixed step. Both sit above the discrete gains in the same select.
        panel = self._radio_panel()
        self.assertIn("<option value=auto", panel)
        self.assertIn("<option value=managed", panel)
        self.assertIn("id=gain-managed", panel)

    def test_the_squelch_controls_match_the_dashboard(self):
        # The platform's signal indicator, verbatim: a threshold-or-AUTO pair
        # over a meter (fill = signal, hairline = floor, thumb = threshold) and a
        # readout with the channel LED — not a bare number.
        panel = self._radio_panel()
        self.assertIn("name=squelch", panel)
        self.assertIn("name=auto_squelch", panel)
        self.assertIn("class=radio-readout", panel)
        self.assertIn("class=meter", panel)
        self.assertIn("id=meter-fill", panel)
        self.assertIn("id=sig-led", panel)

    def test_the_panel_lets_the_receiver_be_heard_before_enrolment(self):
        # No volume control was the gap: an installer could not test the radio
        # here. It is a volume slider driving a Web Audio player — no <audio>
        # element and no play button — over the same /audio.wav the CLI examples
        # use. The slider is in the section; the fetch lives in the page script.
        panel = self._radio_panel()
        self.assertIn("id=volume", panel)
        self.assertNotIn("<audio", panel)
        from gsu.console import Console
        self.assertIn("/audio.wav", Console._devices_script("nonce"))

    def _save_camera(self, type_id, **params):
        form = {"slot": ["camera"], "type_id": [type_id]}
        for name, value in params.items():
            form[f"p_{name}"] = [value]
        return self.console._set_device(form)

    def test_changing_the_camera_stops_the_stream_that_shows_the_old_one(self):
        # The reported bug: switch the demo card to an RTSP source and the box
        # goes on displaying and sending the demo. A running stream built its
        # source from the camera that is being replaced, so it has to be stopped
        # for the swap to take — and stopped even when the platform, not this
        # page, is the one watching, which is the case that needed a restart.
        self._save_camera("simulated-camera")
        self.agent.stream.state = "streaming"
        self.agent.stream._local_only = False  # the platform is watching

        self._save_camera("onvif-network-camera", address="192.168.1.9")

        self.assertEqual(self.agent.stream.state, "idle")
        self.assertEqual(
            self.agent.inventory.fitted["camera"].type_id, "onvif-network-camera")

    def test_a_camera_save_that_changes_nothing_leaves_the_stream_alone(self):
        # A no-op save must not yank a viewer's stream: only a real change is
        # worth the few seconds of black.
        self._save_camera("onvif-network-camera", address="192.168.1.9")
        self.agent.stream.state = "streaming"
        self.agent.stream._local_only = False

        self._save_camera("onvif-network-camera", address="192.168.1.9")

        self.assertEqual(self.agent.stream.state, "streaming")

    def test_another_devices_stored_values_are_not_offered_as_defaults(self):
        # A baud that belonged to the Airmar must not appear pre-filled under a
        # device that happens to have a field of the same name.
        self.agent.inventory.set_device(
            "weather", "airmar-110wx", {"port": "/dev/ttyUSB9", "baud": 4800})
        previewed = self.section("weather", "generic-nmea-weather")
        self.assertNotIn("/dev/ttyUSB9", previewed)

    def test_the_stored_values_are_kept_when_the_choice_is_the_stored_one(self):
        self.agent.inventory.set_device(
            "weather", "airmar-110wx", {"port": "/dev/ttyUSB9", "baud": 4800})
        self.assertIn("/dev/ttyUSB9", self.section("weather", "airmar-110wx"))

    def test_choosing_is_a_get_and_saving_is_a_post(self):
        # Looking at a device's settings must not be something a prefetcher or
        # a back button can write.
        section = self.section("weather")
        self.assertIn("<form method=get action='/devices'", section)
        self.assertIn("<form method=post action='/device'", section)

    def test_it_works_with_no_script(self):
        # The select alone is enough only when the script is running; without
        # it there has to be something to press.
        self.assertIn("pick-go", self.section("weather"))

    def test_a_hand_edited_type_cannot_render_a_foreign_form(self):
        # The query is not trusted: a camera type in the weather slot, or a
        # type this build has never heard of, falls back to what is stored.
        self.agent.inventory.set_device("weather", "airmar-110wx", {})
        for bogus in ("onvif-network-camera", "not-a-device", "../../etc/passwd"):
            section = self.section("weather", bogus)
            self.assertIn(
                "<input type=hidden name=type_id value='airmar-110wx'>",
                section, bogus,
            )

    def test_clearing_the_slot_is_a_real_choice(self):
        self.agent.inventory.set_device("weather", "airmar-110wx", {})
        section = self.section("weather", "")
        self.assertIn("<input type=hidden name=type_id value=''>", section)

class FactoryResetTests(unittest.TestCase):
    """Returning a box to how it shipped.

    The owner's call on ceremony, and it holds: this page answers only on the
    local network, behind a password, inside a time-boxed window, so anybody
    who can see the button is at the hardware intending to reprovision it. Two
    clicks, no typed station name. What is destroyed is a box's configuration,
    not a customer's records — those live on the platform.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = AgentConfig(
            home=Path(self.directory.name), setup_enabled=False,
            single_instance=False, demo=True)
        self.agent = Agent(self.config)
        self.addCleanup(self.agent.shutdown)
        self.console = Console(self.agent)

    def test_it_clears_everything_that_describes_the_old_site(self):
        # Anything left behind makes a reset box behave like the site it came
        # from: an old device list makes a new owner's slots wrong, and kept
        # events are one customer's data on another's hardware.
        self.agent.inventory.set_device("weather", "airmar-110wx", {})
        self.agent.set_location(-42.4, 173.7, 120.0)
        self.agent.store.record_event("test", "info", "something happened")

        self.agent.factory_reset()

        self.assertFalse(self.config.credential_path.exists())
        self.assertFalse(self.config.devices_path.exists())
        self.assertFalse(self.config.site_config_path.exists())
        # The store is rebuilt immediately so the page still works, so what
        # matters is that it is empty rather than that the file is gone.
        self.assertEqual(self.agent.store.recent_events(10), [])

    def test_the_reset_box_is_a_blank_box_in_memory_too(self):
        # Not just on disk: the page the operator is looking at has to show the
        # reset, rather than the old world until somebody restarts the service.
        self.agent.set_location(-42.4, 173.7, 120.0)
        self.agent.inventory.set_device("weather", "airmar-110wx", {})
        self.agent.factory_reset()
        self.assertIsNone(self.agent.site.latitude)
        self.assertIsNone(self.agent.site.elevation_m)
        self.assertIsNone(self.agent.enrolment)
        # Back to what a box ships with, which is every slot on its Demo
        # sensor — not empty. A reset box is a working demo station, and the
        # important part is that it is no longer the *previous site's*
        # selection.
        self.assertEqual(
            self.agent.inventory.fitted["weather"].type_id, "simulated-weather")

    def test_it_says_what_it_cleared(self):
        self.agent.inventory.set_device("weather", "airmar-110wx", {})
        gone = self.agent.factory_reset()
        self.assertIn("credential", gone)
        self.assertIn("device selections", gone)

    def test_it_keeps_the_setup_password(self):
        # It lives in the environment file, not the state directory. A reset
        # that locks the person doing it out of the box is a site visit.
        before = self.console.gate.has_password
        self.agent.factory_reset()
        self.assertEqual(self.console.gate.has_password, before)

    def test_conditions_about_the_old_configuration_do_not_survive(self):
        # "No weather head" was true of a configuration that no longer exists.
        self.agent.health.raise_condition("devices.absent", "warning", "weather")
        self.agent.factory_reset()
        self.assertEqual(self.agent.health.active(), [])

    def test_an_unconfirmed_post_changes_nothing(self):
        self.agent.inventory.set_device("weather", "airmar-110wx", {})
        with self.assertRaises(ValueError):
            self.console._reset({})
        self.assertEqual(
            self.agent.inventory.fitted["weather"].type_id, "airmar-110wx")

    def test_the_confirmation_needs_no_script(self):
        # :target, like every other two-step control here — the form does not
        # exist on the page until the fragment names it.
        section = self.console._section_reset(self.agent.snapshot(), "tok")
        self.assertIn("href='#reset'", section)
        self.assertIn("id=reset class=confirm", section)
        self.assertIn(".confirm:target",
                      (Path(__file__).resolve().parents[1]
                       / "gsu" / "console.py").read_text())

    def test_it_is_the_last_thing_on_the_page(self):
        # Nothing below the most destructive control, so an accidental
        # scroll-and-click cannot land past it.
        body = self.console.render(None, "/connection")
        self.assertGreater(body.index("<h2>Reset</h2>"), body.index("<h2>Security</h2>"))

    def test_only_one_red_button_shows_at_a_time(self):
        # The two danger buttons — the trigger and the commit — must never be on
        # screen together, or it is unclear which arms and which fires. The CSS
        # hides the trigger when the confirm is open, and `~` reaches forward
        # only, so the trigger has to come AFTER the confirm in the markup for
        # that rule to bite. Assert the order, and the rule that depends on it.
        section = self.console._section_reset(self.agent.snapshot(), "tok")
        self.assertLess(
            section.index("id=reset class=confirm"),
            section.index("id=reset-trigger"),
            "the trigger must follow the confirm or the CSS cannot hide it",
        )
        css = (Path(__file__).resolve().parents[1] / "gsu" / "console.py").read_text()
        self.assertIn("#reset:target ~ #reset-trigger", css)

class DemoProvisioningTests(unittest.TestCase):
    """Demo is decided when the box is provisioned, not slot by slot afterwards.

    A fresh station used to come up with every slot on its Demo sensor. That
    made a demo box free and a real installation expensive: six slots to
    un-demo, and the alertness to notice they needed it. The default is now
    nothing fitted, which is true of a box nobody has configured, and
    `GSU_DEMO=1` at provisioning time gives the demo station instead.
    """

    def agent(self, **kwargs):
        from gsu.devices import registry  # noqa: F401 - used below
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        agent = Agent(AgentConfig(
            home=Path(directory.name), setup_enabled=False,
            single_instance=False, **kwargs,
        ))
        self.addCleanup(agent.shutdown)
        return agent

    def test_a_real_box_starts_with_nothing_selected(self):
        fitted = self.agent().inventory.fitted
        self.assertEqual({s: e.type_id for s, e in fitted.items() if e.type_id}, {})

    def test_a_demo_box_starts_complete(self):
        from gsu.devices import registry
        fitted = self.agent(demo=True).inventory.fitted
        for slot in registry.SLOTS:
            self.assertTrue(fitted[slot].type_id, slot)
            self.assertTrue(registry.get(fitted[slot].type_id).simulated, slot)

    def test_the_flag_only_seeds_a_box_nobody_has_configured(self):
        # Provisioning, not a runtime switch. Turning it on later must not
        # replace somebody's real sensors.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        first = Agent(AgentConfig(home=home, setup_enabled=False,
                                  single_instance=False))
        first.inventory.set_device("weather", "airmar-110wx", {})
        first.shutdown()

        second = Agent(AgentConfig(home=home, setup_enabled=False,
                                   single_instance=False, demo=True))
        self.addCleanup(second.shutdown)
        self.assertEqual(
            second.inventory.fitted["weather"].type_id, "airmar-110wx")

    def test_it_is_read_from_the_environment(self):
        from gsu.config import AgentConfig as AC
        with mock.patch.dict(os.environ, {"GSU_DEMO": "1"}, clear=False):
            self.assertTrue(AC.from_env().demo)
        with mock.patch.dict(os.environ, {"GSU_DEMO": "0"}, clear=False):
            self.assertFalse(AC.from_env().demo)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GSU_DEMO", None)
            self.assertFalse(AC.from_env().demo)

    def test_a_reset_returns_the_box_to_its_provisioned_kind(self):
        # A demo box resets to a demo box; a real one resets to empty slots.
        demo = self.agent(demo=True)
        demo.inventory.set_device("weather", "airmar-110wx", {})
        demo.factory_reset()
        self.assertEqual(
            demo.inventory.fitted["weather"].type_id, "simulated-weather")

        real = self.agent()
        real.inventory.set_device("weather", "airmar-110wx", {})
        real.factory_reset()
        # Back to nothing selected, which for a real box means the slot is not
        # in the map at all rather than present-and-empty.
        self.assertFalse(real.inventory.fitted.get("weather"))

    def test_a_demo_sensor_is_badged_on_the_summary(self):
        console = Console(self.agent(demo=True))
        body = console.render(None, "/")
        self.assertIn("<span class='pill demo'>DEMO</span>", body)

    def test_a_real_sensor_is_not(self):
        agent = self.agent()
        agent.inventory.set_device("weather", "airmar-110wx", {})
        agent.build_devices()
        body = Console(agent).render(None, "/")
        rows = [r for r in body.split("<div class=slot-row>") if "Weather" in r]
        self.assertTrue(rows)
        self.assertNotIn("pill demo", rows[0])
