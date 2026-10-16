"""After the whole podman host has restarted, as a machine reboots, rcman brings back its
workspaces and their servers.

``tests/test_container_suite.py`` runs this module in the podman host that
test_install_in_container.py installed rcman in and left a workspace's server running
in, once it has restarted the host: its tmpfs runtime folders are empty, every
container stopped, and systemd booted again. Anywhere else it skips.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1"
    or os.geteuid() != 0
    or not Path("/var/tmp/rcman-left-running.json").exists(),
    reason="runs only as root inside the podman host, after the installation suite",
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from installed import (  # noqa: E402
    MEBIBYTE,
    cgroup_of,
    claude_processes,
    drop_in_times,
    healthy,
    limit_files,
    podman,
    slice_dir,
    until,
    workspace_of,
)

LEFT_RUNNING = Path("/var/tmp/rcman-left-running.json")


@pytest.fixture(scope="module")
def left():
    healthy()
    return json.loads(LEFT_RUNNING.read_text())


def test_the_host_really_restarted(left):
    """Positive control: what a restart clears is gone, so what follows is rcman's doing."""
    assert not Path(left["marker"]).exists()  # /run is a tmpfs, emptied by the restart


def test_the_workspace_runs_again(left):
    def running():
        found = podman("container", "inspect", f"rcman-ws-{left['workspace']}", check=False)
        return found.returncode == 0 and json.loads(found.stdout)[0]["State"]["Running"]

    until(running)


def test_its_server_is_ready_again(left):
    def ready():
        found = workspace_of(left["name"])
        return found and found["server"] and found["server"]["state"] == "ready"

    until(ready)
    assert claude_processes(left["workspace"])


def started(workspace: int) -> datetime:
    """When Podman says the workspace's container last started."""
    found = json.loads(podman("container", "inspect", f"rcman-ws-{workspace}").stdout)[0]
    text = re.sub(r"(\.\d{6})\d+", r"\1", found["State"]["StartedAt"])  # to microseconds
    return datetime.fromisoformat(text)


def test_the_limits_are_back_in_the_kernel_before_the_container_starts(left):
    """The unit manager forgot the limits with the restart (their files are in a tmpfs); rcman
    set them again from its table before it started the workspace, and the container is in the
    workspace's slice."""
    me, workspace = left["person"], left["workspace"]
    own, theirs = slice_dir(me, workspace), slice_dir(me)

    def back():
        return limit_files(own)["pids.max"] == "3000" and limit_files(theirs)["pids.max"] == "6000"

    until(back)
    assert limit_files(own) == {
        "memory.max": str(2048 * MEBIBYTE),
        "memory.swap.max": "0",
        "cpu.max": "150000 100000",
        "pids.max": "3000",
    }
    assert limit_files(theirs) == {
        "memory.max": str(4096 * MEBIBYTE),
        "memory.swap.max": "0",
        "cpu.max": "300000 100000",
        "pids.max": "6000",
    }
    found = json.loads(podman("container", "inspect", f"rcman-ws-{workspace}").stdout)[0]
    leaf = cgroup_of(found["State"]["Pid"])
    assert leaf.parent.parent == own and leaf.parent.name.startswith("libpod-")
    times = drop_in_times(me, workspace)
    assert set(times) == set(left["drop_ins"]) and times  # the same properties, set again
    for name, then in left["drop_ins"].items():
        assert times[name] > then, name  # written after the restart emptied the tmpfs
    assert max(times.values()) / 1e9 < started(workspace).timestamp()  # and before the start
