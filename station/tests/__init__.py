"""Tests for the station agent.

    station/.venv/bin/python -m unittest discover -s tests -t . -q

Everything here runs offline: no broker, no platform, no hardware. The schema
tests read `contract/schemas/` directly, so a contract change shows up here
before it shows up in conformance.
"""

import logging

# The agent logs health conditions and absent devices at WARNING, which is right
# in the field and noise in a test run. Failures still print.
#
# Quietened by raising the level on the agent's own loggers rather than with
# `logging.disable`, which is process-wide and outranks everything: it also
# silences the records `assertLogs` is waiting for, so a test that a warning
# *is* emitted can never pass. That is not a quiet test run, it is a test that
# cannot fail — and the H.264 parameter-set fallback is exactly the kind of
# degradation whose whole contract is that it says so out loud.
logging.getLogger("gsu").setLevel(logging.ERROR)
