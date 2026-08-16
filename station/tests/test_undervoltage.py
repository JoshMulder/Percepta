"""The one reading on this station that predicts its own death.

On 2026-08-15 the Kennels Road Pi 5 logged `hwmon hwmon3: Undervoltage detected!`
and stopped executing in the same second — the SoC lost its rail while the PHY
stayed up, so the board sat there with a link light until somebody power-cycled
it on site. The kernel knew. Nothing read the file, so nobody was told.

Two properties are pinned here, and both were failure modes rather than
hypotheticals:

  1. The device is found BY NAME. The kernel message names `hwmon3`, which is
     exactly what must not be hard-coded — hwmon numbering follows probe order
     and moves between boots, so an index that is right today reads the fan
     tomorrow and returns a plausible zero for ever.
  2. The condition LATCHES. The alarm is instantaneous, and the event it reports
     lasts less than a telemetry interval. A condition that cleared itself would
     be raised and gone between two frames and would reach nobody — which is
     precisely what happened three times.
"""

from __future__ import annotations

import gsu.system as system


def _hwmon(tmp_path, devices: dict[str, dict[str, str]]):
    """Build a fake /sys/class/hwmon. `devices` maps dir name -> files."""
    root = tmp_path / "hwmon"
    root.mkdir()
    for name, files in devices.items():
        d = root / name
        d.mkdir()
        for filename, content in files.items():
            (d / filename).write_text(content)
    return root


def test_reads_the_alarm_from_the_device_named_rpi_volt(tmp_path, monkeypatch):
    root = _hwmon(tmp_path, {
        "hwmon0": {"name": "cpu_thermal\n"},
        "hwmon1": {"name": "pwmfan\n"},
        "hwmon3": {"name": "rpi_volt\n", "in0_lcrit_alarm": "1\n"},
    })
    monkeypatch.setattr(system, "Path", _rooted(root))
    assert system.undervoltage_now() is True


def test_a_quiet_board_reads_false_not_none(tmp_path, monkeypatch):
    # False and None are different answers: False is "asked, all well", None is
    # "no such sensor". A caller that conflated them would either alarm on every
    # dev box or stay silent on a real one.
    root = _hwmon(tmp_path, {
        "hwmon3": {"name": "rpi_volt\n", "in0_lcrit_alarm": "0\n"},
    })
    monkeypatch.setattr(system, "Path", _rooted(root))
    assert system.undervoltage_now() is False


def test_the_device_is_found_by_name_not_by_index(tmp_path, monkeypatch):
    """The regression this test exists for.

    Here `rpi_volt` is hwmon2 and hwmon3 is the FAN. Code that trusted the
    kernel message's "hwmon3" would read the fan's directory, find no
    `in0_lcrit_alarm`, and report a healthy board for ever.
    """
    root = _hwmon(tmp_path, {
        "hwmon0": {"name": "cpu_thermal\n"},
        "hwmon2": {"name": "rpi_volt\n", "in0_lcrit_alarm": "1\n"},
        "hwmon3": {"name": "pwmfan\n", "fan1_input": "9404\n"},
    })
    monkeypatch.setattr(system, "Path", _rooted(root))
    assert system.undervoltage_now() is True


def test_no_rpi_volt_device_is_unknown_rather_than_healthy(tmp_path, monkeypatch):
    # A dev box or a non-Pi host. "Unknown" must not render as "fine", or the
    # absence of a sensor becomes a clean bill of health.
    root = _hwmon(tmp_path, {"hwmon0": {"name": "coretemp\n"}})
    monkeypatch.setattr(system, "Path", _rooted(root))
    assert system.undervoltage_now() is None


def test_no_hwmon_at_all_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "Path", _rooted(tmp_path / "absent"))
    assert system.undervoltage_now() is None


def test_a_device_with_a_name_but_no_alarm_file_does_not_crash(tmp_path, monkeypatch):
    # A kernel that renames or drops the attribute must degrade to "unknown",
    # not take the health tick down with it.
    root = _hwmon(tmp_path, {"hwmon0": {"name": "rpi_volt\n"}})
    monkeypatch.setattr(system, "Path", _rooted(root))
    assert system.undervoltage_now() is None


def _rooted(root):
    """A Path stand-in that redirects /sys/class/hwmon at the fixture."""
    import pathlib

    real = pathlib.Path

    def factory(arg):
        if str(arg) == "/sys/class/hwmon":
            return real(root)
        return real(arg)

    return factory


# ---------------------------------------------------------- the thread cap

def test_a_thread_cap_reaches_whispers_argv(tmp_path):
    """The dial works, whatever the default happens to be.

    A cap of 2 shipped in v0.4.2 and was reverted the same day — it cost
    throughput without fixing the board — so the DEFAULT is now whisper's own
    (every core). The mechanism stays, because it is the right lever if power
    ever has to win over throughput, and this asserts it still reaches the
    command line: a constant being right is worth nothing if it never gets
    there.
    """
    from gsu.radio import transcribe

    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        raise RuntimeError("not actually running whisper")

    import subprocess

    real = subprocess.run
    subprocess.run = fake_run
    try:
        transcribe.whisper_transcribe(
            "whisper-cli", "model.bin", tmp_path / "over.wav", threads=2
        )
    except Exception:
        pass
    finally:
        subprocess.run = real

    command = seen.get("command") or []
    assert "-t" in command, "the thread cap never reached whisper's argv"
    assert command[command.index("-t") + 1] == "2"


def test_zero_threads_restores_whispers_own_default(tmp_path):
    # `-t 0` is not the same as omitting the flag: the binary would have to
    # decide what zero means. Omission is the honest way to say "your choice".
    from gsu.radio import transcribe

    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        raise RuntimeError("stop")

    import subprocess

    real = subprocess.run
    subprocess.run = fake_run
    try:
        transcribe.whisper_transcribe(
            "whisper-cli", "model.bin", tmp_path / "over.wav", threads=0
        )
    except Exception:
        pass
    finally:
        subprocess.run = real

    assert "-t" not in (seen.get("command") or [])
