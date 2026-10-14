"""After the whole podman host has restarted, as a machine reboots, rcman brings back its
workspaces and their servers.

``tests/test_container_suite.py`` runs this module in the podman host that
test_install_in_container.py installed rcman in and left a workspace's server running
in, once it has restarted the host: its tmpfs runtime folders are empty, every
container stopped, and systemd booted again. Anywhere else it skips.
"""

import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1"
    or os.geteuid() != 0
    or not Path("/var/tmp/rcman-left-running.json").exists(),
    reason="runs only as root inside the podman host, after the installation suite",
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from installed import claude_processes, healthy, podman, until, workspace_of  # noqa: E402

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
