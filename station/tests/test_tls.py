"""The trust rules, which are the ones that must not quietly stop holding.

Everything here is a property the station has to keep whatever else changes:
it pins, it refuses the wrong CA, it refuses plaintext, it never downgrades,
and it never authenticates as anyone but itself. Those are easy to lose in a
refactor and impossible to notice from the outside — a downgraded station looks
exactly like a working one until somebody reads a packet capture.

The tests that need a certificate generate one with `openssl` into a temporary
directory and are skipped where it is missing. The CA is generated with
`basicConstraints` and `keyUsage` because Python's `ssl` module rejects a CA
without them ("CA cert does not include key usage extension") — a real trap,
since `redis-cli --cacert` accepts such a CA happily and proves nothing about
whether this station will.
"""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gsu import tls
from gsu.agent import Agent
from gsu.config import AgentConfig
from gsu.enrolment import EnrolmentClient
from gsu.transport import build_transport, redact_url

STATION = "29ed8568-999e-4725-8daa-3ee3cea1751e"
HAS_OPENSSL = shutil.which("openssl") is not None


def make_ca(directory: Path, name: str = "ca") -> Path:
    """A CA Python will actually accept as one."""
    key, crt = directory / f"{name}.key", directory / f"{name}.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(crt), "-days", "2",
         "-subj", f"/CN=Percepta Test {name}",
         "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        check=True, capture_output=True,
    )
    return crt


def make_server_cert(directory: Path, ca: Path) -> tuple[Path, Path]:
    key, csr, crt = directory / "s.key", directory / "s.csr", directory / "s.crt"
    ext = directory / "s.ext"
    ext.write_text(
        "basicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
    )
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(csr), "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca),
         "-CAkey", str(ca.with_suffix(".key")), "-CAcreateserial",
         "-out", str(crt), "-days", "2", "-extfile", str(ext)],
        check=True, capture_output=True,
    )
    return crt, key


class PolicyTests(unittest.TestCase):
    """What the station will and will not connect to, before any socket."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.home = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def pinned(self) -> tls.Trust:
        return tls.Trust(mode=tls.TRUST_PINNED, path=self.home / "ca.pem",
                         source="test", fingerprint="AA:BB")

    def test_plaintext_is_refused_once_a_ca_is_pinned(self):
        # The downgrade this whole module exists to prevent.
        with self.assertRaises(tls.Refusal) as caught:
            self.pinned().check("redis://broker:6379/0", "the broker")
        self.assertIn("rediss", str(caught.exception))

    def test_tls_without_a_ca_is_refused_rather_than_using_the_system_store(self):
        with self.assertRaises(tls.Refusal):
            tls.Trust().check("rediss://broker:6380/0", "the broker")
        with self.assertRaises(tls.Refusal):
            tls.Trust().check("https://platform:8000", "the platform API")

    def test_require_tls_refuses_plaintext_even_with_no_ca(self):
        with self.assertRaises(tls.Refusal):
            tls.Trust(require_tls=True).check("redis://broker:6379/0", "the broker")

    def test_plaintext_is_allowed_only_on_an_unpinned_development_box(self):
        tls.Trust().check("redis://127.0.0.1:6380/0", "the broker")

    def test_an_unknown_scheme_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(tls.Refusal):
            tls.Trust().check("gopher://broker/0", "the broker")

    def test_there_is_no_mode_that_disables_verification(self):
        # The system bundle is weaker than pinning and still verifies. If a
        # third mode ever appears, this is where it has to be argued for.
        self.assertEqual(set(tls.TRUST_MODES), {"pinned", "system"})
        trusts = [tls.Trust(mode=tls.TRUST_SYSTEM)]
        if HAS_OPENSSL:
            trusts.append(tls.Trust(mode=tls.TRUST_PINNED, path=make_ca(self.home)))
        for trust in trusts:
            context = trust.context()
            self.assertTrue(context.check_hostname, trust.mode)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED, trust.mode)
            self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_a_pinned_ca_that_has_gone_missing_is_a_refusal(self):
        # Not a fallback. A trust root that disappears is not permission to
        # trust whatever answers instead.
        trust = tls.Trust(mode=tls.TRUST_PINNED, path=self.home / "vanished.pem")
        with self.assertRaises(tls.Refusal):
            trust.context()

    def test_an_unreadable_installed_ca_is_a_refusal_not_a_fallback(self):
        store = tls.CaStore(self.home / "stored.pem")
        missing = str(self.home / "missing.pem")
        with self.assertRaises(tls.Refusal):
            tls.resolve_broker(store, installed=missing)
        # Especially for the API, where the fallback would be *silent*: the
        # operator asked for pinning and would have got the system bundle.
        with self.assertRaises(tls.Refusal):
            tls.resolve_api(installed=missing)

    def test_the_broker_and_the_api_have_separate_trust_roots(self):
        # The whole point of the split. broker.ca_pem is the broker's root; the
        # API is expected behind a proxy with a public certificate.
        store = tls.CaStore(self.home / "stored.pem")
        broker = tls.resolve_broker(store)
        api = tls.resolve_api()
        self.assertEqual(broker.mode, tls.TRUST_PINNED)
        self.assertEqual(broker.purpose, "broker")
        self.assertEqual(api.mode, tls.TRUST_SYSTEM)
        self.assertEqual(api.purpose, "api")

    def test_the_broker_does_not_reach_system_trust_on_its_own(self):
        # The default is unchanged and is the safe one: nothing pinned, nothing
        # stated, so every TLS URL is refused rather than verified against
        # whatever roots happen to be installed.
        store = tls.CaStore(self.home / "stored.pem")
        self.assertEqual(tls.resolve_broker(store).mode, tls.TRUST_PINNED)
        with self.assertRaises(tls.Refusal):
            tls.resolve_broker(store).check("rediss://broker:6380/0", "the broker")

    def test_an_unrecognised_stated_mode_still_refuses(self):
        # Omission and nonsense both mean "I was not told", and neither is
        # permission. An older platform sends no ca_mode at all, and that box
        # must keep refusing rather than quietly widening its trust.
        store = tls.CaStore(self.home / "stored.pem")
        for stated in (None, "", "sytsem", "none", "off", "insecure"):
            trust = tls.resolve_broker(store, stated_mode=stated)
            self.assertEqual(trust.mode, tls.TRUST_PINNED, stated)
            with self.assertRaises(tls.Refusal, msg=stated):
                trust.check("wss://platform.example/broker", "the relay")

    def test_the_platform_may_state_system_trust_for_the_relay(self):
        # The deployment case: the relay is served by the platform's own 443
        # behind a proxy holding a publicly trusted certificate, so there is no
        # private CA on the wire to pin. Refusing here was a station that
        # enrolled perfectly and then published nothing.
        store = tls.CaStore(self.home / "stored.pem")
        trust = tls.resolve_broker(store, stated_mode=tls.TRUST_SYSTEM)
        self.assertEqual(trust.mode, tls.TRUST_SYSTEM)
        trust.check("wss://platform.example/broker", "the relay")
        # Still full verification, and still no way to ask for less.
        self.assertEqual(trust.context().verify_mode, ssl.CERT_REQUIRED)

    @unittest.skipUnless(HAS_OPENSSL, "needs openssl to make a certificate")
    def test_a_pinned_ca_outranks_a_stated_system_mode(self):
        # Precedence is the property that keeps this from being a downgrade:
        # no box that is pinned today can be argued out of it by an answer
        # from the platform, whether the CA came from enrolment or was
        # installed out of band.
        pem = make_ca(self.home, "broker").read_text()
        store = tls.CaStore(self.home / "stored.pem")
        store.save(pem)
        self.assertEqual(
            tls.resolve_broker(store, stated_mode=tls.TRUST_SYSTEM).mode,
            tls.TRUST_PINNED,
        )
        installed = str(make_ca(self.home, "installed"))
        self.assertEqual(
            tls.resolve_broker(tls.CaStore(self.home / "empty.pem"),
                               installed=installed,
                               stated_mode=tls.TRUST_SYSTEM).mode,
            tls.TRUST_PINNED,
        )

    @unittest.skipUnless(HAS_OPENSSL, "needs openssl to make a certificate")
    def test_the_api_can_be_pinned_when_the_platform_serves_its_own_cert(self):
        api = tls.resolve_api(installed=str(make_ca(self.home, "api")))
        self.assertEqual(api.mode, tls.TRUST_PINNED)
        self.assertIsNotNone(api.fingerprint)
        api.check("https://platform:8000", "the platform API")

    def test_the_two_refusals_name_different_fixes(self):
        store = tls.CaStore(self.home / "stored.pem")
        with self.assertRaises(tls.Refusal) as broker:
            tls.resolve_broker(store).check("rediss://b:6380/0", "the broker")
        self.assertIn("enrolment response", str(broker.exception))
        self.assertIn("GSU_CA_FILE", str(broker.exception))

        stranded = tls.Trust(mode=tls.TRUST_PINNED, purpose="api")
        with self.assertRaises(tls.Refusal) as api:
            stranded.check("https://p:8000", "the platform API")
        self.assertIn("GSU_API_CA_FILE", str(api.exception))

    def test_the_mandatory_redis_tls_settings_are_all_present(self):
        kwargs = tls.Trust(mode="pinned", path=self.home / "ca.pem").redis_kwargs(
            "rediss://broker:6380/0"
        )
        self.assertEqual(kwargs["ssl_cert_reqs"], "required")
        self.assertIs(kwargs["ssl_check_hostname"], True)
        self.assertIn("ssl_ca_certs", kwargs)

    def test_a_pinned_ca_is_context_material_not_decoration(self):
        self.assertEqual(tls.Trust().redis_kwargs("redis://broker:6379/0"), {})


class CaStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = tls.CaStore(Path(self._dir.name) / "platform-ca.pem")

    def tearDown(self):
        self._dir.cleanup()

    @unittest.skipUnless(HAS_OPENSSL, "needs openssl to make a certificate")
    def test_it_is_written_0600_and_a_change_is_reported(self):
        directory = Path(self._dir.name)
        first = make_ca(directory, "one").read_text()
        second = make_ca(directory, "two").read_text()

        self.assertFalse(self.store.save(first), "a first CA is not a rotation")
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.store.save(first), "the same CA is not a change")
        # A CA that changes is either a rotation or somebody else's certificate,
        # and from the station those are indistinguishable — so it is reported.
        self.assertTrue(self.store.save(second))
        self.assertIsNotNone(tls.fingerprint(self.store.load()))

    def test_rubbish_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.save("-----BEGIN NOT A CERTIFICATE-----")
        self.assertIsNone(self.store.load())


class UrlSecrecyTests(unittest.TestCase):
    """A URL is logged by every proxy between here and the platform.

    This class used to be about a genuine redis-py trap:
    `ConnectionPool.from_url` finished with `kwargs.update(url_options)`, so a
    URL carrying `user:pass@` silently replaced the station's identity with
    whatever it named — failing confusingly at best, and at worst succeeding as
    somebody else, which is a station that has left the tenancy model the whole
    platform rests on.

    That trap is gone with the Redis transport, and with the username contract
    2.0 removed: there is no principal for a URL to override, and the
    credential travels in an `Authorization` header rather than anywhere a URL
    could reach. What survives is the display rule, because a hand-edited
    `GSU_BROKER_URL` can still carry a secret and the setup console has no
    authentication in front of it.
    """

    def test_display_never_leaks_the_password(self):
        shown = redact_url("wss://bob:s3cret@host/broker")
        self.assertNotIn("s3cret", shown)
        self.assertIn("host", shown)

    def test_a_clean_url_is_left_exactly_alone(self):
        for url in ("wss://platform.example/broker", "ws://127.0.0.1:8099/broker",
                    "https://platform:8000"):
            self.assertEqual(redact_url(url), url)

    def test_only_the_relay_is_a_transport_now(self):
        """`rediss://` was a transport and is not one any more.

        Deleted rather than deprecated: it spoke a topic-based protocol no
        contract document describes, and it was the only reason the transport
        interface had to stay topic-shaped. A URL that used to work must fail
        loudly rather than fall back to something that looks similar.
        """
        with self.assertRaises(ValueError):
            build_transport("rediss://broker:6380/0", secret="x")


@unittest.skipUnless(HAS_OPENSSL, "needs openssl to make a certificate")
class PinnedApiTests(unittest.TestCase):
    """The API half, against a real TLS server on a real socket."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        directory = Path(cls._dir.name)
        cls.ca = make_ca(directory, "ca")
        cls.other_ca = make_ca(directory, "other")
        crt, key = make_server_cert(directory, cls.ca)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({
                    "station_id": STATION, "name": "Test", "config_version": 1,
                    "credential_expires_at": None, "renew_now": False,
                    "server_time": datetime.now(UTC).isoformat(),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(crt, key)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.url = f"https://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls._dir.cleanup()

    def trust(self, ca: Path) -> tls.Trust:
        return tls.Trust(mode=tls.TRUST_PINNED, path=ca, source="test",
                         fingerprint=tls.fingerprint(ca.read_text()))

    def test_the_right_ca_verifies(self):
        client = EnrolmentClient(self.url, trust=self.trust(self.ca))
        self.assertEqual(client.status("secret").station_id, STATION)

    def test_the_wrong_ca_is_refused_and_not_retried_weaker(self):
        client = EnrolmentClient(self.url, trust=self.trust(self.other_ca))
        with self.assertRaises(Exception) as caught:
            client.status("secret")
        self.assertIn("will not accept", str(caught.exception))
        # Not retryable: no amount of waiting turns the wrong CA into the right
        # one, and a retry loop would hide the fault behind "link down".
        self.assertFalse(caught.exception.retryable)

    def test_a_self_signed_platform_is_rejected_by_the_system_bundle(self):
        # The API's default. A public certificate for a real domain verifies;
        # this test server's private one must not, or the default would be
        # accepting anything.
        client = EnrolmentClient(self.url, trust=tls.resolve_api())
        with self.assertRaises(Exception) as caught:
            client.status("secret")
        self.assertIn("will not accept", str(caught.exception))

    def test_pinning_asked_for_and_unusable_never_becomes_the_system_store(self):
        stranded = tls.Trust(mode=tls.TRUST_PINNED, purpose="api")
        client = EnrolmentClient(self.url, trust=stranded)
        with self.assertRaises(Exception) as caught:
            client.status("secret")
        self.assertIn("GSU_API_CA_FILE", str(caught.exception))


@unittest.skipUnless(HAS_OPENSSL, "needs openssl to make a certificate")
class AgentRefusalTests(unittest.TestCase):
    """A refused uplink must stop publishing and nothing else."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.home = Path(self._dir.name)
        self.ca = make_ca(self.home, "ca")

    def tearDown(self):
        self._dir.cleanup()

    def enrol(self, agent) -> None:
        from gsu.credentials import Broker, Credential, Enrolment, Site

        now = datetime.now(UTC)
        agent._attach(Enrolment(
            station_id=STATION,
            credential=Credential("bearer", "secret", now + timedelta(days=90),
                                  now + timedelta(days=45)),
            broker=Broker(
                url="redis://127.0.0.1:6399/0",
                ca_pem=self.ca.read_text(),
            ),
            site=Site("Test", "UTC", -43.5, 172.6),
            config_version=1, enrolled_at=now,
        ))

    def test_a_pinned_station_refuses_a_plaintext_broker_and_keeps_working(self):
        agent = Agent(AgentConfig(home=self.home, setup_enabled=False,
                                  single_instance=False, demo=True))
        self.enrol(agent)
        try:
            self.assertIsNone(agent.transport, "it must not have connected")
            conditions = {c["id"] for c in agent.health.to_list()}
            self.assertIn("uplink.refused", conditions)
            # The point of refusing rather than exiting: the box on the hillside
            # carries on sensing, recording and alerting locally.
            agent.step(1.0, weather_due=True, health_due=True)
            self.assertFalse(agent.security()["publishing"])
            self.assertTrue(agent.snapshot()["devices"])
        finally:
            agent.shutdown()

    def test_the_ca_from_enrolment_is_persisted_0600_and_pinned_next_boot(self):
        agent = Agent(AgentConfig(home=self.home, setup_enabled=False,
                                  single_instance=False, demo=True))
        self.enrol(agent)
        agent.shutdown()
        # Named for the broker, because that is whose root it is.
        stored = self.home / "broker-ca.pem"
        self.assertTrue(stored.exists())
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)

        restarted = Agent(AgentConfig(home=self.home, setup_enabled=False,
                                      single_instance=False, demo=True))
        try:
            self.assertTrue(restarted.trust.pinned)
            self.assertEqual(restarted.trust.source, "enrolment")
        finally:
            restarted.shutdown()

    def test_dropping_a_pinned_ca_for_public_trust_is_recorded_not_only_logged(self):
        # A box pinned yesterday, moved to a public-certificate broker today: the
        # pin is dropped (correctly — a stale pin would refuse every connection),
        # but that is a real reduction in the broker's trust and the console now
        # shows the result as a plain green "public certificate" row. So the
        # transition itself must leave a trace an operator can find — an event,
        # the same as a CA rotation gets, not just a log line.
        from gsu.credentials import Broker, Credential, Enrolment, Site
        from gsu import tls

        agent = Agent(AgentConfig(home=self.home, setup_enabled=False,
                                  single_instance=False, demo=True))
        try:
            self.enrol(agent)                               # pins self.ca
            self.assertTrue(agent.trust.pinned)
            now = datetime.now(UTC)
            agent._attach(Enrolment(
                station_id=STATION,
                credential=Credential("bearer", "secret", now + timedelta(days=90),
                                      now + timedelta(days=45)),
                broker=Broker(url="rediss://broker.example:443/0",
                              ca_mode=tls.TRUST_SYSTEM),
                site=Site("Test", "UTC", -43.5, 172.6),
                config_version=1, enrolled_at=now,
            ))
            self.assertEqual(agent.trust.mode, tls.TRUST_SYSTEM)
            self.assertFalse((self.home / "broker-ca.pem").exists())  # pin cleared
            kinds = {e.kind for e in agent.store.recent_events()}
            self.assertIn("tls.ca_dropped", kinds)
        finally:
            agent.shutdown()


if __name__ == "__main__":
    unittest.main()
