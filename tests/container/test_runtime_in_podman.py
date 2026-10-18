"""The Podman runtime (backend/rcman/runtime.py) against real rootless Podman: the tests'
engine (tests/podman_service.py), with the suites' sizes (tests/container/sizes.py), the tools
as the installer writes them, an empty Claude store and the default workspace image
(tests/container/conftest.py). The same contract the host runtime keeps
(tests/runtime_contract.py) runs here first.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest
from rcman import channel as ch
from rcman import runtime as rt

from tests import runtime_contract
from tests.container import sizes

# The most memory one channel's podman client may hold: one is open per workspace with a
# server, so it bounds what rcman's channels cost.
CLIENT_MAX_RSS_BYTES = 64 * 1024 * 1024
# The workspace ids this module's own workspaces have, clear of the other modules'.
FIRST_WORKSPACE = 500


def spec(n: int) -> rt.Spec:
    """The n-th workspace a test makes, of a person of its own numbered as the workspace is, in
    one of the people's slots in turn: the engine holds a few slots, not every one rcman has."""
    slots = sizes.PEOPLE_SLOTS
    slot = slots[n % len(slots)]
    workspace = FIRST_WORKSPACE + n
    return rt.Spec(
        name=rt.container_name(workspace), slot=slot, workspace=workspace, person=workspace
    )


@pytest.fixture(scope="module", autouse=True)
def nothing_left(podman):
    """What the module made goes when it ends: the engine is the session's, and another module
    may give the same slot to a person of the same number. The contract's cases remove their
    workspaces but leave a person's account, as a person's deletion is what removes it."""
    yield
    for name, labels in podman.labelled().items():
        if int(labels[rt.LABEL_WORKSPACE]) >= FIRST_WORKSPACE:
            podman.remove(name)
    for n in range(1, len(runtime_contract.CASES) + 100):
        podman.remove_account(spec(n).person)
        podman.remove_account(spec(n).person + 100)  # the contract's other person of a slot


@pytest.mark.parametrize("case", runtime_contract.CASES, ids=lambda case: case.__name__)
def test_podman_keeps_the_contract(podman, case):
    case(podman, spec)


@pytest.fixture
def workspace(podman):
    made = spec(2)
    podman.create(made)
    try:
        yield made.name
    finally:
        podman.remove(made.name)
        podman.remove_account(made.person)


def inspect(engine, name: str) -> dict:
    found = engine.cli("container", "inspect", name, timeout=120)
    assert found.returncode == 0, found.stderr
    return json.loads(found.stdout)[0]


def unshare_stat(engine, path: str) -> list[str]:
    """``%u %g %a`` of ``path``, as the engine's user namespace sees ids: a range's root is the
    range's base."""
    found = engine.local("unshare", "stat", "-c", "%u %g %a", path, timeout=120)
    assert found.returncode == 0, found.stderr
    return found.stdout.decode().split()


def volume_mount(engine, volume: str) -> str:
    mount = engine.cli("volume", "inspect", "--format", "{{.Mountpoint}}", volume, timeout=120)
    assert mount.returncode == 0, mount.stderr
    return mount.stdout.decode().strip()


def in_it(podman, name: str, script: str) -> rt.Result:
    layout = podman.layout(name)
    return podman.exec(name, [layout.shell, "-c", script])


def test_a_workspace_runs_in_its_persons_uid_range(podman, workspace):
    found = (
        in_it(podman, workspace, "cat /proc/self/uid_map /proc/self/gid_map").out.decode().split()
    )
    base = str(spec(2).slot * sizes.TEST_RANGE)
    assert found == ["0", base, str(sizes.TEST_RANGE), "0", base, str(sizes.TEST_RANGE)]


def test_an_exec_runs_the_images_own_program_and_answers_without_its_mark(podman, workspace):
    """The program a bare name finds on the container's PATH is the image's, as Podman runs it,
    not the mounted busybox's applet of that name; what it wrote and its code are its own, and
    the mark the exec writes first (rt.EXEC_MARK) is not in the answer."""
    found = podman.exec(workspace, ["stat", "--version"], again=True)
    assert found.code == 0 and found.out.startswith(b"stat (GNU coreutils)"), found
    said = podman.exec(workspace, [podman.layout(workspace).shell, "-c", "echo e >&2; exit 4"])
    assert said == rt.Result(4, b"", b"e\n")
    missing = podman.exec(workspace, ["no-such-program"])
    assert missing.code == 127 and b"no-such-program" in missing.err, missing


def test_its_volume_belongs_to_its_ranges_root_and_no_one_else(podman_service, workspace):
    base = spec(2).slot * sizes.TEST_RANGE
    owner = unshare_stat(podman_service, volume_mount(podman_service, workspace))
    assert owner == [str(base), str(base), "700"]


def test_a_workspace_has_an_init_and_publishes_nothing(podman_service, podman, workspace):
    found = inspect(podman_service, workspace)
    assert found["HostConfig"]["Init"] is True
    assert not found["HostConfig"]["PortBindings"]
    assert podman_service.cli("port", workspace).stdout.strip() == b""
    first = in_it(podman, workspace, "cat /proc/1/cmdline").out
    assert b"init" in first.split(b"\0")[0]


def test_the_tools_and_the_claude_store_are_read_only_and_the_runtime_folder_a_tmpfs(
    podman, workspace
):
    for folder in (rt.TOOLS_MOUNT, rt.CLAUDE_MOUNT):
        assert in_it(podman, workspace, f"touch {folder}/x").code != 0, folder
    assert in_it(podman, workspace, f"touch {rt.VOLUME_MOUNT}/x").code == 0  # positive control
    mounts = in_it(podman, workspace, "cat /proc/mounts").out.decode().splitlines()
    assert any(line.split()[1:3] == [rt.RUN_MOUNT, "tmpfs"] for line in mounts), mounts


def test_an_unreachable_socket_is_unavailable_not_missing(podman_service, podman):
    nowhere = rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/0/nowhere.sock"),
            image="x",
            tools_dir=podman.config.tools_dir,
            claude_store=podman.config.claude_store,
        )
    )
    with pytest.raises(rt.RuntimeUnavailable):
        nowhere.exists("rcman-ws-1")


def test_the_channels_latency_through_podman(podman, workspace):
    channel = podman.channel(workspace)
    try:
        true = podman.layout(workspace).tool("wc") + ["-c", "/dev/null"]
        channel.run(true)
        times = []
        for _ in range(200):
            started = time.perf_counter()
            channel.run(true)
            times.append(time.perf_counter() - started)
        times.sort()
        assert times[189] < ch.LATENCY_P95_SECONDS, times[189]
    finally:
        channel.close()


def test_a_channels_podman_client_holds_little_memory(podman, workspace):
    channel = podman.channel(workspace)
    try:
        channel.run(podman.layout(workspace).tool("wc") + ["-c", "/dev/null"])
        status = Path(f"/proc/{channel.process.pid}/status").read_text()
        line = next(line for line in status.splitlines() if line.startswith("VmRSS:"))
        rss = int(line.split()[1])
        assert 0 < rss * 1024 < CLIENT_MAX_RSS_BYTES, rss
    finally:
        channel.close()


# --- a person's Claude sign-in --------------------------------------------------------


def credentials_in(layout: rt.Layout) -> str:
    return f"{layout.config}/{rt.CREDENTIALS}"


def test_a_persons_credentials_file_is_their_ranges_roots_alone(podman_service, podman, workspace):
    who = spec(2)
    mount = volume_mount(podman_service, rt.account_volume(who.person))
    base = who.slot * sizes.TEST_RANGE
    assert unshare_stat(podman_service, f"{mount}/{rt.CREDENTIALS}") == [
        str(base),
        str(base),
        "600",
    ]
    labels = podman_service.cli(
        "volume", "inspect", "--format", "{{json .Labels}}", rt.account_volume(who.person)
    )
    assert json.loads(labels.stdout) == {rt.LABEL_PERSON: str(who.person)}
    seen = in_it(podman, workspace, f"stat -c '%u %a' {credentials_in(podman.layout(workspace))}")
    assert seen.out.split() == [b"0", b"600"]  # root's, as the workspace sees it


def test_a_rename_over_the_bound_file_is_refused_busy_and_a_write_in_place_is_shared(
    podman, workspace
):
    """Why the fake claude, and the real one as recorded, write in place when a rename
    fails: the file is a bind mount, and a rename over a mount point is EBUSY."""
    target = credentials_in(podman.layout(workspace))
    renamed = in_it(podman, workspace, f"echo new > {target}.new && mv {target}.new {target}")
    assert renamed.code != 0 and b"busy" in renamed.err.lower(), renamed
    assert in_it(podman, workspace, f"printf kept > {target}").code == 0  # positive control
    assert podman.read_account(spec(2).person, spec(2).slot) == b"kept"


def test_a_sign_in_container_removes_itself_when_its_time_is_up(podman):
    who = spec(3)
    name = podman.start_sign_in(who.person, who.slot, seconds=3)
    try:
        assert podman.exists(name)  # positive control
        deadline = time.monotonic() + 60
        while podman.exists(name):
            assert time.monotonic() < deadline, "the sign-in container is still there"
            time.sleep(0.5)
    finally:
        podman.end_sign_in(name)
        podman.remove_account(who.person)


# --- a person's git account -----------------------------------------------------------


def test_a_persons_git_folder_and_its_files_are_their_ranges_roots_alone(
    podman_service, podman, workspace
):
    who = spec(2)
    podman.put_account(who.person, who.slot, "credential", b"password=x\n")
    base = str(who.slot * sizes.TEST_RANGE)
    folder = f"{volume_mount(podman_service, rt.account_volume(who.person))}/{rt.GIT_DIR}"
    assert unshare_stat(podman_service, folder) == [base, base, "700"]
    assert unshare_stat(podman_service, f"{folder}/credential") == [base, base, "600"]
    layout = podman.layout(workspace)
    seen = in_it(podman, workspace, f"stat -c '%u %a' {layout.home}/{rt.GIT_HOME}")
    assert seen.out.split() == [b"0", b"700"]  # root's, as the workspace sees it
    seen = in_it(podman, workspace, f"stat -c '%u %a' {layout.home}/{rt.GIT_HOME}/credential")
    assert seen.out.split() == [b"0", b"600"]


def test_the_git_folder_is_bound_and_a_rename_inside_it_works(podman_service, podman, workspace):
    """Unlike the credentials file, which is one bound file and so cannot be replaced by a
    rename, the folder is a mount point of its own and what is in it can be."""
    who = spec(2)
    layout = podman.layout(workspace)
    target = f"{layout.home}/{rt.GIT_HOME}"
    mounted = [
        m for m in inspect(podman_service, workspace)["Mounts"] if m["Destination"] == target
    ]
    own = volume_mount(podman_service, rt.account_volume(who.person))
    assert [m["Source"] for m in mounted] == [f"{own}/{rt.GIT_DIR}"]
    renamed = in_it(podman, workspace, f"cd {target} && echo new > t.new && mv t.new t")
    assert renamed.code == 0, renamed
    podman.put_account(who.person, who.slot, "config", b"[user]\n")  # a rename, from outside
    assert in_it(podman, workspace, f"cat {target}/config").out == b"[user]\n"
    assert in_it(podman, workspace, f"cat {target}/t").out == b"new\n"  # nothing else lost


def test_a_git_file_is_written_with_no_container_left_and_the_data_not_on_a_command_line(
    podman_service, podman
):
    who = spec(3)
    secret = b"password=SECRET-FOR-THE-PODMAN-TEST\n"
    before = podman_service.cli("ps", "--all", "--format", "{{.Names}}").stdout.split()
    try:
        podman.put_account(who.person, who.slot, "credential", secret)
        left = podman_service.cli("ps", "--all", "--format", "{{.Names}}")
        assert left.stdout.split() == before  # the container that wrote it removed itself
        own = volume_mount(podman_service, rt.account_volume(who.person))
        assert podman_service.local("unshare", "cat", f"{own}/{rt.GIT_DIR}/credential").stdout == (
            secret
        )
        commands = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True, errors="replace"
        ).stdout
        assert "SECRET-FOR-THE-PODMAN-TEST" not in commands
    finally:
        podman.remove_account(who.person)
