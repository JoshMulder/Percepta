"""Percepta ground station unit — the software on the onboard computer.

This package replaces `server/app/backend/scripts/simulate_station.py`. It
speaks the contract in `contract/` across the real boundary: it publishes
telemetry and audio on `gsu/{station_id}/…` and subscribes to
`cmd/gsu/{station_id}`, and nothing downstream can tell it from hardware.

Layout, and why it is cut this way:

    transport/   the only code that knows the broker is Redis. Production is
                 MQTT over TLS and the contract requires that difference to be
                 confined to one place; this is that place.
    sensors/     interfaces first, simulated implementations behind them. No
                 real hardware is attached to this machine and nothing here
                 pretends otherwise.
    radio/       the receiver, and the squelch/noise-floor logic that
                 `contract/README.md` rule 3 makes station-side correctness.
    enrolment.py how the box gets and keeps its identity.
    agent.py     the loop that ties it together.

Everything that must keep working with no link at all — sensing, recording,
local alerting — lives above the transport and does not ask whether it is
connected.
"""

AGENT_VERSION = "0.1.0"

__all__ = ["AGENT_VERSION"]
