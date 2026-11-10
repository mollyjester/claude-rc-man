"""Containers in a workspace: the tools' podman and docker (backend/rcman/podman/podman) in a
workspace that the Podman runtime made on the tests' engine from the default image, as root,
who runs Podman in a nest of its own, and as a user who is not root, who runs it rootless.

Nothing here reaches a registry: the images are a busybox imported from a tarball and what is
built from it.
"""

from __future__ import annotations

import io
import tarfile

import pytest
from rcman import runtime as rt

from installer import install as ins
from tests.container import sizes
from tests.hang_guard import CONTAINER_HANG_GUARD

WRAPPER = f"{rt.TOOLS_MOUNT}/{ins.PODMAN_TOOLS}/bin/podman"
BUSYBOX = f"{rt.TOOLS_MOUNT}/bin/busybox"
# Root's record of its nest: a folder of root's alone in the image's /run (the wrapper's STATE).
STATE = rt.ROOT_PODMAN_STATE
# The image's own user who is not root, and the home that holds its Podman storage, in the
# volume as every home of a workspace is.
USER, USER_ID = "ubuntu", 1000
USER_HOME = f"{rt.VOLUME_MOUNT}/home-{USER}"
# A user whose newuidmap is planted setuid, as Ubuntu ships it.
TWIN, TWIN_ID = "twin", 2000
TWIN_HOME = f"{rt.VOLUME_MOUNT}/home-{TWIN}"
NESTED = "localhost/rcman-test-bb"


def tarball() -> bytes:
    """An image's root: the host's static busybox, its shell, and a file the build removes."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        tar.add(ins.BUSYBOX, arcname="bin/busybox")
        link = tarfile.TarInfo("bin/sh")
        link.type, link.linkname = tarfile.SYMTYPE, "busybox"
        tar.addfile(link)
        gone = tarfile.TarInfo("gone")
        gone.size = 4
        tar.addfile(gone, io.BytesIO(b"bye\n"))
    return data.getvalue()


def ranges(user: str, own: int) -> str:
    """/etc/subuid lines that give ``user`` every id of the workspace but root's and its own."""
    return f"{user}:1:{own - 1}\n{user}:{own + 1}:{sizes.TEST_RANGE - own - 1}\n"


@pytest.fixture(scope="module")
def nest_workspace(podman):
    spec = rt.Spec(rt.container_name(701), slot=1, workspace=701, person=1)
    podman.create(spec)
    try:
        layout = podman.layout(spec.name)
        script = (
            f"mkdir -p {USER_HOME} {TWIN_HOME} && chown {USER_ID}:{USER_ID} {USER_HOME} && "
            f"useradd -M -u {TWIN_ID} -d {TWIN_HOME} {TWIN} && "
            f"chown {TWIN_ID}:{TWIN_ID} {TWIN_HOME} && chmod 711 {rt.VOLUME_MOUNT} && "
            f"printf %s '{ranges(USER, USER_ID)}{ranges(TWIN, TWIN_ID)}' > /etc/subuid && "
            "cp /etc/subuid /etc/subgid"
        )
        made = podman.exec(spec.name, ["sh", "-c", script], cwd=layout.work)
        assert made.code == 0, made
        yield spec.name
    finally:
        podman.remove(spec.name)
        podman.remove_account(spec.person)


def run(podman, name: str, *args: str, cwd: str | None = None, stdin: bytes | None = None,
        program: str = WRAPPER, as_user: tuple[int, str] | None = None,
        environment: tuple[str, ...] = ()) -> rt.Result:  # fmt: skip
    """``program`` with ``args`` in the workspace, as root or ``as_user`` (an id and a home) with
    nothing else in the environment but ``environment``, NAME=value each."""
    argv = [program, *args]
    if as_user is not None:
        uid, home = as_user
        argv = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--init-groups", "env", "-i",
                f"HOME={home}", "PATH=/usr/local/bin:/usr/bin:/bin", *environment,
                *argv]  # fmt: skip
    elif environment:
        argv = ["env", *environment, *argv]
    return podman.exec(
        name, argv, cwd=cwd or podman.layout(name).work, stdin=stdin, timeout=CONTAINER_HANG_GUARD
    )


def ok(result: rt.Result) -> str:
    assert result.code == 0, result
    return result.out.decode()


@pytest.fixture(scope="module")
def imported(podman, nest_workspace):
    ok(run(podman, nest_workspace, "import", "-", NESTED, stdin=tarball()))
    return NESTED


def test_root_runs_an_imported_image_in_a_pid_namespace_with_a_fresh_proc(
    podman, nest_workspace, imported
):
    said = ok(
        run(
            podman,
            nest_workspace,
            "run",
            "--rm",
            "--pull=never",
            imported,
            "busybox",
            "sh",
            "-c",
            "echo $$; busybox cat /gone; busybox ls /proc",
        )
    )
    lines = said.split()
    assert lines[0] == "1" and lines[1] == "bye"
    assert len([w for w in lines[2:] if w.isdigit()]) <= 3  # its own processes only


# --- a Podman of a program's own, with its storage under /tmp -----------------------------------


# --- the nest is made once, whatever the timing ------------------------------------------------

# The workspace's processes that are nests: in a user namespace other than the workspace's, the
# mounted busybox's sleep. Printed as "<pid> <user namespace>" lines.
NESTS = f"""
import os
own = os.stat("/proc/self/ns/user").st_ino
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        users = os.stat(f"/proc/{{pid}}/ns/user").st_ino
        line = open(f"/proc/{{pid}}/cmdline", "rb").read()
    except OSError:
        continue
    if users != own and line == b"{rt.TOOLS_MOUNT}/bin/busybox\\x00sleep\\x002147483647\\x00":
        print(pid, users)
"""


def nests(podman, name: str) -> list[list[str]]:
    return [line.split() for line in ok(podman.exec(name, ["python3", "-c", NESTS])).splitlines()]


# The file a delayed nest waits for before it makes its user namespace.
GO = "/root/nest-go"


def delayed(podman, name: str, waiting: bool) -> str:
    """A copy of the wrapper whose nest is slow to make its user namespace, as under load: it
    makes it only once GO is there, which the test makes. With ``waiting``, the copy waits for it
    with no count of its own, so that the test, not a count of looks, decides how slow it is;
    without, the wait for it is taken out, as the wrapper once wrote the maps."""
    ok(podman.exec(name, ["rm", "-f", GO]))
    script = ok(podman.exec(name, ["cat", WRAPPER]))
    slow = script.replace(
        "\t$B setsid $B unshare -U",
        f"\t$B setsid $B sh -c 'while [ ! -e {GO} ]; do $0 usleep 10000; done; exec \"$@\"' "
        "$B $B unshare -U",
    )
    assert slow != script  # the plant took effect
    if waiting:
        counted = ' || [ "$tries" -ge 1000 ]; then\n\t\t\tsay "the nest did not start"'
        assert counted in slow  # and this one too
        slow = slow.replace(counted, '; then\n\t\t\tsay "the nest did not start"', 1)
    else:
        start, end = slow.index("\t# (waits for the unshare)"), slow.index("\t# (has waited")
        slow = slow[:start] + slow[end:]
    copy = f"/root/podman-{'wait' if waiting else 'nowait'}"
    ok(podman.exec(name, ["sh", "-c", f"cat > {copy} && chmod 755 {copy}"], stdin=slow.encode()))
    return copy


# --- root's record is root's alone -------------------------------------------------------------


# --- compose ----------------------------------------------------------------------------------


# --- a published port ------------------------------------------------------------------------


# --- the default image ------------------------------------------------------------------------


# --- the lock and the record hold whatever is left in the folder ------------------------------


def without(podman, name: str, marker: str) -> str:
    """A copy of the wrapper without its lines marked ``marker``, as it was before they were
    written: the twin of the test that needs them."""
    script = ok(podman.exec(name, ["cat", WRAPPER]))
    kept = "".join(line for line in script.splitlines(True) if marker not in line)
    assert kept != script  # the plant took effect
    copy = f"/root/podman-without-{abs(hash(marker))}"
    ok(podman.exec(name, ["sh", "-c", f"cat > {copy} && chmod 755 {copy}"], stdin=kept.encode()))
    return copy


def runs(podman, name: str, pid: str) -> bool:
    """Whether the process ``pid`` of the workspace runs: there, and not a zombie."""
    state = "s=$(cut -d')' -f2 /proc/$1/stat 2>/dev/null) && set -- $s && [ \"$1\" != Z ]"
    return podman.exec(name, ["sh", "-c", state, "sh", pid]).code == 0


# --- on the PATH ------------------------------------------------------------------------------
