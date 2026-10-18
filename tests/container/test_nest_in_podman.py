"""Containers in a workspace: the tools' podman and docker (backend/rcman/podman/podman) in a
workspace that the Podman runtime made on the tests' engine from the default image, as root,
who runs Podman in a nest of its own, and as a user who is not root, who runs it rootless.

Nothing here reaches a registry: the images are a busybox imported from a tarball and what is
built from it.
"""

from __future__ import annotations

import io
import re
import tarfile

import pytest
from rcman import runtime as rt

from installer import install as ins
from tests.container import sizes

WRAPPER = f"{rt.TOOLS_MOUNT}/{ins.PODMAN_TOOLS}/bin/podman"
DOCKER = f"{rt.TOOLS_MOUNT}/{ins.PODMAN_TOOLS}/bin/docker"
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
BUILT = "localhost/rcman-test-built"
CONTAINERFILE = (
    f"FROM {NESTED}\nRUN busybox rm /gone\nRUN busybox sh -c 'echo written > /written'\n"
)


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
        program: str = WRAPPER, as_user: tuple[int, str] | None = None) -> rt.Result:  # fmt: skip
    argv = [program, *args]
    if as_user is not None:
        uid, home = as_user
        argv = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--init-groups", "env", "-i",
                f"HOME={home}", "PATH=/usr/local/bin:/usr/bin:/bin", *argv]  # fmt: skip
    return podman.exec(name, argv, cwd=cwd or podman.layout(name).work, stdin=stdin, timeout=900)


def ok(result: rt.Result) -> str:
    assert result.code == 0, result
    return result.out.decode()


def nest_pid(podman, name: str) -> str:
    return ok(podman.exec(name, ["cat", f"{STATE}/nest"])).split()[0].split(".")[0]


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


def test_root_builds_from_a_subfolder_and_the_whiteout_holds(podman, nest_workspace, imported):
    work = podman.layout(nest_workspace).work
    ok(podman.exec(nest_workspace, ["sh", "-c",
                                    'mkdir -p ctx/sub && printf %s "$1" > ctx/sub/Containerfile',
                                    "sh", CONTAINERFILE], cwd=work))  # fmt: skip
    ok(
        run(
            podman,
            nest_workspace,
            "build",
            "-q",
            "--pull=never",
            "-t",
            BUILT,
            ".",
            cwd=f"{work}/ctx/sub",
        )
    )
    said = ok(
        run(
            podman,
            nest_workspace,
            "run",
            "--rm",
            "--pull=never",
            BUILT,
            "busybox",
            "sh",
            "-c",
            "busybox test ! -e /gone && busybox cat /written",
        )
    )
    assert said == "written\n"
    before = ok(
        run(
            podman,
            nest_workspace,
            "run",
            "--rm",
            "--pull=never",
            imported,
            "busybox",
            "cat",
            "/gone",
        )
    )
    assert before == "bye\n"  # control: the file was there below the whiteout


def test_root_runs_with_an_init(podman, nest_workspace, imported):
    said = ok(
        run(
            podman,
            nest_workspace,
            "run",
            "--rm",
            "--pull=never",
            "--init",
            imported,
            "busybox",
            "cat",
            "/proc/1/cmdline",
        )
    )
    assert "podman-init" in said  # catatonit, the configuration's init_path, bound there
    plain = ok(
        run(
            podman,
            nest_workspace,
            "run",
            "--rm",
            "--pull=never",
            imported,
            "busybox",
            "cat",
            "/proc/1/cmdline",
        )
    )
    assert "podman-init" not in plain  # control


def test_roots_storage_is_native_overlay_in_the_volume_and_its_mounts_are_in_the_nest(
    podman, nest_workspace, imported
):
    said = ok(run(podman, nest_workspace, "info", "--format",
                  "{{.Store.GraphRoot}}|{{.Store.GraphDriverName}}|{{.Store.GraphOptions}}|"
                  "{{.Store.GraphStatus}}|{{.Host.Security.Rootless}}"))  # fmt: skip
    root, driver, options, status, rootless = said.strip().split("|")
    assert root.startswith(f"{rt.VOLUME_MOUNT}/") and driver == "overlay"
    assert "Native Overlay Diff:true" in status and "mount_program" not in options
    assert rootless == "false"  # root of the nest
    running = ok(
        run(
            podman, nest_workspace, "run", "-d", "--pull=never", imported, "busybox", "sleep", "600"
        )
    )
    try:
        pid = nest_pid(podman, nest_workspace)
        nest = ok(podman.exec(nest_workspace, ["cat", f"/proc/{pid}/mountinfo"]))
        own = ok(podman.exec(nest_workspace, ["cat", "/proc/self/mountinfo"]))
        overlay = f"{root}/overlay/"
        assert overlay in nest  # positive control: the container's root is mounted there
        assert overlay not in own  # and not in the workspace's own mount namespace
    finally:
        run(podman, nest_workspace, "rm", "-f", "-t0", running.strip())


def test_the_nest_is_made_once_and_a_stale_or_unreadable_record_is_replaced(
    podman, nest_workspace, imported
):
    ok(run(podman, nest_workspace, "version"))
    first = ok(podman.exec(nest_workspace, ["cat", f"{STATE}/nest"])).split()
    ok(run(podman, nest_workspace, "version"))
    assert ok(podman.exec(nest_workspace, ["cat", f"{STATE}/nest"])).split() == first
    assert len(first) == 3 and re.fullmatch(r"\d+\.\d+", first[0])  # pid.start
    assert all(w.isdigit() for w in first[1:])
    own_users = ok(podman.exec(nest_workspace, ["stat", "-L", "-c", "%i", "/proc/self/ns/user"]))
    assert first[1] != own_users.strip()  # a user namespace of its own
    for planted in ("999999 1 2", "1;touch$IFS/planted 1 2", "garbage"):
        ok(podman.exec(nest_workspace, ["sh", "-c", f'printf "%s\\n" "$1" > {STATE}/nest', "sh",
                                        planted]))  # fmt: skip
        assert ok(podman.exec(nest_workspace, ["cat", f"{STATE}/nest"])).strip() == planted
        ok(run(podman, nest_workspace, "run", "--rm", "--pull=never", imported, "busybox", "true"))
        record = ok(podman.exec(nest_workspace, ["cat", f"{STATE}/nest"])).split()
        assert len(record) == 3 and all(w.isdigit() for w in record[1:]), record
        assert record[0].split(".")[0] != "999999"
    assert podman.exec(nest_workspace, ["test", "-e", "/planted"]).code != 0
    # a lock whose holder and whose claimer are gone is taken over, and let go
    plant = (f"rm -f {STATE}/nest && mkdir {STATE}/lock.999999.1 && "
             f"ln -s lock.999999.1 {STATE}/lock && "
             f"ln -s 999998.1 {STATE}/lock.999999.1/claim.1")  # fmt: skip
    ok(podman.exec(nest_workspace, ["sh", "-c", plant]))
    assert podman.exec(nest_workspace, ["test", "-L", f"{STATE}/lock"]).code == 0  # planted
    ok(run(podman, nest_workspace, "version"))
    assert podman.exec(nest_workspace, ["test", "-L", f"{STATE}/lock"]).code != 0
    assert podman.exec(nest_workspace, ["test", "-e", f"{STATE}/lock.999999.1"]).code != 0


def test_docker_is_the_same_podman_and_compose_is_refused(podman, nest_workspace, imported):
    said = ok(
        run(
            podman,
            nest_workspace,
            "run",
            "--rm",
            "--pull=never",
            imported,
            "busybox",
            "echo",
            "hi",
            program=DOCKER,
        )
    )
    assert said == "hi\n"
    for program in (WRAPPER, DOCKER):
        refused = run(podman, nest_workspace, "compose", "up", program=program)
        assert refused.code != 0 and b"compose is not run here" in refused.err, refused


def test_a_user_who_is_not_root_runs_podman_rootless_over_every_id_of_the_workspace(
    podman, nest_workspace
):
    user = (USER_ID, USER_HOME)
    said = ok(run(podman, nest_workspace, "unshare", "cat", "/proc/self/uid_map", as_user=user))
    outside = set()
    for line in said.splitlines():
        inside, start, count = (int(n) for n in line.split())
        outside |= set(range(start, start + count))
    assert outside == set(range(1, sizes.TEST_RANGE))  # every id of the workspace but root's
    ok(run(podman, nest_workspace, "import", "-", NESTED, stdin=tarball(), as_user=user))
    work = podman.layout(nest_workspace).work
    ok(
        podman.exec(
            nest_workspace,
            [
                "sh",
                "-c",
                f'mkdir -p {USER_HOME}/ctx && printf %s "$1" > '
                f"{USER_HOME}/ctx/Containerfile && chown -R {USER_ID} "
                f"{USER_HOME}/ctx",
                "sh",
                CONTAINERFILE,
            ],
            cwd=work,
        )
    )
    ok(
        run(
            podman,
            nest_workspace,
            "build",
            "-q",
            "--pull=never",
            "-t",
            BUILT,
            ".",
            cwd=f"{USER_HOME}/ctx",
            as_user=user,
        )
    )
    said = ok(
        run(
            podman,
            nest_workspace,
            "run",
            "--rm",
            BUILT,
            "busybox",
            "sh",
            "-c",
            "echo $$; busybox test ! -e /gone && busybox cat /written",
            as_user=user,
        )
    )
    assert said.split() == ["1", "written"]
    info = ok(run(podman, nest_workspace, "info", "--format",
                  "{{.Store.GraphRoot}}|{{.Store.GraphStatus}}|{{.Host.Security.Rootless}}",
                  as_user=user))  # fmt: skip
    root, status, rootless = info.strip().split("|")
    assert root.startswith(f"{USER_HOME}/") and "Native Overlay Diff:true" in status
    assert rootless == "true"


def test_with_a_setuid_newuidmap_a_user_who_is_not_root_cannot_map_the_ids(podman, nest_workspace):
    """The twin of the test above: newuidmap as Ubuntu ships it, setuid and without the file
    capability, earlier on the user's PATH."""
    plant = (
        "cp /usr/bin/newuidmap /usr/local/bin/newuidmap && chmod 4755 /usr/local/bin/newuidmap && "
        "cp /usr/bin/newgidmap /usr/local/bin/newgidmap && chmod 4755 /usr/local/bin/newgidmap"
    )
    ok(podman.exec(nest_workspace, ["sh", "-c", plant]))
    try:
        mode = ok(podman.exec(nest_workspace, ["stat", "-c", "%a", "/usr/local/bin/newuidmap"]))
        assert mode.strip() == "4755"  # the plant took effect
        caps = podman.exec(
            nest_workspace,
            ["python3", "-c",
             "import os; print(os.getxattr('/usr/local/bin/newuidmap', 'security.capability'))"],
        )  # fmt: skip
        assert caps.code != 0 and b"No data available" in caps.err  # no capability on the plant
        failed = run(podman, nest_workspace, "unshare", "true", as_user=(TWIN_ID, TWIN_HOME))
        assert failed.code != 0 and b"uid_map" in failed.err, failed
    finally:
        podman.exec(
            nest_workspace, ["sh", "-c", "rm -f /usr/local/bin/newuidmap /usr/local/bin/newgidmap"]
        )


@pytest.fixture
def without_podman(podman, base_image):
    """A workspace of an image without Podman: the default image's base."""
    spec = rt.Spec(rt.container_name(702), slot=2, workspace=702, person=2, image=base_image)
    podman.create(spec)
    try:
        yield spec.name
    finally:
        podman.remove(spec.name)
        podman.remove_account(spec.person)


def by_name(podman, name: str, *argv: str) -> rt.Result:
    """``argv`` found by name on the PATH Claude's programs and the terminal have."""
    layout = podman.layout(name)
    return podman.exec(name, layout.with_env(rt.child_env(layout), list(argv)))


def test_without_podman_in_the_image_and_nothing_after_the_wrappers_it_says_so(
    podman, without_podman
):
    for program in ("podman", "docker"):
        said = by_name(podman, without_podman, program, "version")
        assert said.code == 127 and b"no Podman" in said.err, said
        assert f"has no Podman and no {program} of its own".encode() in said.err, said
        assert b"the default image has Podman: for Podman, make a new workspace from it" in said.err


def test_without_podman_in_the_image_the_images_own_docker_after_the_wrappers_runs(
    podman, without_podman
):
    plant = "printf '#!/bin/sh\necho the image docker \"$@\"\n' > /usr/local/bin/docker"
    ok(podman.exec(without_podman, ["sh", "-c", plant + " && chmod 755 /usr/local/bin/docker"]))
    try:
        said = ok(by_name(podman, without_podman, "docker", "compose", "up"))
        assert said == "the image docker compose up\n"  # its own, compose and all
        refused = by_name(podman, without_podman, "podman", "version")
        assert refused.code == 127  # the control: no podman of the image's
    finally:
        podman.exec(without_podman, ["rm", "-f", "/usr/local/bin/docker"])


def test_the_wrapper_never_takes_itself_for_the_next_one(podman, without_podman):
    """The twin: a link to the wrapper later on the PATH is the wrapper, not the image's."""
    wrapper = f"{rt.PODMAN_BIN}/podman"
    ok(podman.exec(without_podman, ["ln", "-s", wrapper, "/usr/local/bin/docker"]))
    try:
        said = by_name(podman, without_podman, "docker", "version")
        assert said.code == 127 and b"no Podman" in said.err, said
    finally:
        podman.exec(without_podman, ["rm", "-f", "/usr/local/bin/docker"])


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


def no_nest(podman, name: str) -> None:
    """Every nest of the workspace ended and its record gone, as at the container's start."""
    for pid, _ in nests(podman, name):
        podman.exec(name, ["kill", "-9", pid])
    ok(podman.exec(name, ["find", STATE, "-mindepth", "1", "-delete"]))  # a tmpfs: emptied
    for _ in range(100):
        if not nests(podman, name):
            return
    raise AssertionError(nests(podman, name))


def test_calls_at_once_with_no_nest_and_a_stale_lock_make_one_nest(podman, nest_workspace):
    for _ in range(4):  # rounds, each with no nest and a lock its holder left
        no_nest(podman, nest_workspace)
        plant = (f"mkdir -p {STATE} && mkdir {STATE}/lock.999999.1 && "
                 f"ln -s lock.999999.1 {STATE}/lock")  # fmt: skip
        ok(podman.exec(nest_workspace, ["sh", "-c", plant]))
        at_once = f"for i in 1 2 3 4 5 6 7 8; do {WRAPPER} version >/dev/null & done; wait"
        ok(podman.exec(nest_workspace, ["sh", "-c", at_once], timeout=600))
        made = nests(podman, nest_workspace)
        record = ok(podman.exec(nest_workspace, ["cat", f"{STATE}/nest"])).split()
        assert len(made) == 1, made  # one nest, though eight asked for one at once
        assert [record[0].split(".")[0], record[1]] == made[0]  # and the record names it
        assert podman.exec(nest_workspace, ["test", "-L", f"{STATE}/lock"]).code != 0  # let go


def delayed(podman, name: str, wait: bool) -> str:
    """A copy of the wrapper whose nest is slow to make its user namespace, as under load; and,
    without ``wait``, with the wait for it taken out, as the wrapper once wrote the maps."""
    script = ok(podman.exec(name, ["cat", WRAPPER]))
    slow = script.replace(
        "\t$B setsid $B unshare -U",
        "\t$B setsid $B sh -c '$0 sleep 1; exec \"$@\"' $B $B unshare -U",
    )
    assert slow != script  # the plant took effect
    if not wait:
        start, end = slow.index("\t# (waits for the unshare)"), slow.index("\t# (has waited")
        slow = slow[:start] + slow[end:]
    copy = f"/root/podman-{'wait' if wait else 'nowait'}"
    ok(podman.exec(name, ["sh", "-c", f"cat > {copy} && chmod 755 {copy}"], stdin=slow.encode()))
    return copy


def test_a_nest_slow_to_make_its_namespace_is_still_made(podman, nest_workspace):
    no_nest(podman, nest_workspace)
    made = run(podman, nest_workspace, "version", program=delayed(podman, nest_workspace, True))
    assert made.code == 0, made
    assert len(nests(podman, nest_workspace)) == 1


def test_without_the_wait_a_nest_slow_to_make_its_namespace_is_not_mapped(podman, nest_workspace):
    """The twin: the maps written before the child has a user namespace of its own."""
    no_nest(podman, nest_workspace)
    failed = run(podman, nest_workspace, "version", program=delayed(podman, nest_workspace, False))
    assert failed.code != 0 and b"could not be mapped" in failed.err, failed


# --- root's record is root's alone -------------------------------------------------------------


@pytest.mark.parametrize("plant", ["another's folder", "a folder others may write"])
def test_root_refuses_a_record_folder_that_is_not_roots_alone(podman, nest_workspace, plant):
    no_nest(podman, nest_workspace)
    planted = {
        "another's folder": f"chown {USER_ID} {STATE}",
        "a folder others may write": f"chmod 777 {STATE}",
    }[plant]
    ok(podman.exec(nest_workspace, ["sh", "-c", planted]))
    try:
        refused = run(podman, nest_workspace, "version")
        assert refused.code == 125 and b"of root's alone" in refused.err, refused
        assert nests(podman, nest_workspace) == []  # nothing was made
    finally:
        ok(podman.exec(nest_workspace, ["sh", "-c", f"chown 0 {STATE} && chmod 700 {STATE}"]))
    ok(run(podman, nest_workspace, "version"))  # the control: once it is root's, it runs


def test_a_workspace_made_without_the_record_tmpfs_is_refused(podman, monkeypatch):
    """As one made before containers were supported is: nothing of root's Podman runs there."""
    monkeypatch.setattr(rt.PodmanRuntime, "_root_podman_state", lambda self: [])
    spec = rt.Spec(rt.container_name(705), slot=2, workspace=705, person=2)
    podman.create(spec)
    try:
        mounts = ok(podman.exec(spec.name, ["cat", "/proc/self/mountinfo"]))
        assert f" {STATE} " not in mounts  # the plant took effect
        refused = run(podman, spec.name, "version")
        assert refused.code == 125, refused
        assert b"made before containers were supported; make a new workspace" in refused.err
        assert podman.exec(spec.name, ["test", "-e", STATE]).code != 0  # nothing was made
    finally:
        podman.remove(spec.name)
        podman.remove_account(spec.person)


# --- compose ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [["compose", "up"], ["--log-level=debug", "compose", "up"], ["--log-level", "debug", "compose"],
     ["-l", "debug", "compose", "up"], ["--remote", "compose"], ["--", "compose"]],
)  # fmt: skip
def test_compose_is_refused_wherever_it_is_the_subcommand(podman, nest_workspace, args):
    for program in (WRAPPER, DOCKER):
        refused = run(podman, nest_workspace, *args, program=program)
        assert refused.code == 125 and b"compose is not run here" in refused.err, refused


def test_compose_as_an_argument_of_another_subcommand_is_not_refused(
    podman, nest_workspace, imported
):
    said = ok(
        run(
            podman,
            nest_workspace,
            "--log-level",
            "error",
            "run",
            "--rm",
            "--pull=never",
            imported,
            "busybox",
            "echo",
            "compose",
        )
    )
    assert said == "compose\n"


# --- the default image ------------------------------------------------------------------------

CAPABILITIES = """
import os, struct, sys
for path, bit in (("/usr/bin/newuidmap", 7), ("/usr/bin/newgidmap", 6)):
    mode = os.stat(path).st_mode
    data = os.getxattr(path, "security.capability")
    permitted = struct.unpack_from("<I", data, 4)[0]
    print(path, oct(mode & 0o7777), bool(permitted & (1 << bit)), permitted)
"""


def test_the_default_images_newuidmap_and_newgidmap_have_their_capability_and_no_setuid(
    podman, nest_workspace
):
    lines = ok(podman.exec(nest_workspace, ["python3", "-c", CAPABILITIES])).splitlines()
    assert [line.split()[:3] for line in lines] == [
        ["/usr/bin/newuidmap", "0o755", "True"],  # CAP_SETUID, and not setuid
        ["/usr/bin/newgidmap", "0o755", "True"],  # CAP_SETGID
    ]
    assert all(line.split()[3] == str(1 << b) for line, b in zip(lines, (7, 6), strict=True))


def test_the_default_image_has_every_package_it_asks_for_after_the_purge(podman, nest_workspace):
    import re

    asked = re.search(r"--no-install-recommends \\\n\s*([^\\]+)\\", ins.CONTAINERFILE.read_text())
    packages = [p for p in asked.group(1).split() if p != "libcap2-bin"]
    assert {"podman", "uidmap", "crun", "catatonit", "git", "gh"} <= set(packages)  # read whole
    shown = ok(podman.exec(nest_workspace, ["dpkg-query", "-W", "-f", "${Package} ${Status}\n",
                                            *packages]))  # fmt: skip
    installed = {line.split()[0] for line in shown.splitlines() if line.endswith("installed")}
    assert installed == set(packages), shown
    purged = podman.exec(nest_workspace, ["dpkg-query", "-W", "-f", "${Status}", "libcap2-bin"])
    assert b"install ok installed" not in purged.out  # setcap was removed once used


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


DANGLING = f"mkdir -p -m 700 {STATE} && ln -s lock.999999.1 {STATE}/lock"


def test_a_lock_whose_folder_is_gone_is_taken_over_and_let_go(podman, nest_workspace):
    no_nest(podman, nest_workspace)
    ok(podman.exec(nest_workspace, ["sh", "-c", DANGLING]))
    made = run(podman, nest_workspace, "version")
    assert made.code == 0, made
    assert podman.exec(nest_workspace, ["test", "-L", f"{STATE}/lock"]).code != 0
    assert len(nests(podman, nest_workspace)) == 1


def test_without_making_the_claims_folder_a_lock_whose_folder_is_gone_holds_forever(
    podman, nest_workspace
):
    """The twin: the claim cannot be made in a folder that is not there."""
    no_nest(podman, nest_workspace)
    ok(podman.exec(nest_workspace, ["sh", "-c", DANGLING]))
    copy = without(podman, nest_workspace, "(claim's folder)")
    held = podman.exec(nest_workspace, ["timeout", "15", copy, "version"], timeout=60)
    assert held.code == 124, held  # still trying when it was stopped
    ok(podman.exec(nest_workspace, ["rm", "-f", f"{STATE}/lock"]))


def test_letting_go_leaves_a_folder_whose_claimer_runs_and_removes_one_whose_holders_are_gone(
    podman, nest_workspace
):
    no_nest(podman, nest_workspace)
    sleeper = ["sh", "-c", "setsid sleep 300 >/dev/null 2>&1 & echo $!"]
    claimer = ok(podman.exec(nest_workspace, sleeper))
    pid = claimer.strip()
    start = ok(podman.exec(nest_workspace, ["sh", "-c", f"cut -d')' -f2 /proc/{pid}/stat"]))
    started = start.split()[19]
    plant = (f"mkdir -p -m 700 {STATE}/lock.999999.1 {STATE}/lock.999998.1 && "
             f"ln -s {pid}.{started} {STATE}/lock.999999.1/claim.1")  # fmt: skip
    ok(podman.exec(nest_workspace, ["sh", "-c", plant]))
    try:
        ok(run(podman, nest_workspace, "version"))
        assert podman.exec(nest_workspace, ["test", "-d", f"{STATE}/lock.999999.1"]).code == 0
        assert podman.exec(nest_workspace, ["test", "-e", f"{STATE}/lock.999998.1"]).code != 0
    finally:
        podman.exec(nest_workspace, ["kill", pid])


OWN = "readlink /proc/self/ns/user /proc/self/ns/mnt | tr -dc '0-9\\n'"


def plant_own_record(podman, name: str) -> str:
    """A record naming pid 1, which runs, as the nest, with the workspace's own namespaces."""
    start = ok(podman.exec(name, ["sh", "-c", "cut -d')' -f2 /proc/1/stat"])).split()[19]
    users, mounts = ok(podman.exec(name, ["sh", "-c", OWN])).split()
    record = f"1.{start} {users} {mounts}"
    ok(podman.exec(name, ["sh", "-c", f'mkdir -p -m 700 {STATE} && echo "$1" > {STATE}/nest',
                          "sh", record]))  # fmt: skip
    return record


def test_after_a_restart_the_record_is_gone_and_a_record_of_the_workspaces_own_is_refused(
    podman, nest_workspace
):
    ok(run(podman, nest_workspace, "version"))
    assert ok(podman.exec(nest_workspace, ["ls", "-A", STATE]))  # control: there is a record
    podman.stop(nest_workspace)
    podman.start(nest_workspace)
    assert ok(podman.exec(nest_workspace, ["ls", "-A", STATE])) == ""  # a tmpfs, empty again
    assert ok(podman.exec(nest_workspace, ["stat", "-c", "%u %a", STATE])).split() == ["0", "700"]
    planted = plant_own_record(podman, nest_workspace)
    ok(run(podman, nest_workspace, "version"))
    record = ok(podman.exec(nest_workspace, ["cat", f"{STATE}/nest"])).split()
    assert record != planted.split() and record[0].split(".")[0] != "1"
    assert len(record) == 3 and len(nests(podman, nest_workspace)) == 1
    assert ok(podman.exec(nest_workspace, ["sh", "-c", OWN])).split() != record[1:]


def test_without_the_checks_a_record_of_the_workspaces_own_is_entered(podman, nest_workspace):
    """The twin: the record taken as it reads, so that root's Podman would be entered into the
    workspace's own namespaces; no nest is made, and nothing runs."""
    no_nest(podman, nest_workspace)
    planted = plant_own_record(podman, nest_workspace)
    copy = without(podman, nest_workspace, "(refuses a stale or own record)")
    ran = run(podman, nest_workspace, "version", program=copy)
    # it takes pid 1 for the nest, and cannot enter the user namespace it is in already
    assert ran.code != 0 and b"reassociate to namespace 'user'" in ran.err, ran
    assert ok(podman.exec(nest_workspace, ["cat", f"{STATE}/nest"])).strip() == planted
    assert nests(podman, nest_workspace) == []
    no_nest(podman, nest_workspace)


def test_in_a_workspace_whose_user_is_not_root_roots_record_folder_is_roots_alone(podman):
    spec = rt.Spec(rt.container_name(703), slot=2, workspace=703, person=2,
                   user=str(USER_ID), uid=USER_ID, gid=USER_ID)  # fmt: skip
    podman.create(spec)
    try:
        assert ok(podman.exec(spec.name, ["id", "-u"])).strip() == str(USER_ID)  # its user
        shown = ok(podman.exec(spec.name, ["stat", "-c", "%u %a", STATE])).split()
        assert shown == ["0", "700"]  # what the wrapper asks of it
        mounts = ok(podman.exec(spec.name, ["cat", "/proc/self/mountinfo"]))
        assert f" {STATE} " in mounts  # a mount point, which no one renames away
        own_run = ok(podman.exec(spec.name, ["stat", "-c", "%u", rt.RUN_MOUNT])).strip()
        assert own_run == str(USER_ID)  # the control: RUN_MOUNT is the user's there
    finally:
        podman.remove(spec.name)
        podman.remove_account(spec.person)


# --- on the PATH ------------------------------------------------------------------------------


def test_podman_and_docker_are_the_wrappers_on_the_path_and_git_the_images(podman, nest_workspace):
    """In the environment Claude's programs and the Terminal tab's shell have (child_env), and in
    the one rcman runs the lifecycle commands with (Workspaces._hook_env), as the shell finds
    each by name."""
    from rcman.workspaces import Workspaces

    layout = podman.layout(nest_workspace)
    find = [layout.shell, "-c", "for c in podman docker git; do command -v $c; done"]
    for env in (rt.child_env(layout), Workspaces._hook_env(layout)):
        found = ok(podman.exec(nest_workspace, layout.with_env(env, find))).split()
        assert found == [WRAPPER, DOCKER, "/usr/bin/git"], found
    # and the name runs the wrapper: root's Podman in its nest
    info = [layout.shell, "-c", "podman info --format '{{.Store.GraphRoot}}'"]
    ran = ok(podman.exec(nest_workspace, layout.with_env(rt.child_env(layout), info)))
    assert ran.strip() == "/rcman/containers/storage"
