"""Branch coverage for the host updater shell script, `deploy/gsu-update.sh`.

The script has never run against real Docker (DECISIONS item 48, "not
verified"), and a happy-path real run would never exercise the paths that
matter most — a rejected signature, a pull that fails, a new image that does not
come up publishing, and the rollback that follows. So this drives it with stub
`docker`, `cosign` and `sleep` executables on PATH ahead of the real ones, each
behaving per scenario from the environment, and asserts what the script records
(status file, rejected-digests, whether the request was cleared) and its exit
code.

The stubs deliberately model the one hard case: a gate that passes for one image
ref and fails for another (a new image that will not publish, a previous one
that will), by reading the `GSU_IMAGE_REF` the script writes into `.env` before
each recreate. That is what lets "rolled back and publishing" be a tested fact
rather than a hope.

Runs bash; skipped where it or /bin/sleep is unavailable (Windows dev host —
these run under WSL/Linux, see the dev-machine note).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "gsu-update.sh"
DIGEST = "sha256:" + "a" * 64
OLD_REF = "reg.example/gsu@sha256:" + "0" * 64
TARGET = f"reg.example/gsu@{DIGEST}"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not Path("/bin/sleep").exists(),
    reason="needs bash and /bin/sleep (run under WSL/Linux)",
)

# Dispatches on the docker subcommand. `exec` reads the ref the script just
# wrote into .env and reports the agent 'publishing' (link true, a rising
# counter) only when it matches FAKE_GOOD_REF — unset means every ref publishes.
_DOCKER = r"""#!/usr/bin/env bash
sub="${1:-}"; shift || true
case "$sub" in
  inspect)
    [ "${1:-}" = "--format" ] && echo "${FAKE_RUNNING_IMAGE:-reg.example/gsu}"
    exit 0 ;;
  info) exit 0 ;;
  login) cat >/dev/null 2>&1; exit 0 ;;
  pull) [ -n "${FAKE_PULL_FAIL:-}" ] && exit 1; exit 0 ;;
  compose) exit 0 ;;
  exec)
    ref="$(sed -n 's/^GSU_IMAGE_REF=//p' "${GSU_ENV_FILE:-/dev/null}" 2>/dev/null | tail -1)"
    good="${FAKE_GOOD_REF:-__any__}"
    if [ "$good" = "__any__" ] || [ "$ref" = "$good" ]; then
      c="$(cat "${PUBCOUNT}" 2>/dev/null || echo 0)"; c=$((c + 1)); echo "$c" >"${PUBCOUNT}"
      printf '{"link": true, "published": %s}\n' "$c"
    else
      printf '{"link": false, "published": 0}\n'
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
"""

_COSIGN = "#!/usr/bin/env bash\n[ -n \"${FAKE_VERIFY_FAIL:-}\" ] && exit 1\nexit 0\n"
# Fast, but real wall-clock so the gate's SECONDS deadline still advances.
_SLEEP = "#!/usr/bin/env bash\nexec /bin/sleep 0.02\n"


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _run(tmp_path, *, arg=None, request=True, running=OLD_REF, rejected=None,
         gate_s="4", scenario=None):
    compose = tmp_path / "compose"
    compose.mkdir()
    (compose / "docker-compose.yml").write_text("services:\n  gsu: {}\n")
    env_file = compose / ".env"
    env_file.write_text("GSU_SITE_NAME=test\n")
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    if request:
        (handoff / "update-request.json").write_text(json.dumps(
            {"digest": DIGEST, "tag": "v1.2.3", "image": "reg.example/gsu"}))
    if rejected:
        (state / "rejected-digests").write_text(rejected + "\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "docker", _DOCKER)
    _exe(bindir / "cosign", _COSIGN)
    _exe(bindir / "sleep", _SLEEP)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env.update({
        "GSU_COMPOSE_DIR": str(compose),
        "GSU_ENV_FILE": str(env_file),
        "GSU_UPDATE_HANDOFF": str(handoff),
        "GSU_UPDATE_STATE": str(state),
        "GSU_IMAGE": "reg.example/gsu",
        "GSU_UPDATE_GATE_S": gate_s,
        "FAKE_RUNNING_IMAGE": running,
        "PUBCOUNT": str(tmp_path / "pubcount"),
    })
    env.update(scenario or {})

    cmd = ["bash", str(SCRIPT)] + ([arg] if arg else [])
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)

    status_path = handoff / "update-status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else None
    rejects_path = state / "rejected-digests"
    rejects = rejects_path.read_text() if rejects_path.exists() else ""
    return {
        "proc": proc, "status": status, "rejects": rejects,
        "request_left": (handoff / "update-request.json").exists(),
        "previous": (state / "previous-ref").exists(),
    }


def test_no_request_does_nothing(tmp_path):
    r = _run(tmp_path, request=False)
    assert r["proc"].returncode == 0
    assert "nothing to do" in r["proc"].stdout
    assert r["status"] is None


def test_happy_path_updates_and_clears_the_request(tmp_path):
    r = _run(tmp_path)  # pull ok, verify ok, gate passes for any ref
    assert r["proc"].returncode == 0, r["proc"].stderr + r["proc"].stdout
    assert r["status"]["last_result"] == "updated"
    assert r["status"]["last_version"] == "v1.2.3"
    assert not r["request_left"]


def test_a_bad_signature_is_refused_and_remembered(tmp_path):
    r = _run(tmp_path, scenario={"FAKE_VERIFY_FAIL": "1"})
    assert r["proc"].returncode == 1
    assert r["status"]["last_result"] == "signature_rejected"
    assert DIGEST in r["rejects"]
    assert r["request_left"]  # not cleared — it never ran


def test_a_failed_pull_leaves_the_container_untouched(tmp_path):
    r = _run(tmp_path, scenario={"FAKE_PULL_FAIL": "1"})
    assert r["proc"].returncode == 0
    assert "pull failed" in r["proc"].stdout
    assert r["status"] is None
    assert r["request_left"]


def test_a_new_image_that_will_not_publish_rolls_back(tmp_path):
    # New ref never publishes; the previous ref (what it rolls back to) does.
    r = _run(tmp_path, gate_s="1", scenario={"FAKE_GOOD_REF": OLD_REF})
    assert r["proc"].returncode == 1  # rolled back
    assert r["status"]["last_result"] == "rolled_back"
    assert DIGEST in r["rejects"]


def test_a_rollback_that_also_fails_is_reported(tmp_path):
    # Nothing publishes, so even the rollback fails to gate.
    r = _run(tmp_path, gate_s="1",
             scenario={"FAKE_GOOD_REF": "reg.example/gsu@sha256:nomatch"})
    assert r["proc"].returncode == 2
    assert r["status"]["last_result"] == "rollback_failed"


def test_already_on_the_target_digest_is_a_noop(tmp_path):
    # Also guards the set -e fix: a hand-run on a no-op must exit 0, not 1.
    r = _run(tmp_path, running=TARGET)
    assert r["proc"].returncode == 0
    assert r["status"] is None
    assert not r["previous"]  # no side effect


def test_a_previously_rejected_digest_is_skipped(tmp_path):
    r = _run(tmp_path, rejected=DIGEST)
    assert r["proc"].returncode == 0
    assert "rejected before" in r["proc"].stdout
    assert r["status"] is None


def test_check_is_side_effect_free(tmp_path):
    r = _run(tmp_path, arg="--check")
    assert r["proc"].returncode == 0
    assert "would update" in r["proc"].stdout
    assert "nothing was changed" in r["proc"].stdout
    assert r["status"] is None
    assert r["request_left"]
    assert not r["previous"]
