"""Entry point.

    python -m gsu run                 # the station (this is what runs in the field)
    python -m gsu preflight           # everything that has to be true before it works
    python -m gsu enrol --token …     # claim a code without the local console
    python -m gsu devices             # what is fitted, and what was actually found
    python -m gsu bench               # what a tick costs — run this on the target
    python -m gsu status              # what the platform thinks of us
    python -m gsu whoami              # what this box thinks it is, offline

`run` is the only one a technician ever causes to happen; the rest are for
whoever is debugging a box, and `preflight`, `devices` and `bench` are the three
that work with no link at all.
"""

from __future__ import annotations

import argparse
import logging
import platform
import socket
import ssl
import sys
import time

from . import AGENT_VERSION, clock, tls
from .agent import Agent
from .config import AgentConfig
from .devices.serialio import list_ports


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )


def _bench(agent) -> int:
    """Measure what one tick of this agent costs, here.

    Reported as CPU milliseconds per tick and as a percentage of one core at
    1 Hz, so the number means the same thing on a workstation and on a Pi. It
    measures *this software only* — dump1090, an airband demodulator and the
    camera encoder are separate processes and have to be measured separately on
    the target, which is the whole point of shipping this as a command rather
    than quoting a figure from a different machine.
    """
    import platform

    for _ in range(3):
        agent.step(1.0)

    def measure(label: str, fn, runs: int) -> None:
        start_cpu = time.process_time()
        start_wall = time.monotonic()
        for _ in range(runs):
            fn()
        cpu = (time.process_time() - start_cpu) / runs * 1000
        wall = (time.monotonic() - start_wall) / runs * 1000
        print(f"  {label:36} {cpu:7.2f} ms CPU  {wall:7.2f} ms wall  "
              f"{cpu / 10:5.1f}% of one core at 1 Hz")

    print(f"\n{platform.machine()} / {platform.processor() or 'unknown'} / "
          f"Python {platform.python_version()}\n")
    front_end = agent.radio.front_end if agent.radio is not None else None
    set_traffic = getattr(front_end, "set_traffic", None)
    if set_traffic:
        set_traffic("off", transmitting=False)
    measure("full tick, squelch closed", lambda: agent.step(1.0), 100)
    if agent.radio is not None:
        measure("radio tick, squelch closed", lambda: agent.radio.tick(1.0), 100)
        if set_traffic:
            set_traffic("busy", transmitting=True)
            measure("radio tick, squelch open (1 s audio)", lambda: agent.radio.tick(1.0), 30)
    if agent.adsb is not None:
        measure("ADS-B poll (MAVLink decode)", lambda: agent.adsb.poll(1.0), 100)
    if agent.weather is not None:
        measure("weather read", lambda: agent.weather.read(5.0), 100)
    if agent.power is not None:
        measure("power read", lambda: agent.power.read(1.0), 100)
    print(
        "\nThe simulated airband front end synthesises audio in Python, which no\n"
        "real station does — on hardware that work is the SDR pipeline's, in its\n"
        "own process. Measure that separately.\n"
    )
    agent.shutdown()
    return 0


def _handshake(url: str, trust: tls.Trust, timeout: float = 8.0) -> tuple[bool, str]:
    """Open a TLS connection and verify the certificate. Nothing else.

    No HTTP, no RESP, no credentials — this answers only "would this station
    accept that server's certificate", which is the question that is hardest to
    answer from a log line and the one most likely to be wrong on the day. It
    sends no token and no secret, so it is safe to run before enrolment.
    """
    host = tls.host_of(url)
    tail = url.split("://", 1)[-1].split("/", 1)[0]
    port_text = tail.rsplit(":", 1)[-1] if ":" in tail and not tail.endswith("]") else ""
    default_port = 443 if url.startswith("https") else 6380
    try:
        port = int(port_text) if port_text.isdigit() else default_port
    except ValueError:
        port = default_port
    try:
        context = trust.context()
    except tls.Refusal as exc:
        return False, str(exc)
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secure:
                cert = secure.getpeercert() or {}
        subject = dict(
            item for entry in cert.get("subject", ()) for item in entry
        ).get("commonName", "?")
        return True, (
            f"verified {host}:{port}, certificate CN={subject}, "
            f"expires {cert.get('notAfter', '?')}"
        )
    except ssl.SSLCertVerificationError as exc:
        return False, f"certificate rejected: {exc.verify_message or exc}"
    except (OSError, ssl.SSLError) as exc:
        return False, f"could not reach {host}:{port}: {exc}"


def _preflight(agent, config: AgentConfig, probe: bool) -> int:
    """Everything that has to be true before this box can work, checked here.

    Written to be run over SSH on a box that is not behaving, and to be the
    first step in DEPLOYMENT.md's commissioning list. It says PASS, WARN or FAIL
    per line and returns non-zero if anything failed, so it can also be the
    thing a person runs before driving away.
    """
    failures = warnings = 0

    def line(state: str, label: str, detail: str = "") -> None:
        nonlocal failures, warnings
        if state == "FAIL":
            failures += 1
        elif state == "WARN":
            warnings += 1
        print(f"  {state:4}  {label}")
        if detail:
            for part in str(detail).split("\n"):
                print(f"          {part}")

    print(f"\n{platform.machine()} / {platform.system()} "
          f"{platform.release()} / Python {platform.python_version()}")
    print(f"agent {AGENT_VERSION}, state in {config.home}\n")

    # Read the stored identity now rather than attaching to it: preflight must
    # be safe to run on a box that is already running the service, so it opens
    # no broker connection and starts no renewer.
    stored = None
    stored_error = None
    try:
        stored = agent.credentials.load()
    except ValueError as exc:
        stored_error = str(exc)
    broker_url = config.broker_url or (stored.broker.url if stored else None)

    # --- the clock, which is what strands a remote site (enrolment.md §6) ---
    print("Clock")
    reason = clock.implausible_reason()
    if reason:
        line("FAIL", "plausible", reason + " Enrolment will be refused until this is fixed.")
    else:
        line("PASS", "plausible", clock.now().isoformat())
    state = clock.discipline(force=True)
    if state.synchronised is True:
        line("PASS", f"disciplined by {state.source}", state.detail)
    elif state.synchronised is False:
        line("FAIL", "not disciplined", state.detail)
    else:
        line("WARN", "cannot tell what keeps this clock", state.detail)
    if state.rtc_present:
        line("PASS", "hardware RTC present", "time survives a power cut")
    else:
        line("WARN", "no hardware RTC",
             "This box boots with no idea of the time until NTP answers. "
             "See HARDWARE.md §4 — an RTC module is a few pounds.")

    # --- trust, before anything is sent anywhere ---
    print("\nTrust")
    trust = agent.trust
    if trust.mode == tls.TRUST_SYSTEM:
        line("WARN", "system CA bundle", "Not pinned to the platform's CA.")
    elif trust.path is None:
        line("WARN", "no CA pinned yet",
             "Normal before the first enrolment if the platform is plaintext. "
             "For an https:// platform, install the CA and set GSU_CA_FILE.")
    else:
        line("PASS", f"CA pinned from {trust.source}",
             f"{trust.path}\nSHA-256 {trust.fingerprint}\n"
             "Compare with: openssl x509 -in ca.crt -noout -fingerprint -sha256")

    for label, url in (("platform API", config.platform_url),
                       ("broker", broker_url)):
        if not url:
            line("WARN", f"{label}: no address yet",
                 "not enrolled, and no GSU_BROKER_URL set")
            continue
        try:
            trust.check(url, label)
        except tls.Refusal as exc:
            line("FAIL", f"{label}: refused", str(exc))
            continue
        if tls.is_tls(url):
            if probe:
                ok, detail = _handshake(url, trust)
                line("PASS" if ok else "FAIL", f"{label}: {url}", detail)
            else:
                line("PASS", f"{label}: {url}", "TLS, not probed (use --probe)")
        else:
            line("WARN", f"{label}: {url}", "plaintext — development only")

    # --- identity ---
    print("\nIdentity")
    enrolment = stored
    if stored_error:
        line("FAIL", "stored credential unreadable", stored_error)
    if enrolment is None:
        line("WARN", "not enrolled", "Enter a code on the setup page.")
    else:
        remaining = enrolment.credential.seconds_remaining() / 3600
        line("PASS" if remaining > 0 else "FAIL",
             f"enrolled as {enrolment.site.name}",
             f"{enrolment.station_id}\ncredential expires in {remaining:.1f} h "
             f"({enrolment.credential.expires_at.isoformat()})")
    for path in (config.credential_path, config.ca_path):
        if not path.exists():
            continue
        mode = path.stat().st_mode & 0o777
        line("PASS" if mode == 0o600 else "FAIL", f"{path.name} permissions",
             f"{oct(mode)}" + ("" if mode == 0o600 else " — should be 0600"))

    # --- what is plugged in ---
    print("\nSerial ports present")
    ports = list_ports()
    if not ports:
        line("WARN", "none", "Neither USB-UART is enumerating. Check leads, then dmesg.")
    for port in ports:
        line("PASS" if port.stable else "WARN", port.path,
             port.target if port.stable else
             "unstable name — use the /dev/serial/by-id/… one instead")

    print("\nDevices")
    for _ in range(3):
        agent.step(1.0)
        time.sleep(0.2)
    for report in agent.inventory.report():
        if not report.configured:
            line("WARN", f"{report.slot}: nothing fitted")
        elif report.status == "present":
            line("PASS", f"{report.slot}: {report.label}", report.detail)
        else:
            line("FAIL", f"{report.slot}: {report.label}", report.detail)
    for conflict in agent.inventory.conflicts():
        line("FAIL", "device conflict", conflict)

    print(f"\n{failures} failed, {warnings} warned.")
    if failures:
        print("A FAIL means this station will not do that thing. Fix, re-run.\n")
    agent.shutdown()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gsu", description=__doc__)
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "preflight", "enrol", "status", "whoami",
                                 "devices", "bench"])
    parser.add_argument("--token", help="enrolment code, as issued (XXXX-XXXX-XXXX)")
    parser.add_argument("--probe", action="store_true",
                        help="preflight: open a TLS connection to the platform and "
                             "the broker and verify their certificates. Sends no "
                             "credential and no token.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _logging(args.verbose)

    config = AgentConfig.from_env()
    agent = Agent(config)

    if args.command == "run":
        return agent.run()

    if args.command == "preflight":
        return _preflight(agent, config, args.probe)

    if args.command == "devices":
        # Give each device a moment to say something before asking whether it
        # is talking: a driver that has been constructed and never read from is
        # indistinguishable from one that is silent, and reporting it as absent
        # would be the exact mistake this command exists to expose.
        for _ in range(3):
            agent.step(1.0)
            time.sleep(0.3)
        # Intent against fact, which is the whole point of the inventory.
        for report in agent.inventory.report():
            print(
                f"{report.slot:8} {report.status:18} {report.label}\n"
                f"         {'found: ' + report.detail if report.detail else ''}"
            )
            if report.absent:
                print(f"         no source for: {', '.join(report.absent)}")
        for conflict in agent.inventory.conflicts():
            print(f"CONFLICT {conflict}")
        unsourced = agent.inventory.unsourced_streams()
        if unsourced:
            print(f"\nNot published at all (no source): {', '.join(sorted(unsourced))}")
        agent.shutdown()
        return 0

    if args.command == "bench":
        # Run this on the target hardware. The station's own cost is only part
        # of the load question, but it is the part this code is responsible for.
        return _bench(agent)

    if args.command == "enrol":
        token = args.token or config.enrol_token
        if not token:
            print("Give a code with --token or GSU_ENROL_TOKEN.", file=sys.stderr)
            return 2
        try:
            enrolment = agent.enrol(token)
        except Exception as exc:  # noqa: BLE001 - this message is the product
            print(f"Enrolment failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"Enrolled as {enrolment.site.name} ({enrolment.station_id}).\n"
            f"  broker      {enrolment.broker.url} as {enrolment.broker.username}\n"
            f"  telemetry   {enrolment.broker.telemetry_topic}\n"
            f"  commands    {enrolment.broker.command_topic}\n"
            f"  expires     {enrolment.credential.expires_at.isoformat()}\n"
            f"  renew after {enrolment.credential.renew_after.isoformat()}"
        )
        return 0

    enrolment = agent.credentials.load()
    if enrolment is None:
        print("This box is not enrolled.", file=sys.stderr)
        return 1

    if args.command == "whoami":
        print(
            f"{enrolment.site.name} ({enrolment.station_id})\n"
            f"  agent       {AGENT_VERSION}\n"
            f"  broker      {config.broker_url or enrolment.broker.url} "
            f"as {enrolment.broker.username}\n"
            f"  credential  expires {enrolment.credential.expires_at.isoformat()}, "
            f"renew after {enrolment.credential.renew_after.isoformat()}"
        )
        return 0

    try:
        standing = agent.client.status(enrolment.credential.secret)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach the platform: {exc}", file=sys.stderr)
        return 1
    print(
        f"{standing.name} ({standing.station_id})\n"
        f"  config version {standing.config_version}\n"
        f"  credential expires {standing.credential_expires_at}\n"
        f"  renew now: {standing.renew_now}\n"
        f"  platform clock {standing.server_time} (reference only)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
