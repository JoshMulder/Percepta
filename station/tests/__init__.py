"""Tests for the station agent.

    station/.venv/bin/python -m unittest discover -s tests -t . -q

Everything here runs offline: no broker, no platform, no hardware. The schema
tests read `contract/schemas/` directly, so a contract change shows up here
before it shows up in conformance.
"""

import logging

# The agent logs health conditions and absent devices at WARNING, which is right
# in the field and noise in a test run. Failures still print.
logging.disable(logging.WARNING)
