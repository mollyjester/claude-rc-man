"""Rootless Podman for a service account, under systemd, inside the podman host.

``tests/test_container_suite.py`` runs this module in the podman host: a container
built from ``tests/container/podman/Dockerfile`` that runs systemd as PID 1, set up
by ``tests/container/podman_host.py``, with a base image loaded into the account's
Podman and named in ``RCMAN_PODMAN_IMAGE``. Anywhere else it skips.
"""

import json
import os
import pwd
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests.container import podman_host as ph

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1"
    or os.geteuid() != 0
    or "RCMAN_PODMAN_IMAGE" not in os.environ,
    reason="runs only as root inside the podman host",
)

# Seconds a container started through the socket may take to run.
STARTS_WITHIN = 120


def as_account(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ph.as_account(*argv), capture_output=True, text=True, errors="replace", cwd="/", timeout=300
    )
    assert proc.returncode == 0 or not check, proc.stdout + proc.stderr
    return proc


def uid() -> int:
    return pwd.getpwnam(ph.ACCOUNT).pw_uid


def info() -> dict:
    return json.loads(as_account("podman", "info", "--format", "json").stdout)


def fstype(path: str) -> str:
    """The type of the filesystem ``path`` is on: its longest mount point's in
    /proc/self/mountinfo."""
    best, found = "", ""
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        point = fields[4]
        kind = fields[fields.index("-") + 1]
        if (path == point or path.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, found = point, kind
    return found


def cgroup(pid: int) -> str:
    return Path(f"/proc/{pid}/cgroup").read_text().strip().removeprefix("0::")


def systemctl(*args: str) -> str:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, errors="replace", timeout=120
    ).stdout.strip()


def test_podman_runs_rootless_as_the_service_account():
    host = info()["host"]
    assert host["security"]["rootless"] is True
    assert host["cgroupManager"] == "systemd"
    assert host["cgroupVersion"] == "v2"


def test_podmans_storage_is_on_a_volume_not_on_the_images_overlay():
    store = info()["store"]
    assert store["graphDriverName"] == "overlay"
    assert store["graphRoot"].startswith(ph.HOME + "/")
    assert fstype("/") == "overlay"  # positive control: the image itself is overlay
    assert fstype(store["graphRoot"]) not in ("overlay", "")


def test_linger_and_the_podman_socket_are_active():
    linger = subprocess.run(
        ["loginctl", "show-user", ph.ACCOUNT, "--property", "Linger", "--value"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    assert linger.stdout.strip() == "yes", linger.stderr
    assert systemctl("--user", "-M", f"{ph.ACCOUNT}@", "is-active", "podman.socket") == "active"
    assert Path(f"/run/user/{uid()}/podman/podman.sock").is_socket()


def test_memory_cpu_and_pids_are_delegated_to_the_accounts_user_manager():
    wanted = {"memory", "cpu", "pids"}
    manager = Path(f"/sys/fs/cgroup/user.slice/user-{uid()}.slice/user@{uid()}.service")
    parent = (manager.parent / "cgroup.subtree_control").read_text()
    assert wanted <= set((manager / "cgroup.controllers").read_text().split()), parent
    assert wanted <= set(info()["host"]["cgroupControllers"])


@pytest.fixture
def caller():
    """A system service of the account's that starts a container through the socket,
    and stays running: (its unit, the container's name). Both are removed afterwards."""
    unit = f"rcman-test-caller-{uuid.uuid4().hex[:8]}.service"
    name = f"probe-{uuid.uuid4().hex[:8]}"
    image = os.environ["RCMAN_PODMAN_IMAGE"]
    started = subprocess.run(
        ["systemd-run", f"--unit={unit}", f"--uid={ph.ACCOUNT}",
         f"--setenv=XDG_RUNTIME_DIR=/run/user/{uid()}", "--working-directory=/", "sh", "-c",
         f"podman --remote run -d --init --name {name} {image} sleep 600 && exec sleep 600"],
        capture_output=True, text=True, errors="replace",
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    try:
        yield unit, name
    finally:
        subprocess.run(["systemctl", "stop", unit], capture_output=True)
        as_account("podman", "--remote", "rm", "-f", "-t", "0", name, check=False)


def test_a_container_started_through_the_socket_runs_outside_the_callers_cgroup(caller):
    unit, name = caller
    deadline = time.monotonic() + STARTS_WITHIN
    while True:
        found = as_account(
            "podman", "--remote", "inspect", "--format",
            "{{.State.Running}} {{.State.Pid}} {{.State.ConmonPid}}", name, check=False,
        )  # fmt: skip
        if found.stdout.startswith("true "):
            break
        journal = ["journalctl", "--no-pager", "--unit", unit]
        assert systemctl("is-active", unit) == "active", (
            found.stderr
            + subprocess.run(journal, capture_output=True, text=True, errors="replace").stdout
        )
        assert time.monotonic() < deadline, found.stdout + found.stderr
        time.sleep(0.5)
    _, pid, conmon = found.stdout.split()
    own = systemctl("show", unit, "--property", "ControlGroup", "--value")
    main = int(systemctl("show", unit, "--property", "MainPID", "--value"))
    assert cgroup(main) == own  # positive control: the caller is in its own cgroup
    manager = f"/user.slice/user-{uid()}.slice/user@{uid()}.service/"
    for process in (int(pid), int(conmon)):
        assert not cgroup(process).startswith(own + "/") and cgroup(process) != own
        assert cgroup(process).startswith(manager)

    subprocess.run(["systemctl", "stop", unit], check=True, timeout=120)
    still = as_account("podman", "--remote", "inspect", "--format", "{{.State.Pid}}", name)
    assert still.stdout.strip() == pid  # the caller gone, the container runs on
    assert systemctl("is-active", unit) != "active"  # positive control: it is gone
    for process in (int(pid), int(conmon)):
        assert Path(f"/proc/{process}").exists()
        assert cgroup(process).startswith(manager)
