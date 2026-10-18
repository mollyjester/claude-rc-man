"""A workspace on the tests' engine (tests/podman_service.py): ``PodmanRuntime.create``'s own
argv, with the suites' sizes injected (tests/container/sizes.py), and what rcman does in it - an
exec, the channel, the Terminal tab's shell; what the workspace's seccomp profile and its unmasked
/proc allow a container nested in it, against a sign-in container, which has neither; nothing of
the workspace left once it is stopped or removed; and the sizes fitting the engine's ids and every
id the suites use.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import pty
import select
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
from rcman import runtime as rt

from tests import podman_service as ps
from tests.container import sizes

CONTAINER_SUITES = Path(__file__).resolve().parent
# The length of a sleep no other test runs, by which a process the test starts in the workspace
# is found on the host.
MARKER_SLEEP = "31337"
GONE_WITHIN = 30


@pytest.fixture(scope="module")
def workspace(podman):
    spec = rt.Spec(rt.container_name(711), slot=3, workspace=711, person=3)
    podman.create(spec)
    try:
        yield spec
    finally:
        podman.remove(spec.name)
        podman.remove_account(spec.person)


@pytest.fixture(scope="module")
def sign_in(podman, workspace):
    """A sign-in container of the same person: made without the workspace's seccomp profile or
    its unmasked /proc."""
    name = podman.start_sign_in(workspace.person, workspace.slot, 600)
    try:
        yield name
    finally:
        podman.end_sign_in(name)


def ok(result) -> str:
    assert result.code == 0, result
    return result.out.decode()


def test_a_workspace_is_made_with_its_name_its_range_and_the_injected_network(podman, workspace):
    assert podman.running(workspace.name)
    assert ok(podman.exec(workspace.name, ["cat", "/proc/sys/kernel/hostname"])).strip() == (
        workspace.name
    )
    base = workspace.slot * sizes.TEST_RANGE
    found = ok(podman.exec(workspace.name, ["cat", "/proc/self/uid_map", "/proc/self/gid_map"]))
    assert found.split() == [str(n) for n in (0, base, sizes.TEST_RANGE)] * 2
    inspected = json.loads(subprocess.run(
        ["podman", "--remote", "--url", f"unix://{podman.config.socket}", "container", "inspect",
         workspace.name], capture_output=True, check=True).stdout)[0]  # fmt: skip
    assert inspected["HostConfig"]["NetworkMode"] == sizes.TEST_NETWORK
    assert inspected["HostConfig"]["PidsLimit"] in (0, -1, None)  # no limit of Podman's own
    assert not inspected["HostConfig"].get("CgroupParent")  # no slices on the engine


def test_rcmans_exec_and_channel_work_in_it(podman, workspace):
    assert ok(podman.exec(workspace.name, ["echo", "hi"])) == "hi\n"
    assert podman.exec(workspace.name, ["cat"], stdin=b"in").out == b"in"
    channel = podman.channel(workspace.name)
    try:
        layout = podman.layout(workspace.name)
        answered = channel.run([*layout.tool("cat"), "/proc/self/uid_map"])
        assert (
            answered.code == 0 and str(workspace.slot * sizes.TEST_RANGE) in answered.out.decode()
        )
    finally:
        channel.close()


def test_the_terminal_runs_a_shell_in_it(podman, workspace):
    layout = podman.layout(workspace.name)
    ok(podman.exec(workspace.name, [*layout.tool("mkdir"), "-p", f"{layout.run}/{rt.TERMINALS}"]))
    pidfile = f"{layout.run}/{rt.TERMINALS}/{'0' * 15}1.pid"
    launch = podman.terminal_argv(workspace.name, layout, pidfile)
    main, side = pty.openpty()
    shell = subprocess.Popen(launch.argv, stdin=side, stdout=side, stderr=side, env=launch.env)
    os.close(side)
    try:
        os.write(main, b"echo $((6*7))\r")
        seen = b""
        deadline = time.monotonic() + 60
        while b"42\r\n" not in seen and time.monotonic() < deadline:
            if select.select([main], [], [], 1)[0]:
                seen += os.read(main, 4096)
        assert b"42\r\n" in seen, seen
        os.write(main, b"exit\r")
        assert shell.wait(timeout=60) == 0
    finally:
        if shell.poll() is None:
            shell.kill()
        os.close(main)


BUSYBOX = f"{rt.TOOLS_MOUNT}/bin/busybox"
# A user and UTS namespace made in the container, whose hostname is then set.
UTS = [BUSYBOX, "unshare", "-U", "-r", "-u", BUSYBOX, "hostname", "nested"]


def test_a_uts_namespace_the_workspace_makes_takes_a_hostname_and_its_own_does_not(
    podman, workspace, sign_in
):
    assert ok(podman.exec(workspace.name, UTS)) == ""
    own = podman.exec(workspace.name, [BUSYBOX, "hostname", "other"])
    assert own.code != 0 and b"not permitted" in own.err  # the kernel's, in its own
    refused = podman.exec(sign_in, UTS)
    assert refused.code != 0 and b"not permitted" in refused.err  # Podman's default profile


FRESH_PROC = ["unshare", "-U", "-r", "-m", "-p", "-f", "--mount-proc", "busybox", "sh", "-c",
              "echo $$; busybox ls /proc"]  # fmt: skip


def covered(mountinfo: str) -> list[str]:
    """The mount points in /proc, below /proc itself."""
    return [
        line.split()[4] for line in mountinfo.splitlines() if line.split()[4].startswith("/proc/")
    ]


def test_its_proc_is_unmasked_so_a_pid_namespace_it_makes_mounts_a_fresh_one(
    podman, workspace, sign_in
):
    busybox = f"{rt.TOOLS_MOUNT}/bin/busybox"
    assert covered(ok(podman.exec(workspace.name, ["cat", "/proc/self/mountinfo"]))) == []
    assert covered(ok(podman.exec(sign_in, [busybox, "cat", "/proc/self/mountinfo"])))  # control
    said = ok(podman.exec(workspace.name, [busybox, *FRESH_PROC])).split()
    assert said[0] == "1" and len([w for w in said[1:] if w.isdigit()]) <= 3
    refused = podman.exec(sign_in, [busybox, *FRESH_PROC])
    assert refused.code != 0  # control: the kernel refuses a /proc where parts are covered


# --- what each of the two options is for, shown by a workspace without it ---------------------

WRAPPER = f"{rt.TOOLS_MOUNT}/podman/bin/podman"


def nested_run(runtime, name: str) -> rt.Result:
    """Root's podman in the workspace importing a busybox and running it."""
    from tests.container.test_nest_in_podman import tarball

    work = runtime.layout(name).work
    imported = runtime.exec(name, [WRAPPER, "import", "-", "localhost/rcman-test-bb"],
                            stdin=tarball(), cwd=work, timeout=600)  # fmt: skip
    assert imported.code == 0, imported  # the import runs nothing
    argv = [WRAPPER, "run", "--rm", "--pull=never", "localhost/rcman-test-bb", "busybox", "true"]
    return runtime.exec(name, argv,
                        cwd=work, timeout=600)  # fmt: skip


def test_a_workspace_made_without_the_unmask_has_a_covered_proc_and_runs_no_nested_container(
    podman, monkeypatch
):
    monkeypatch.setattr(rt, "UNMASK_PROC", "--security-opt=label=disable")  # harmless here
    spec = rt.Spec(rt.container_name(713), slot=2, workspace=713, person=2)
    podman.create(spec)
    try:
        assert covered(ok(podman.exec(spec.name, ["cat", "/proc/self/mountinfo"])))
        assert podman.exec(spec.name, [BUSYBOX, *FRESH_PROC]).code != 0
        failed = nested_run(podman, spec.name)
        assert failed.code != 0 and b"proc" in failed.err, failed
    finally:
        podman.remove(spec.name)
        podman.remove_account(spec.person)


@pytest.fixture
def default_profile_tools(engine_tools):
    """The tools with Podman's own seccomp profile in place of the workspaces'."""
    import shutil

    from devtools import held_folders as hf
    from helpers import seccomp

    with hf.held("rctools-") as folder:
        shutil.copytree(engine_tools, folder, symlinks=True, dirs_exist_ok=True)
        shutil.copyfile(seccomp.PODMAN_DEFAULT, folder / seccomp.NAME)
        os.chmod(folder, 0o755)
        os.chmod(folder / seccomp.NAME, 0o644)
        yield folder


def test_a_workspace_with_podmans_own_profile_names_no_uts_namespace_and_runs_no_nested_container(
    podman, default_profile_tools
):
    from tests.container.conftest import engine_runtime

    config = podman.config
    runtime = engine_runtime(ps.Engine(config.socket.parent), config.image,
                             default_profile_tools, config.claude_store,
                             config.scratch_dir)  # fmt: skip
    spec = rt.Spec(rt.container_name(714), slot=2, workspace=714, person=2)
    runtime.create(spec)
    try:
        refused = runtime.exec(spec.name, UTS)
        assert refused.code != 0 and b"not permitted" in refused.err, refused
        failed = nested_run(runtime, spec.name)
        assert failed.code != 0 and b"sethostname" in failed.err, failed
    finally:
        runtime.remove(spec.name)
        runtime.remove_account(spec.person)


# --- what the unmasked /proc lets a workspace reach ---------------------------------------------


def test_the_unmasked_proc_still_keeps_the_kernels_own_files_from_a_workspace(podman, workspace):
    name = workspace.name

    def sh(script: str) -> rt.Result:
        return podman.exec(name, ["sh", "-c", script])

    for path in ("/proc/kcore", "/proc/timer_list"):
        read = sh(f"head -c 1 {path} >/dev/null")
        assert read.code != 0 and b"Permission denied" in read.err, (path, read)
    for path, value in (("/proc/sysrq-trigger", "h"), ("/proc/sys/kernel/panic", "10")):
        written = sh(f"echo {value} > {path}")
        assert written.code != 0 and b"Permission denied" in written.err, (path, written)
    # the control: a setting of the workspace's own IPC namespace is its root's to write
    before = ok(sh("cat /proc/sys/kernel/shmmax")).strip()
    try:
        ok(sh("echo 1000000000 > /proc/sys/kernel/shmmax"))
        assert ok(sh("cat /proc/sys/kernel/shmmax")).strip() == "1000000000"
    finally:
        sh(f"echo {before} > /proc/sys/kernel/shmmax")
    # what it can read: the header of latency_stats, its own keys alone, hardware's folders
    assert ok(sh("cat /proc/latency_stats")).startswith("Latency Top version")
    assert ok(sh("cat /proc/keys")) == ""
    assert ok(sh("ls /proc/bus /proc/irq"))


def host_range(engine, slot: int) -> range:
    """The host's uids of ``slot``, by the user's rootless map."""
    shown = subprocess.run(["podman", "unshare", "cat", "/proc/self/uid_map"], capture_output=True,
                           text=True, check=True, env=ps.env(engine.folder)).stdout  # fmt: skip
    for line in shown.splitlines():
        inside, outside, count = (int(n) for n in line.split())
        start = slot * sizes.TEST_RANGE
        if inside <= start < inside + count:
            return range(outside + start - inside, outside + start - inside + sizes.TEST_RANGE)
    raise AssertionError(shown)


def processes_in(uids: range) -> list[tuple[int, bytes]]:
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                if os.stat(f"/proc/{entry}").st_uid in uids:
                    found.append((int(entry), Path(f"/proc/{entry}/cmdline").read_bytes()))
            except OSError:
                continue
    return found


def plant(podman, name: str) -> None:
    busybox = f"{rt.TOOLS_MOUNT}/bin/busybox"
    ok(podman.exec(name, [busybox, "sh", "-c",
                          f"{busybox} setsid {busybox} sleep {MARKER_SLEEP} "
                          "</dev/null >/dev/null 2>&1 &"]))  # fmt: skip


def gone(uids: range) -> list:
    deadline = time.monotonic() + GONE_WITHIN
    while (left := processes_in(uids)) and time.monotonic() < deadline:
        time.sleep(0.2)
    return left


def test_nothing_of_a_workspace_runs_once_it_is_stopped_or_removed(podman, podman_service):
    # a slot no other workspace of the session runs in
    spec = rt.Spec(rt.container_name(712), slot=4, workspace=712, person=4)
    uids = host_range(podman_service, spec.slot)
    assert processes_in(uids) == []
    podman.create(spec)
    try:
        plant(podman, spec.name)
        running = processes_in(uids)
        assert any(c == f"{rt.TOOLS_MOUNT}/bin/busybox\0sleep\0{MARKER_SLEEP}\0".encode()
                   for _, c in running), running  # fmt: skip
        podman.stop(spec.name)
        assert gone(uids) == []
        podman.start(spec.name)
        plant(podman, spec.name)
        assert any(MARKER_SLEEP.encode() in c for _, c in processes_in(uids))  # positive again
        podman.remove(spec.name)
        assert gone(uids) == []
    finally:
        podman.remove(spec.name)
        podman.remove_account(spec.person)


# --- the sizes fit -------------------------------------------------------------------------------


def suite_ids() -> dict[str, int]:
    """Every id a container suite names: each module-level ``*_ID`` integer of the modules in
    tests/container/, its test modules and the rest (clone.py's among them), found by importing
    them."""
    found = {}
    for path in sorted(CONTAINER_SUITES.glob("*.py")):
        module = importlib.import_module(f"tests.container.{path.stem}")
        for name, value in vars(module).items():
            if name.endswith("_ID") and isinstance(value, int) and not isinstance(value, bool):
                found[f"{path.stem}.{name}"] = value
    return found


def image_owners(engine, image: str, folder: Path) -> tuple[int, int]:
    """The highest uid and gid of a file in ``image``, read from the layers Podman saves."""
    archive = folder / "image.tar"
    saved = engine.cli("image", "save", "--format", "docker-archive", "-o", str(archive), image)
    assert saved.returncode == 0, saved.stderr
    top = (0, 0)
    try:
        with tarfile.open(archive) as outer:
            for member in outer.getmembers():
                if not member.name.endswith(".tar"):
                    continue
                with tarfile.open(fileobj=io.BytesIO(outer.extractfile(member).read())) as layer:
                    for entry in layer.getmembers():
                        top = (max(top[0], entry.uid), max(top[1], entry.gid))
    finally:
        archive.unlink(missing_ok=True)
    return top


def misfits(uid_map: str, ids: dict[str, int], owners: dict[str, tuple[int, int]]) -> list[str]:
    """How the suites' sizes do not fit: the slots beyond the ids the engine's map holds, or an id
    or a file owner beyond one slot's range."""
    held = set()
    for line in uid_map.splitlines():
        inside, _, count = (int(n) for n in line.split())
        held |= set(range(inside, inside + count))
    problems = []
    slots = [*sizes.PEOPLE_SLOTS, sizes.BUILDER_SLOT]
    if sizes.BUILDER_SLOT in sizes.PEOPLE_SLOTS or 0 in slots:
        problems.append("the builder's slot or slot 0 is a person's")
    for slot in slots:
        wanted = range(slot * sizes.TEST_RANGE, (slot + 1) * sizes.TEST_RANGE)
        if not set(wanted) <= held:
            problems.append(f"slot {slot} is beyond the engine's ids")
    for name, value in ids.items():
        if not 0 <= value < sizes.TEST_RANGE:
            problems.append(f"{name} = {value} is beyond a slot")
    for image, (uid, gid) in owners.items():
        if max(uid, gid) >= sizes.TEST_RANGE:
            problems.append(f"{image} holds a file of {uid}:{gid}")
    return problems


def engine_map(engine) -> str:
    return subprocess.run(["podman", "unshare", "cat", "/proc/self/uid_map"], capture_output=True,
                          text=True, check=True, env=ps.env(engine.folder)).stdout  # fmt: skip


def test_the_sizes_fit(podman_service, default_image, builder_image, tmp_path):
    ids = suite_ids()
    assert {
        "test_devcontainer_in_podman.DEV_ID",
        "test_devcontainer_in_podman.EXTRA_ID",
        "test_builder_in_podman.CHOWNED_ID",
        "test_nest_in_podman.USER_ID",
        "test_builder_in_podman.CAPABLE_ID",
        "test_builder_in_podman.SUBORDINATE_ID",
        "clone.DEV_ID",
    } <= set(ids)
    listed = podman_service.cli("images", "--format", "{{.ID}}")
    images = sorted(set(listed.stdout.decode().split()))
    for made in (default_image, builder_image):  # non-vacuity: the images the suites build
        made_id = podman_service.cli("image", "inspect", "--format", "{{.Id}}", made)
        assert made_id.stdout.decode().strip()[:12] in images, made
    owners = {image: image_owners(podman_service, image, tmp_path) for image in images}
    assert owners[images[0]] is not None
    assert misfits(engine_map(podman_service), ids, owners) == []


def test_the_fit_finds_a_smaller_map_and_an_image_with_a_file_beyond_a_slot(
    podman_service, tmp_path
):
    whole = engine_map(podman_service)
    assert misfits(whole, {}, {}) == []  # the control
    smaller = "0 1001 1\n1 165536 40000\n"
    assert misfits(smaller, {}, {}), smaller
    assert misfits(whole, {"x.BIG_ID": sizes.TEST_RANGE}, {})
    tarball = io.BytesIO()
    with tarfile.open(fileobj=tarball, mode="w") as tar:
        far = tarfile.TarInfo("far")
        far.uid = far.gid = sizes.TEST_RANGE + 1
        tar.addfile(far, io.BytesIO(b""))
    name = "localhost/rcman-test-far"
    made = podman_service.cli("import", "-", name, input=tarball.getvalue())
    assert made.returncode == 0, made.stderr
    try:
        owner = image_owners(podman_service, name, tmp_path)
        assert owner == (sizes.TEST_RANGE + 1, sizes.TEST_RANGE + 1)  # the plant took effect
        assert misfits(whole, {}, {name: owner}) == [
            f"{name} holds a file of {owner[0]}:{owner[1]}"
        ]
    finally:
        podman_service.cli("rmi", "--force", name)


def imported(engine, name: str) -> str:
    """An image of one empty file, imported as ``name``."""
    tarball = io.BytesIO()
    with tarfile.open(fileobj=tarball, mode="w") as tar:
        tar.addfile(tarfile.TarInfo("empty"), io.BytesIO(b""))
    made = engine.cli("import", "-", name, input=tarball.getvalue())
    assert made.returncode == 0, made.stderr
    return name


def test_the_sessions_images_survive_a_collection_and_an_image_nothing_holds_does_not(
    podman_service, podman, default_image, builder_image, system_image
):
    """A service deleting a workspace collects the images nothing refers to (runtime
    ``collect_images``); the images the session's fixtures made are held for the session, so a
    later module still has them. The twin: an image imported and not held is collected."""
    loose = imported(podman_service, "localhost/rcman-test-loose:1")
    held = ps.hold(podman_service, imported(podman_service, "localhost/rcman-test-held:1"))
    try:
        podman.collect_images(set(), set())
        for image in (default_image, builder_image, system_image, held):
            assert podman_service.cli("image", "exists", image).returncode == 0, image
        assert podman_service.cli("image", "exists", loose).returncode != 0  # the twin went
    finally:
        podman_service.cli("rm", "--force", ps.holder_of(held))
        podman_service.cli("rmi", "--force", held, loose)
