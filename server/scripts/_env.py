"""The platform base URL for the ops/debug scripts in this directory.

Resolution order, so nothing is tied to a fixed address:
  1. the PERCEPTA_URL environment variable (override for one run), then
  2. PERCEPTA_URL in ../.env (the deployment's own value; see .env.example), then
  3. a hostname default — never an IP.
"""

from __future__ import annotations

import os
import pathlib

_DEFAULT = "https://percepta.aeronavics.com"


def platform_url() -> str:
    """The platform base URL, e.g. https://percepta.aeronavics.com (no trailing /)."""
    value = os.environ.get("PERCEPTA_URL")
    if not value:
        dotenv = pathlib.Path(__file__).resolve().parents[1] / ".env"
        try:
            for raw in dotenv.read_text().splitlines():
                line = raw.strip()
                if line.startswith("PERCEPTA_URL=") and not line.startswith("#"):
                    value = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass
    return (value or _DEFAULT).rstrip("/")


def platform_host() -> str:
    """Host[:port] only, for callers that build their own scheme."""
    return platform_url().split("://", 1)[-1]
