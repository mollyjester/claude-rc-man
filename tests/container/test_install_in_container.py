"""rcman installed by its real installer, as root, in a stubbed installation: a container of the
system image (tests/container/system/) on the tests' engine (tests/podman_service.py), with no
network, real accounts, files, modes and ``runuser``, and ``systemctl``, ``loginctl`` and rcman's
``podman`` the recorded stand-ins of tests/container/stand_ins.py, under which the installer's
units really run as their accounts.

The checkout's files are at /src (tests/snapshot.py); a user ``dev`` clones them and runs
``install.sh`` through the real path, with the fake claude as the Claude Code release it downloads
(tests/container/installed.py). The toolchain comes from the user's cache, filled with the
installer's own download code (the ``toolchain`` fixture); the frontend's and the backend's
packages from caches filled beforehand in a container of the same image with the network
(``offline_caches``); tmux is the one built on the engine with the installer's own argument list,
which the stand-in podman hands the installer. The first installation is made once; each test then
runs its check of tests/container/install_checks.py, of the same name, as root in the container,
in order, on that installation. What needs systemd, rcman's own rootless Podman, the kernel's
limits or the journal is checked by hand (project-knowledge/real-claude-checklist.md).
The container tier keeps only the first check, as a basic install-and-serve check.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from devtools import held_folders as hf
from installer import install as ins
from tests.hang_guard import CONTAINER_HANG_GUARD, EXEC_GUARD, GUARD_VARIABLE

# The installer's toolchain, where the container has it.
TOOLCHAIN = Path("/opt/rcman/toolchain")
# The programs the stand-ins are mounted over.
STOOD_IN = ("/usr/bin/systemctl", "/usr/bin/loginctl", "/usr/bin/podman")
STAND_INS = "/var/lib/rcman-test-stand-ins"


def ran(done, what: str) -> str:
    output = (done.stdout + done.stderr).decode(errors="replace")
    assert done.returncode == 0, f"{what}:\n{output[-12000:]}"
    return output


@pytest.fixture(scope="session")
def offline_caches(podman_service, system_image, source, toolchain, labelled):
    """npm's cache of the frontend's packages and uv's of the backend's, filled in a container of
    the system image with the engine's own network, as the installer would fetch them: so that
    the installation, which has no network, installs them from the caches alone."""
    with hf.held("rccache-") as folder:
        os.chmod(folder, 0o755)
        node, uv = ins.node_bin(TOOLCHAIN), ins.uv_binary(TOOLCHAIN)
        script = (
            "set -e; cp -a /src /tmp/s; cd /tmp/s/frontend; "
            f"PATH={node}:$PATH npm ci --no-audit --no-fund --cache /cache/npm; cd /tmp/s; "
            f"UV_CACHE_DIR=/cache/uv {uv} sync --frozen --no-dev --no-editable "
            "--python /usr/bin/python3; chmod -R a+rwX /cache"
        )
        done = podman_service.cli(
            "run", "--rm", "--pull=never", "--network=host", *labelled, "-v", f"{source}:/src:ro",
            "-v", f"{toolchain}:{TOOLCHAIN}:ro", "-v", f"{folder}:/cache", system_image,
            "sh", "-c", script, timeout=CONTAINER_HANG_GUARD,
        )  # fmt: skip
        ran(done, "the caches were not filled")
        yield folder


class Installation:
    """The stubbed installation's container on the engine."""

    def __init__(self, engine, name: str):
        self.engine, self.name = engine, name

    def exec(self, *argv: str, stdin: bytes | None = None):
        """``argv`` in the installation's container, handed its tier's hang guard (``until``'s
        bound there) and killed beyond it (EXEC_GUARD), so that a wait there that never ends
        says what it waited for."""
        return self.engine.cli(
            "exec", "-i", "-w", "/src", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e",
            f"{GUARD_VARIABLE}={CONTAINER_HANG_GUARD}", self.name, *argv, input=stdin,
            timeout=EXEC_GUARD,
        )  # fmt: skip

    def check(self, name: str) -> None:
        """The check of tests/container/install_checks.py called ``name``, as root."""
        done = self.exec("python3", "tests/container/install_checks.py", name)
        ran(done, f"the check {name} failed")


@pytest.fixture(scope="module")
def installation(
    podman_service, system_image, labelled, source, toolchain, offline_caches, engine_tools
):
    stand_in = source / "tests" / "container" / "stand_ins.py"
    name = f"rcman-test-install-{os.getpid()}"
    mounts = [arg for program in STOOD_IN for arg in ("-v", f"{stand_in}:{program}:ro")]
    # A copy of the user's toolchain: the installer, as root there, takes its lock in it and
    # fills what is missing, which the user's own cache never gets.
    # In a folder of the held one's: the held folder's own lock is a file of the name the
    # installer's lock has.
    with hf.held("rctoolchain-") as held_copy:
        toolchain_copy = held_copy / "toolchain"
        shutil.copytree(toolchain, toolchain_copy, symlinks=True)
        os.chmod(held_copy, 0o755)
        made = podman_service.cli(
            "run", "-d", "--init", "--pull=never", "--network=none", *labelled, "--name", name,
            "-v", f"{source}:/src:ro", "-v", f"{toolchain_copy}:{TOOLCHAIN}",
            "-v", f"{offline_caches}:/cache", *mounts, system_image, "sleep", "infinity",
            timeout=CONTAINER_HANG_GUARD,
        )  # fmt: skip
        try:
            ran(made, "the installation's container did not start")
            found = Installation(podman_service, name)
            tmux = (engine_tools / "bin" / "tmux").read_bytes()
            put = found.exec(
                "sh", "-c", f"mkdir -p -m 777 {STAND_INS} && cat > {STAND_INS}/tmux", stdin=tmux
            )
            ran(put, "tmux was not handed to the stand-in")
            found.check("first")
            yield found
        finally:
            podman_service.cli("rm", "--force", "--time=0", name, timeout=CONTAINER_HANG_GUARD)


def test_the_first_installation_puts_rcman_in_caddy_and_restarts_it(installation):
    installation.check("test_the_first_installation_puts_rcman_in_caddy_and_restarts_it")
