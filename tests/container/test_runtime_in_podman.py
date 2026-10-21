"""The Podman runtime (backend/rcman/runtime.py) against real rootless Podman: the tests'
engine (tests/podman_service.py), with the suites' sizes (tests/container/sizes.py), the tools
as the installer writes them, an empty Claude store and the default workspace image
(tests/container/conftest.py). The same contract the host runtime keeps
(tests/runtime_contract.py) runs here first.
"""

import dataclasses
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from rcman import channel as ch
from rcman import runtime as rt

from helpers import instructions
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


@pytest.mark.parametrize("case", runtime_contract.PODMAN_CASES, ids=lambda case: case.__name__)
def test_podman_keeps_its_own_cases(podman, case):
    case(podman, spec)


def test_every_case_of_the_contract_and_of_podmans_own_is_run_here():
    from tests.collected import collect

    module = "tests/container/test_runtime_in_podman.py"
    found = set(collect([module]))
    for test, cases in (
        ("test_podman_keeps_the_contract", runtime_contract.CASES),
        ("test_podman_keeps_its_own_cases", runtime_contract.PODMAN_CASES),
    ):
        assert cases  # non-vacuity
        named = f"{module}::{test}"
        ran = {n.removeprefix(named) for n in found if n.startswith(named + "[")}
        assert ran == {f"[{case.__name__}]" for case in cases}, test


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


# --- what Claude is told: a file of the tools bound read-only at Claude Code's managed path ------
#
# These hold the bind create makes, of the file of the tools the workspace's image calls for: where
# the image has no /etc/claude-code, where the ids are swapped, over an image's own folder, and
# what a rename of the file on the host, as the installer writes it, comes to.

MANAGED = instructions.MANAGED
BUSYBOX = f"{rt.TOOLS_MOUNT}/bin/busybox"
OWNED_ID = 1000


def told_text(runtime: rt.PodmanRuntime, name: str) -> bytes:
    """The file ``name`` of the instructions in the runtime's tools, as the installer wrote it."""
    return (runtime.config.tools_dir / instructions.FOLDER / name).read_bytes()


@pytest.fixture
def own_tools(podman_service, default_image, engine_tools, engine_store, engine_scratch):
    """A runtime as ``podman`` is, but with a copy of the tools of its own, whose instructions a
    test may write."""
    from devtools import held_folders as hf
    from tests.container.conftest import engine_runtime

    with hf.held("rctold-") as folder:
        shutil.copytree(engine_tools, folder, symlinks=True, dirs_exist_ok=True)
        os.chmod(folder, 0o755)
        yield engine_runtime(podman_service, default_image, folder, engine_store, engine_scratch)


def image_of_files(
    engine, name: str, entries: list[tuple[str, bytes, str, int]], labels: tuple[str, ...] = ()
) -> str:
    """An image of the host's static busybox imported as ``name``, with ``entries``: a path, the
    content of a file (or b"" for what is not one), the kind (file, folder or link) and, for a
    link, its target as the content; labelled ``labels``, each ``key=value``."""
    import io
    import tarfile

    from installer import install as ins

    kinds = {"file": tarfile.REGTYPE, "folder": tarfile.DIRTYPE, "link": tarfile.SYMTYPE}
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        tar.add(ins.BUSYBOX, arcname="bin/busybox")
        for path, content, kind, mode in entries:
            entry = tarfile.TarInfo(path)
            entry.type, entry.mode = kinds[kind], mode
            if kind == "link":
                entry.linkname = content.decode()
                tar.addfile(entry)
            else:
                entry.size = len(content) if kind == "file" else 0
                tar.addfile(entry, io.BytesIO(content) if kind == "file" else None)
    changes = [f"--change=LABEL {label}" for label in labels]
    made = engine.cli("import", *changes, "-", name, input=data.getvalue(), timeout=300)
    assert made.returncode == 0, made.stderr
    return name


def read_in(podman, name: str, path: str) -> rt.Result:
    return podman.exec(name, [BUSYBOX, "cat", path])


@pytest.mark.parametrize("image", ["default", "without-etc", "labelled"])
def test_the_managed_file_is_bound_where_the_image_has_no_etc_claude_code(
    podman_service, podman, image
):
    """The default image has none (tests/container/test_nest_in_podman.py); the others have no
    /etc at all, one of them labelled as the default image is, as an image built from it is.
    Each is told what its image calls for."""
    name, told = None, instructions.DEFAULT_TOOLS
    if image == "without-etc":
        name = image_of_files(podman_service, "localhost/rcman-test-no-etc", [])
        told = instructions.OWN_IMAGE
    elif image == "labelled":
        labels = (f"{instructions.TOOLS_LABEL}=1",)
        name = image_of_files(podman_service, "localhost/rcman-test-labelled", [], labels)
        assert podman.image_facts(name).labels.get(instructions.TOOLS_LABEL) == "1"  # the plant
    # the files differ, so that which one is bound is known
    assert len({told_text(podman, n) for n in instructions.FILES}) == len(instructions.FILES)
    made = dataclasses.replace(spec(40), image=name)
    try:
        podman.create(made)
        found = podman.exec(made.name, [BUSYBOX, "ls", "-l", "/etc/claude-code"])
        assert found.code == 0 and b"CLAUDE.md" in found.out, found  # Podman made the folder
        assert read_in(podman, made.name, MANAGED).out == told_text(podman, told)
        written = podman.exec(made.name, [BUSYBOX, "sh", "-c", f"echo x > {MANAGED}"])
        assert written.code != 0 and b"Read-only" in written.err, written
        assert read_in(podman, made.name, MANAGED).out == told_text(podman, told)
    finally:
        podman.remove(made.name)
        podman.remove_account(made.person)
        if name:
            podman_service.cli("rmi", "--force", name)


def test_the_managed_file_is_readable_where_the_ids_are_swapped(podman_service, podman):
    name = image_of_files(podman_service, "localhost/rcman-test-swapped", [])
    made = dataclasses.replace(spec(41), image=name, user=f"{OWNED_ID}:{OWNED_ID}", uid=OWNED_ID,
                               gid=OWNED_ID)  # fmt: skip
    try:
        podman.create(made)
        shown = podman.exec(made.name, [BUSYBOX, "id", "-u"])
        assert shown.out.strip() == str(OWNED_ID).encode()  # the control: the user is not root
        assert read_in(podman, made.name, MANAGED).out == told_text(podman, instructions.OWN_IMAGE)
    finally:
        podman.remove(made.name)
        podman.remove_account(made.person)
        podman_service.cli("rmi", "--force", name)


def test_an_images_own_managed_settings_stay_and_its_own_claude_md_is_replaced(
    podman_service, podman
):
    entries = [
        ("etc", b"", "folder", 0o755),
        ("etc/claude-code", b"", "folder", 0o755),
        ("etc/claude-code/managed-settings.json", b'{"own": true}\n', "file", 0o644),
        (MANAGED[1:], b"the image's own\n", "file", 0o644),
    ]
    name = image_of_files(podman_service, "localhost/rcman-test-own-managed", entries)
    made = dataclasses.replace(spec(42), image=name)
    try:
        assert podman.image_facts(name).managed == {  # what the image has, and is not refused for
            "/etc": instructions.IS_FOLDER,
            "/etc/claude-code": instructions.IS_FOLDER,
            MANAGED: instructions.IS_FILE,
        }
        podman.create(made)
        settings = read_in(podman, made.name, "/etc/claude-code/managed-settings.json")
        assert settings.out == b'{"own": true}\n', settings
        assert read_in(podman, made.name, MANAGED).out == told_text(podman, instructions.OWN_IMAGE)
    finally:
        podman.remove(made.name)
        podman.remove_account(made.person)
        podman_service.cli("rmi", "--force", name)


def test_a_rename_over_the_managed_file_reaches_the_workspace_at_its_next_start_not_before(
    own_tools,
):
    """What the installer's write_if_changed does: a new file renamed over the old. The bind
    holds the old file until the container is started again, and then binds the new one."""
    from installer import install as ins

    podman, made = own_tools, spec(43)
    path = podman.config.tools_dir / instructions.FOLDER / instructions.DEFAULT_TOOLS
    old = path.read_bytes()
    try:
        podman.create(made)
        assert read_in(podman, made.name, MANAGED).out == old
        before = path.stat().st_ino
        assert ins.write_if_changed(path, "rcman's new text\n", 0o644)
        assert path.stat().st_ino != before  # the plant took effect, by a rename
        assert read_in(podman, made.name, MANAGED).out == old  # not before
        podman.stop(made.name)
        podman.start(made.name)
        assert read_in(podman, made.name, MANAGED).out == b"rcman's new text\n"
    finally:
        podman.remove(made.name)
        podman.remove_account(made.person)


def test_the_default_image_has_every_command_its_instructions_name(podman):
    from tests.containers import named_commands

    fragments = Path(__file__).resolve().parents[2] / "backend" / "rcman" / instructions.FOLDER
    named = named_commands(
        "".join((fragments / f).read_text() for f in (instructions.GH, instructions.TOOLS))
    )
    assert {"gh", "make", "unzip", "docker"} <= set(named)  # the positive
    made = spec(45)
    try:
        podman.create(made)
        layout = podman.layout(made.name)
        look = [*layout.tool("env"), f"PATH={layout.path}", layout.shell, "-c", 'command -v "$1"']
        for command in named:
            found = podman.exec(made.name, [*look, "sh", command])
            assert found.code == 0, (command, found)
        absent = podman.exec(made.name, [*look, "sh", "no-such-tool"])
        assert absent.code != 0  # the control: one that is not there is not found
    finally:
        podman.remove(made.name)
        podman.remove_account(made.person)


# --- what an image has at /etc/claude-code, as the image's probe reads it ------------------------

ETC_KINDS = {
    "folder": [
        ("etc", b"", "folder", 0o755),
        ("etc/claude-code", b"", "folder", 0o755),
        (MANAGED[1:], b"own\n", "file", 0o644),
    ],
    "file": [("etc", b"", "folder", 0o755), ("etc/claude-code", b"a file\n", "file", 0o644)],
    "link": [
        ("etc", b"", "folder", 0o755),
        ("elsewhere", b"", "folder", 0o755),
        ("etc/claude-code", b"/elsewhere", "link", 0o777),
    ],
    "etc-link": [
        ("real", b"", "folder", 0o755),
        ("real/claude-code", b"", "folder", 0o755),
        ("etc", b"/real", "link", 0o777),
    ],
    "claude-md-link": [
        ("etc", b"", "folder", 0o755),
        ("etc/claude-code", b"", "folder", 0o755),
        (MANAGED[1:], b"/etc/passwd", "link", 0o777),
    ],
    "claude-md-link-file": [
        ("etc", b"", "folder", 0o755),
        ("etc/claude-code", b"", "folder", 0o755),
        ("etc/claude-code/text", b"own\n", "file", 0o644),
        (MANAGED[1:], b"/etc/claude-code/text", "link", 0o777),
    ],
    "claude-md-link-folder": [
        ("etc", b"", "folder", 0o755),
        ("etc/claude-code", b"", "folder", 0o755),
        ("etc/claude-code/folder", b"", "folder", 0o755),
        (MANAGED[1:], b"/etc/claude-code/folder", "link", 0o777),
    ],
    "absent": [("etc", b"", "folder", 0o755)],
}
# What the probe reads at /etc, /etc/claude-code and MANAGED, image by image; None where there is
# nothing, or where nothing is read, below what is not a folder. podman cp follows a link, at the
# end of the path as along it, so a link reads as what it names: a folder as a folder, a file as a
# file, one that names nothing as nothing. A link is never seen as one, and what is refused is
# judged by what a link names; were Podman to stop following them, the link cases here would read
# a link and fail.
FOLDER_IS, FILE_IS = instructions.IS_FOLDER, instructions.IS_FILE
ETC_SEEN = {
    "folder": (FOLDER_IS, FOLDER_IS, FILE_IS),
    "file": (FOLDER_IS, FILE_IS, None),
    "link": (FOLDER_IS, FOLDER_IS, None),
    "etc-link": (FOLDER_IS, FOLDER_IS, None),
    "claude-md-link": (FOLDER_IS, FOLDER_IS, None),
    "claude-md-link-file": (FOLDER_IS, FOLDER_IS, FILE_IS),
    "claude-md-link-folder": (FOLDER_IS, FOLDER_IS, FOLDER_IS),
    "absent": (FOLDER_IS, None, None),
}


@pytest.mark.parametrize("kind", sorted(ETC_KINDS))
def test_what_an_image_has_at_etc_claude_code_is_read_by_the_image_probe(
    podman_service, podman, kind
):
    name = image_of_files(podman_service, f"localhost/rcman-test-etc-{kind}", ETC_KINDS[kind])
    try:
        facts = podman.image_facts(name)
        seen = tuple(facts.managed.get(path) for path in instructions.PATHS)
        assert seen == ETC_SEEN[kind], (kind, seen)
    finally:
        podman_service.cli("rmi", "--force", name)
