"""tests/container/stand_ins.py: the stand-ins for systemctl, loginctl and rcman's podman, as
rcman's own code reads what they answer: the Podman runtime's limits (backend/rcman/runtime.py)
through ``PodmanConfig.systemctl``, and every call recorded."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rcman import limits
from rcman import runtime as rt
from rcman.limits import Caps, Limits

STAND_INS = Path(__file__).resolve().parent / "container" / "stand_ins.py"


@pytest.fixture
def systemctl(tmp_path) -> Path:
    """The stand-in as a program called systemctl, its state in a folder of the test's."""
    program = tmp_path / "systemctl"
    program.write_text(
        f"#!/bin/sh\nRCMAN_STAND_IN_DIR={tmp_path / 'state'} exec {sys.executable} "
        f'{STAND_INS} systemctl "$@"\n'
    )
    program.chmod(0o755)
    return program


def runtime_with(systemctl: Path) -> rt.PodmanRuntime:
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/nowhere.sock"), image="i", tools_dir=Path("/t"),
            claude_store=Path("/c"), systemctl=str(systemctl),
        )
    )  # fmt: skip


def calls(tmp_path) -> list[dict]:
    lines = (tmp_path / "state" / "calls").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_a_slice_nothing_set_has_no_cap_and_uses_nothing(systemctl):
    runtime = runtime_with(systemctl)
    assert runtime.caps(7, 12) == Caps()
    assert runtime.usage(7, 12) == limits.Usage()


def test_limits_set_are_the_slices_caps_until_reverted(systemctl, tmp_path):
    runtime = runtime_with(systemctl)
    runtime.apply_limits(7, 12, Limits(memory_mib=2048, cpu_percent=150, pids=3000))
    assert runtime.caps(7, 12) == Caps(2048 * limits.MEBIBYTE, 3000)
    assert runtime.caps(7, None) == Caps()  # the person's slice is another
    runtime.drop_limits(7, 12)
    assert runtime.caps(7, 12) == Caps()
    recorded = calls(tmp_path)
    assert [c["args"][1] for c in recorded if c["args"][:1] == ["--user"]][:2] == [
        "set-property",
        "show",
    ]
    assert all(c["program"] == "systemctl" and c["uid"] == os.getuid() for c in recorded)


def test_a_reboot_forgets_what_was_set(systemctl, tmp_path):
    runtime = runtime_with(systemctl)
    runtime.apply_limits(7, None, Limits(pids=500))
    assert runtime.caps(7, None) == Caps(None, 500)  # positive control
    env = {**os.environ, "RCMAN_STAND_IN_DIR": str(tmp_path / "state")}
    assert subprocess.run([sys.executable, str(STAND_INS), "reboot"], env=env).returncode == 0
    assert runtime.caps(7, None) == Caps()


def test_a_cpu_quota_is_shown_as_systemd_shows_it(systemctl, tmp_path):
    runtime_with(systemctl).apply_limits(7, None, Limits(cpu_percent=150))
    held = json.loads((tmp_path / "state" / "slices.json").read_text())
    assert held[limits.person_slice(7)]["CPUQuotaPerSecUSec"] == "1.5s"


# --- a path unit ------------------------------------------------------------------------------

UPGRADE_PATH_UNIT = "[Path]\nPathModified=/nowhere/request\nUnit=planted.service\n"


@pytest.fixture
def units(tmp_path) -> Path:
    """A folder of units the stand-in reads in place of /etc/systemd/system: a path unit, and
    the service it starts, which marks it ran by writing a file."""
    folder = tmp_path / "units"
    folder.mkdir()
    (folder / "planted.path").write_text(UPGRADE_PATH_UNIT)
    ran = tmp_path / "service-ran"
    (folder / "planted.service").write_text(
        f"[Service]\nType=oneshot\nUser={__import__('pwd').getpwuid(os.getuid()).pw_name}\n"
        f"ExecStart=/usr/bin/touch {ran}\n"
    )
    return folder


def stand_in(tmp_path, units, *args) -> subprocess.CompletedProcess:
    env = {**os.environ, "RCMAN_STAND_IN_DIR": str(tmp_path / "state"),
           "RCMAN_STAND_IN_UNITS": str(units)}  # fmt: skip
    return subprocess.run(
        [sys.executable, str(STAND_INS), "systemctl", *args], env=env, capture_output=True,
        text=True, timeout=60,
    )  # fmt: skip


@pytest.mark.parametrize("verb", [["restart"], ["start"], ["enable", "--now"]])
def test_a_path_unit_is_started_as_systemd_starts_one_and_runs_no_service(tmp_path, units, verb):
    """systemd starts watching; the service runs only when the path changes, which nothing
    here does."""
    assert stand_in(tmp_path, units, "is-active", "planted.path").returncode == 3  # not yet
    done = stand_in(tmp_path, units, *verb, "planted.path")
    assert done.returncode == 0, done.stderr
    assert stand_in(tmp_path, units, "is-active", "planted.path").returncode == 0
    assert stand_in(tmp_path, units, "is-active", "planted.service").returncode == 3
    assert not (tmp_path / "service-ran").exists()
    assert [c["args"] for c in calls(tmp_path) if "is-active" not in c["args"]] == [
        [*verb, "planted.path"]
    ]


def test_enable_without_now_starts_nothing(tmp_path, units):
    assert stand_in(tmp_path, units, "enable", "planted.path").returncode == 0
    assert stand_in(tmp_path, units, "is-enabled", "planted.path").returncode == 0
    assert stand_in(tmp_path, units, "is-active", "planted.path").returncode == 3


def test_a_show_of_several_slices_is_read_by_the_monitor_slice_by_slice(systemctl):
    """The monitor's one show of many slices (monitor.read_units): a block for each, with its
    Id, parted by blank lines, and CPU time that grows between two shows."""
    from rcman import monitor

    runtime = runtime_with(systemctl)
    units = [limits.person_slice(7), limits.workspace_slice(7, 12)]
    first = monitor.read_units(runtime.show_slices(units))
    second = monitor.read_units(runtime.show_slices(units))
    assert set(first) == set(units)
    for unit in units:
        assert first[unit] is not None and first[unit].memory_bytes is not None
        assert second[unit].cpu_nsec > first[unit].cpu_nsec
