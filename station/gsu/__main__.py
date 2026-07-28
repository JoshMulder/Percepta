"""Entry point.

    python -m gsu run                 # the station (this is what runs in the field)
    python -m gsu enrol --token …     # claim a code without the local console
    python -m gsu devices             # what is fitted, and what was actually found
    python -m gsu bench               # what a tick costs — run this on the target
    python -m gsu status              # what the platform thinks of us
    python -m gsu whoami              # what this box thinks it is, offline

`run` is the only one a technician ever causes to happen; the rest are for
whoever is debugging a box, and `devices` and `bench` are the two that work with
no link at all.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import AGENT_VERSION
from .agent import Agent
from .config import AgentConfig


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gsu", description=__doc__)
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "enrol", "status", "whoami", "devices", "bench"])
    parser.add_argument("--token", help="enrolment code, as issued (XXXX-XXXX-XXXX)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _logging(args.verbose)

    config = AgentConfig.from_env()
    agent = Agent(config)

    if args.command == "run":
        return agent.run()

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
