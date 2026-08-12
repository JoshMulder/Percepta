"""The station's host-shell coordinator, and the helper bridge's gate.

The host shell is the biggest trust escalation in the station, so the station's
own half is deliberately tiny — it only writes the privileged helper a request —
and the two things that matter are tested here: that an opted-out box writes
nothing at all, and that the helper only acts on a request that is open, current
and complete.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time

import pytest

import gsu
from gsu.commands import build_handlers
from gsu.transport.host_shell import HostShellCoordinator, host_ingest_url


class _Cfg:
    def __init__(self, host_shell_url=None, platform_url="https://app.percepta.nz"):
        self.host_shell_url = host_shell_url
        self.platform_url = platform_url


def test_the_ingest_url_is_derived_from_the_platform_host() -> None:
    assert host_ingest_url(_Cfg()) == "wss://app.percepta.nz/host/ingest"
    assert (
        host_ingest_url(_Cfg(platform_url="http://localhost:8000"))
        == "ws://localhost:8000/host/ingest"
    )
    assert host_ingest_url(_Cfg(host_shell_url="wss://via/host")) == "wss://via/host"


def test_an_opted_out_station_writes_no_request(tmp_path) -> None:
    coordinator = HostShellCoordinator(
        tmp_path, "wss://x/host/ingest", "secret", enabled=False
    )
    assert coordinator.request_open(300).startswith("refused")
    # The safety property: no file, so the helper has nothing to act on.
    assert not (tmp_path / "hostshell.json").exists()


def test_an_opted_in_station_writes_a_bounded_request(tmp_path) -> None:
    coordinator = HostShellCoordinator(
        tmp_path, "wss://x/host/ingest", "secret", enabled=True
    )
    assert "120s" in coordinator.request_open(120)
    request = json.loads((tmp_path / "hostshell.json").read_text())
    assert request["open"] is True
    assert request["url"] == "wss://x/host/ingest"
    assert request["secret"] == "secret"
    assert request["deadline"] > time.time()
    # The file holds the station secret, so it is not world-readable.
    assert oct(os.stat(tmp_path / "hostshell.json").st_mode)[-3:] == "600"


def test_close_writes_a_closed_request(tmp_path) -> None:
    coordinator = HostShellCoordinator(tmp_path, "wss://x", "s", enabled=True)
    coordinator.request_open(120)
    coordinator.request_close()
    assert json.loads((tmp_path / "hostshell.json").read_text())["open"] is False


def test_host_handlers_are_registered_only_with_a_coordinator() -> None:
    assert "host.open" not in build_handlers(None, None, None)
    coordinator = HostShellCoordinator("/tmp/nowhere", None, None, enabled=False)
    handlers = build_handlers(None, None, None, host_shell=coordinator)
    assert "host.open" in handlers and "host.close" in handlers
    assert handlers["host.open"]({"lease_seconds": 10}).startswith("refused")


# --- the helper bridge's gate --------------------------------------------


def _load_bridge():
    """Import the helper's bridge, which lives outside the package.

    It does `import websocket` (the agent's WS client, copied verbatim into the
    helper image); here that resolves by putting gsu/media on the path, which is
    also a check that the client really is standalone and stdlib-only.
    """
    media_dir = os.path.join(os.path.dirname(gsu.__file__), "media")
    if media_dir not in sys.path:
        sys.path.insert(0, media_dir)
    path = os.path.join(
        os.path.dirname(__file__), "..", "deploy", "hostshell", "bridge.py"
    )
    spec = importlib.util.spec_from_file_location("hostshell_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bridge():
    return _load_bridge()


def test_a_session_is_wanted_only_when_open_current_and_complete(bridge) -> None:
    future = time.time() + 60
    assert bridge._wanted(
        {"open": True, "deadline": future, "url": "wss://x", "secret": "s"}
    )
    # Each of the four requirements, missing, closes the gate.
    assert not bridge._wanted({"open": False, "deadline": future, "url": "x", "secret": "s"})
    assert not bridge._wanted({"open": True, "deadline": 1, "url": "x", "secret": "s"})
    assert not bridge._wanted({"open": True, "deadline": future, "secret": "s"})
    assert not bridge._wanted({"open": True, "deadline": future, "url": "x"})
    assert not bridge._wanted(None)
