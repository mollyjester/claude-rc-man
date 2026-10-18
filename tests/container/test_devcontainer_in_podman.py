"""A workspace made from a repository's devcontainer.json, against real rootless Podman.

On the tests' engine (tests/podman_service.py), as ``tests/container/test_github_in_podman.py``
runs, whose stand-in GitHub, workspace image (the default image with the stand-in's authority),
network, person and ``Workspaces`` it shares. What the host-runtime tests could only assume is
checked here against the real thing: how Podman is asked for an image, its user and its files, the
uid map that makes a ``remoteUser`` the host user root is in the person's other workspaces, the
tmpfs's owner, the shared sign-in written in place from both kinds of workspace, and the
lifecycle commands across a restart of rcman.

The images are derived from the workspace image on the engine, by name (no registry holds them);
the repositories are published on the stand-in as ``octo/<name>``.
"""

import concurrent.futures
import dataclasses
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from rcman import claude_server, db, people
from rcman import runtime as rt
from rcman.problems import DEVCONTAINER_MESSAGES
from rcman.repository import Repository
from rcman.workspaces import Workspaces

from devtools import fake_claude as fake
from tests.container import sizes
from tests.container import test_github_in_podman as shared
from tests.container.conftest import cli, on_host

ADDRESS, EMAIL, TOKEN = shared.ADDRESS, shared.EMAIL, shared.TOKEN
exists, gone, settled = shared.exists, shared.gone, shared.settled
# The fixtures of that module, which pytest finds here by these names.
account, clones, github_com, holder, image = (
    shared.account,
    shared.clones,
    shared.github_com,
    shared.holder,
    shared.image,
)
parts, person, pki, podman, service = (
    shared.parts,
    shared.person,
    shared.pki,
    shared.podman,
    shared.service,
)

# The user the derived image runs: an id that is not the ordinary first user's, a group of its
# own and a supplementary one.
DEV_USER, DEV_ID, EXTRA_GROUP, EXTRA_ID = "dev", 1234, "extra", 5000
# What `id` prints for the user: its own group and the supplementary one.
WHO_AM_I = (
    f"uid={DEV_ID}({DEV_USER}) gid={DEV_ID}({DEV_USER}) "
    f"groups={DEV_ID}({DEV_USER}),{EXTRA_ID}({EXTRA_GROUP})"
)
# The workspace ids the tests that make containers without the service use, clear of the
# contract's, the github module's and the service's own (numbered from 1).
ROOT_WORKSPACE, SWAPPED_WORKSPACE, OTHER_WORKSPACE = 710, 711, 712

DOCKERFILE = f"""\
FROM {{base}}
RUN groupadd -g {DEV_ID} {DEV_USER} && groupadd -g {EXTRA_ID} {EXTRA_GROUP} \\
 && useradd -u {DEV_ID} -g {DEV_USER} -G {EXTRA_GROUP} -m -s /bin/bash {DEV_USER}
RUN mkdir -p /opt/dev/bin && printf '#!/bin/sh\\necho devtool\\n' > /opt/dev/bin/devtool \\
 && chmod 755 /opt/dev/bin/devtool
ENV FROM_IMAGE=yes PATH=/opt/dev/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
"""
LABELLED = """\
FROM {base}
LABEL devcontainer.metadata='[{"remoteUser": "dev", "containerEnv": {"FROM_LABEL": "yes"}, \
"postCreateCommand": "echo label >> from-the-label"}]'
"""


def build(podman, context: Path, dockerfile: str, name: str) -> str:
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(dockerfile)
    built = cli(podman, "build", "-q", "--pull=never", "-t", name, str(context))
    assert built.returncode == 0, built.stdout + built.stderr
    return name


def hold(podman, image: str) -> str:
    """A container made from ``image`` that is never started and is no workspace's (it has no
    label), and its name. Deleting a workspace collects the images nothing refers to, and an
    image made or pulled by hand for a test is one of them; the collection never removes an
    image a container is made from, so this keeps it for the tests that use it."""
    name = "rcman-test-hold-" + "".join(c if c.isalnum() else "-" for c in image)
    made = cli(
        podman, "create", f"--name={name}", "--pull=never", "--network=none",
        "--entrypoint=/bin/true", image,
    )  # fmt: skip
    assert made.returncode == 0, made.stderr
    return name


def release(podman, *names: str) -> None:
    for name in names:
        cli(podman, "rm", "--force", "--ignore", name)


@pytest.fixture(scope="module")
def dev_image(podman, image, tmp_path_factory) -> str:
    """The workspace image with a user whose ids are not the first ordinary user's."""
    name = f"localhost/rcman-test-dev:{os.getpid()}"
    build(podman, tmp_path_factory.mktemp("dev-image"), DOCKERFILE.format(base=image), name)
    holding = hold(podman, name)
    yield name
    release(podman, holding)
    cli(podman, "rmi", "--force", name)


@pytest.fixture(scope="module")
def labelled_image(podman, dev_image, tmp_path_factory) -> str:
    """The same, carrying a devcontainer.metadata label."""
    name = f"localhost/rcman-test-labelled:{os.getpid()}"
    dockerfile = LABELLED.replace("{base}", dev_image)
    build(podman, tmp_path_factory.mktemp("labelled-image"), dockerfile, name)
    holding = hold(podman, name)
    yield name
    release(podman, holding)
    cli(podman, "rmi", "--force", name)


@pytest.fixture(scope="module")
def locked_image(podman, dev_image, tmp_path_factory) -> str:
    """The same, whose working folder is one only root can enter."""
    name = f"localhost/rcman-test-locked:{os.getpid()}"
    dockerfile = f"FROM {dev_image}\nRUN mkdir /locked && chmod 700 /locked\nWORKDIR /locked\n"
    build(podman, tmp_path_factory.mktemp("locked-image"), dockerfile, name)
    holding = hold(podman, name)
    yield name
    release(podman, holding)
    cli(podman, "rmi", "--force", name)


@pytest.fixture(scope="module", autouse=True)
def persons_volume_is_removed(podman, person):
    """The person's volume of credentials and git files, which the first workspace made."""
    yield
    podman.remove_account(person[0].id)


def images(podman) -> set[str]:
    found = cli(podman, "images", "--all", "--format", "{{.ID}}")
    assert found.returncode == 0, found.stderr
    return set(found.stdout.split())


def left_of(podman, name: str) -> list[str]:
    """What is left of the workspace by this container name: container and volume."""
    return [kind for kind in ("container", "volume") if exists(podman, kind, name)]


def probes(podman) -> list[str]:
    found = cli(podman, "ps", "--all", f"--filter=label={rt.LABEL_PROBE}", "--format", "{{.Names}}")
    assert found.returncode == 0, found.stderr
    return found.stdout.split()


@pytest.fixture
def direct(podman, person):
    """Containers made without the service: the specs, removed afterwards, container and
    volume."""
    made: list[str] = []

    def make(spec: rt.Spec) -> str:
        podman.create(spec)
        made.append(spec.name)
        return spec.name

    yield make
    for name in made:
        podman.remove(name)
    for name in made:
        assert left_of(podman, name) == []


def spec_for(workspace: int, person, **more) -> rt.Spec:
    who = person[0]
    return rt.Spec(rt.container_name(workspace), who.slot, workspace, who.id, **more)


def swapped(workspace: int, person, image: str, user: str = DEV_USER, **more) -> rt.Spec:
    return spec_for(
        workspace, person, image=image, user=user, uid=DEV_ID, gid=DEV_ID,
        groups=more.pop("groups", ()), **more,
    )  # fmt: skip


def say(podman, name: str, script: str) -> rt.Result:
    """``script`` run by the container's shell as the container's user."""
    return podman.exec(name, [podman.layout(name).shell, "-c", script])


def out(result: rt.Result) -> str:
    assert result.code == 0, result
    return result.out.decode().strip()


def volume_path(podman, name: str) -> str:
    return cli(podman, "volume", "inspect", "--format", "{{.Mountpoint}}", name).stdout.strip()


def inspect(podman, name: str) -> dict:
    found = cli(podman, "container", "inspect", name)
    assert found.returncode == 0, found.stderr
    return json.loads(found.stdout)[0]


# --- what Podman says about an image, and how an image is pulled ---------------------------


def test_the_facts_of_an_image_are_read_from_podmans_own_words_without_running_it(
    podman, dev_image, labelled_image
):
    before = images(podman)
    facts = podman.image_facts(dev_image)
    assert f"{DEV_USER}:x:{DEV_ID}:{DEV_ID}:" in facts.passwd and "root:x:0:0:" in facts.passwd
    assert f"{EXTRA_GROUP}:x:{EXTRA_ID}:{DEV_USER}" in facts.group
    assert facts.env["FROM_IMAGE"] == "yes" and facts.env["PATH"].startswith("/opt/dev/bin:")
    assert facts.user == "" and "devcontainer.metadata" not in facts.labels
    labelled = podman.image_facts(labelled_image)
    entries = json.loads(labelled.labels["devcontainer.metadata"])
    assert entries[0]["remoteUser"] == DEV_USER  # the label, as the image carries it
    assert probes(podman) == [] and images(podman) == before  # nothing was left, nothing was copied
    with pytest.raises(rt.RuntimeFailed):
        podman.image_facts("localhost/rcman-test-not-there:1")
    assert probes(podman) == []


def test_the_user_an_image_is_set_to_run_as_is_read(podman, tmp_path, image):
    name = build(
        podman, tmp_path, f"FROM {image}\nUSER nobody\n", f"localhost/rcman-test-user:{os.getpid()}"
    )
    try:
        assert podman.image_facts(name).user == "nobody"
    finally:
        cli(podman, "rmi", "--force", name)


def test_a_file_the_image_does_not_have_is_read_as_empty_by_the_words_podman_really_uses(
    podman, tmp_path, image
):
    """Only podman saying the file is not there means the image has none; this is that, said by
    the real one, for an image with a passwd file and no group file."""
    name = build(
        podman,
        tmp_path,
        f"FROM {image}\nRUN rm -f /etc/group /etc/group-\n",
        f"localhost/rcman-test-nogroup:{os.getpid()}",
    )
    try:
        facts = podman.image_facts(name)
        assert facts.passwd and facts.group == ""
    finally:
        cli(podman, "rmi", "--force", name)


def test_a_leftover_probe_container_is_found_and_removed(podman, dev_image):
    made = cli(podman,
        "create", f"--label={rt.LABEL_PROBE}=1", "--name=rcman-probe-left", "--pull=never",
        "--entrypoint=/bin/true", dev_image,
    )  # fmt: skip
    assert made.returncode == 0, made.stderr
    try:
        assert probes(podman) == ["rcman-probe-left"]  # positive control: the filter finds it
        podman.clean_probes()
        assert probes(podman) == []
    finally:
        cli(podman, "rm", "--force", "--ignore", "rcman-probe-left")


def test_an_image_from_a_registry_is_pulled_and_each_line_podman_prints_is_passed_on(podman):
    lines: list[str] = []
    podman.pull("docker.io/library/alpine:3.20", lines.append)  # cached; the registry is asked
    assert lines, "a pull printed nothing"
    assert all(line == line.strip() and line for line in lines)  # lines, not carriage returns


def test_an_image_that_is_only_here_is_taken_without_a_registry(podman, dev_image):
    lines: list[str] = []
    podman.pull(dev_image, lines.append)  # no registry holds localhost/...
    assert podman.image_facts(dev_image).passwd  # and it is the image


def test_an_image_nowhere_is_a_failure_that_says_what_podman_said(podman):
    with pytest.raises(rt.RuntimeFailed) as failed:
        podman.pull("localhost/rcman-test-not-there:1")
    assert str(failed.value).strip()
    assert cli(podman, "image", "exists", "localhost/rcman-test-not-there:1").returncode != 0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_pull_that_is_cancelled_stops_podman(podman, tmp_path):
    """A pull that hangs is cancelled while it runs: the call fails and the podman it ran has
    ended, which is looked at by its process, not taken from the call's failing. The program
    run is a wrapper that notes its process and then is a pull that never ends."""
    wrapper = tmp_path / "podman"
    pid_file = tmp_path / "pid"
    wrapper.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 3600\n")
    wrapper.chmod(0o755)
    hanging = rt.PodmanRuntime(dataclasses.replace(podman.config, podman=str(wrapper)))
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    began = time.monotonic()
    with pytest.raises(rt.RuntimeFailed):
        hanging.pull("docker.io/library/alpine:3.20", cancel=cancel)
    assert time.monotonic() - began < 60
    pid = int(pid_file.read_text())  # positive control: it ran
    assert not alive(pid)
    # And the real one, cancelled before it starts, fails without a pull being made.
    set_already = threading.Event()
    set_already.set()
    with pytest.raises(rt.RuntimeFailed):
        podman.pull("docker.io/library/alpine:3.20", cancel=set_already)


# --- the uid swap map, the user and the tmpfs ----------------------------------------------


def test_a_container_of_the_users_own_swaps_the_users_id_with_roots_and_nothing_else(
    podman, person, direct, dev_image
):
    name = direct(swapped(SWAPPED_WORKSPACE, person, dev_image, groups=()))
    base_id = person[0].slot * sizes.TEST_RANGE
    found = out(say(podman, name, "cat /proc/self/uid_map")).splitlines()
    # The map's lines, as Podman shows them: the user's id is the host id root has elsewhere.
    mapped = {int(a): int(b) for a, b, _ in (line.split() for line in found)}
    assert mapped[DEV_ID] == base_id and mapped[0] == base_id + DEV_ID
    assert mapped[1] == base_id + 1
    me = out(podman.exec(name, ["id"]))
    assert me == WHO_AM_I


def test_an_image_whose_working_folder_the_user_cannot_enter_does_not_break_rcmans_commands(
    podman, person, direct, locked_image
):
    """The container starts in the volume's mount, not in the image's own folder, which here
    is root's alone: the folders are made, a command with no folder runs, and the user is who
    the plan says."""
    name = direct(swapped(SWAPPED_WORKSPACE, person, locked_image))
    found = inspect(podman, name)
    assert found["Config"]["WorkingDir"] == rt.VOLUME_MOUNT
    assert out(podman.exec(name, ["id"])) == WHO_AM_I
    assert out(say(podman, name, "pwd")) == rt.VOLUME_MOUNT
    layout = podman.layout(name)
    assert say(podman, name, f"test -d {layout.work} && test -d {layout.config}").code == 0
    assert say(podman, name, "cd /locked").code != 0  # positive control: the user cannot enter it


def test_a_user_given_by_number_keeps_its_groups_through_group_add(
    podman, person, direct, dev_image
):
    """``--user uid:gid`` drops the groups the image gives the user, which ``--group-add`` of
    the map's other id puts back."""
    by_ids = swapped(
        SWAPPED_WORKSPACE, person, dev_image, user=f"{DEV_ID}:{DEV_ID}", groups=(EXTRA_ID,)
    )
    name = direct(by_ids)
    me = out(podman.exec(name, ["id"]))
    assert me == WHO_AM_I


def test_files_an_ordinary_root_made_are_the_users_in_the_swapped_container_and_u_can_rename_them(
    podman, person, direct, dev_image
):
    name = direct(spec_for(SWAPPED_WORKSPACE, person))  # the default container, root of the range
    layout = podman.layout(name)
    work = layout.work
    made = say(podman, name, f"cd {work} && mkdir d && echo x > d/f && chmod 600 d/f && echo y > g")
    assert made.code == 0, made
    credentials = f"{layout.config}/{rt.CREDENTIALS}"
    seen = out(say(podman, name, f"stat -c '%u %g' {work}/d/f {credentials}"))
    assert seen.split() == ["0", "0", "0", "0"]
    on_volume = on_host(podman, f"{volume_path(podman, name)}/{rt.WORK}/d/f")
    base_id = str(person[0].slot * sizes.TEST_RANGE)
    assert on_volume == [base_id, base_id, "600"]

    podman.discard(name)  # the same volume, now with the user's own container
    podman.create(swapped(SWAPPED_WORKSPACE, person, dev_image))
    owner = out(say(podman, name, f"stat -c '%u %g %U %a' {work} {work}/d {work}/d/f {work}/g"))
    assert owner.splitlines() == [f"{DEV_ID} {DEV_ID} {DEV_USER} 755"] + [
        f"{DEV_ID} {DEV_ID} {DEV_USER} {mode}" for mode in ("755", "600", "644")
    ]
    # The user renames, adds and removes in the folders the other kind of root made.
    changed = say(
        podman, name,
        f"cd {work} && mv d/f d/f2 && mv g d/g && echo z > d/h && mkdir e && rm d/g",
    )  # fmt: skip
    assert changed.code == 0, changed
    assert out(say(podman, name, f"cd {work} && ls d e")).split() == ["d:", "f2", "h", "e:"]
    # Seen from the host nothing changed hands: it is still the range's base.
    assert on_host(podman, f"{volume_path(podman, name)}/{rt.WORK}/d/h")[:2] == [base_id, base_id]
    assert on_host(podman, f"{volume_path(podman, name)}/{rt.WORK}/d/f2")[:2] == [base_id, base_id]


def test_the_runtime_folder_and_the_credentials_and_git_folder_are_the_users_in_its_container(
    podman, person, direct, dev_image
):
    name = direct(swapped(SWAPPED_WORKSPACE, person, dev_image))
    layout = podman.layout(name)
    podman.put_account(person[0].id, person[0].slot, "credential", b"password=x\n")
    # The tmpfs is the user's alone (``U``), where uid= and gid= are refused.
    assert (
        out(say(podman, name, f"stat -c '%U %G %a' {rt.RUN_MOUNT}")) == f"{DEV_USER} {DEV_USER} 700"
    )
    assert say(podman, name, f"touch {rt.RUN_MOUNT}/t").code == 0
    shown = out(say(podman, name, f"stat -c '%U %a' {layout.config}/{rt.CREDENTIALS}"))
    assert shown == f"{DEV_USER} 600"
    folder = f"{layout.home}/{rt.GIT_HOME}"
    assert out(say(podman, name, f"stat -c '%U %a' {folder} {folder}/credential")).split() == [
        DEV_USER, "700", DEV_USER, "600",
    ]  # fmt: skip
    assert say(podman, name, f"cat {folder}/credential").out == b"password=x\n"
    # The folders Podman would have made as the container's root (it is the host id the user is
    # in the ordinary map) are the user's: the user writes beside a mount.
    for beside in (layout.work, layout.config, f"{layout.home}/{rt.GIT_HOME.rsplit('/', 1)[0]}"):
        assert say(podman, name, f"touch {beside}/t && rm {beside}/t").code == 0, beside
    assert out(say(podman, name, f"stat -c '%a' {rt.VOLUME_MOUNT}")) == "700"
    podman.remove_account(person[0].id)


def test_a_workspace_of_the_users_own_keeps_the_limits_of_every_workspace(
    podman, person, direct, dev_image
):
    name = direct(swapped(SWAPPED_WORKSPACE, person, dev_image))
    found = inspect(podman, name)
    assert found["HostConfig"]["Init"] is True and not found["HostConfig"]["PortBindings"]
    assert not found["HostConfig"]["Privileged"] and not found["HostConfig"]["CapAdd"]
    entrypoint = found["Config"]["Entrypoint"]
    assert entrypoint in (f"{rt.TOOLS_MOUNT}/bin/busybox", [f"{rt.TOOLS_MOUNT}/bin/busybox"])
    assert (
        found["ImageName"].endswith(dev_image.removeprefix("localhost/"))
        or dev_image in found["ImageName"]
    )
    # The image's own userland runs under rcman's busybox as the process: no init script ran,
    # and the init is the container's.
    first = say(podman, name, "cat /proc/1/cmdline").out
    assert b"init" in first.split(b"\0")[0]
    assert "sleep" in cli(podman, "top", name, "args").stdout


# --- the shared sign-in, from a root workspace and from the user's ----------------------------

# Claude Code's refresh of the credentials, as the file sees it: opened for reading and
# writing, read, truncated, written, whole texts of whatever length the new tokens make. It
# takes no lock on the file (Claude's own lock is a file beside its configuration folder, and
# each workspace has a folder of its own), so none is taken here either.
WRITER = r"""
import json, sys, time
path, who, duration, start_at, salt = sys.argv[1:6]
duration, start_at, salt = float(duration), float(start_at), int(salt)
while time.time() < start_at:  # both writers begin at one moment, however late each started
    pass
rounds = saw_other = empty = torn = 0
first, end = time.time(), start_at + duration
while time.time() < end:
    with open(path, "r+") as handle:  # in place: a rename onto the bound file is refused busy
        text = handle.read()
        if not text.strip():
            empty += 1
        else:
            try:
                found = json.loads(text)
            except ValueError:
                torn += 1
            else:
                if found.get("writer") not in (who, "start"):
                    saw_other += 1
        # lengths that differ from round to round and between the two, a shorter text after
        # a longer one among them
        pad = "x" * (50 + (rounds * 37 + salt) % 400)
        payload = json.dumps({"writer": who, "n": rounds, "claudeAiOauth": {"pad": pad}})
        handle.seek(0)
        handle.truncate(0)
        handle.write(payload)
        handle.flush()
    rounds += 1
print(json.dumps({"who": who, "rounds": rounds, "saw_other": saw_other, "empty": empty,
                  "torn": torn, "first": first, "last": time.time()}))
"""

# One in-place write of a text of the length given, as the refresh does it.
ONE_WRITE = r"""
import json, sys
path, who, length = sys.argv[1:4]
body = json.dumps({"writer": who, "claudeAiOauth": {"pad": ""}})
text = json.dumps({"writer": who, "claudeAiOauth": {"pad": "x" * (int(length) - len(body))}})
assert len(text) == int(length)
with open(path, "r+") as handle:
    handle.truncate(0)
    handle.write(text)
"""


@pytest.fixture
def two(podman, person, direct, dev_image):
    """A root workspace and the user's of the same person: their names, and the credentials
    file's path as both see it."""
    root = direct(spec_for(ROOT_WORKSPACE, person))
    own = direct(swapped(SWAPPED_WORKSPACE, person, dev_image))
    path = f"{podman.layout(root).config}/{rt.CREDENTIALS}"
    assert path == f"{podman.layout(own).config}/{rt.CREDENTIALS}"
    yield root, own, path
    podman.remove_account(person[0].id)


def test_a_root_workspace_and_the_users_read_and_write_the_credentials_file_in_place_at_once(
    podman, person, two
):
    root, own, path = two
    who = person[0]
    mount = volume_path(podman, rt.account_volume(who.id))
    base_id = str(who.slot * sizes.TEST_RANGE)
    inode = on_host(podman, f"{mount}/{rt.CREDENTIALS}", "%i")
    for writer, reader in ((root, own), (own, root), (root, own), (own, root)):
        text = f"written by {writer}"
        assert say(podman, writer, f"printf '{text}' > {path}").code == 0
        assert out(say(podman, reader, f"cat {path}")) == text  # at once, not after a sync
        assert podman.read_account(who.id, who.slot) == text.encode()
        assert (
            on_host(podman, f"{mount}/{rt.CREDENTIALS}", "%i") == inode
        )  # the one file, never replaced
        # Its mode and owner do not flip: as the host sees it, always the range's base.
        assert on_host(podman, f"{mount}/{rt.CREDENTIALS}") == [base_id, base_id, "600"]
        assert out(say(podman, root, f"stat -c '%u %g %a' {path}")) == "0 0 600"
        assert out(say(podman, own, f"stat -c '%u %g %a' {path}")) == f"{DEV_ID} {DEV_ID} 600"
    renamed = say(podman, own, f"echo new > {path}.new && mv {path}.new {path}")
    assert renamed.code != 0 and b"busy" in renamed.err.lower()  # in place only, for both kinds
    renamed = say(podman, root, f"echo new > {path}.new && mv {path}.new {path}")
    assert renamed.code != 0 and b"busy" in renamed.err.lower()
    assert say(podman, own, f"rm -f {path}.new").code == 0


# How long after the test asks both writers to start they begin, which is longer than starting
# a command in a container takes; and how long they write, together.
WRITERS_START_AFTER, WRITERS_RUN_FOR = 6.0, 3.0
# The least rounds each must manage in that time, and the least times each must read what the
# other wrote, for the two to have taken part in one interleaving.
LEAST_ROUNDS, LEAST_SEEN = 50, 5
# The lengths, in bytes, of the texts the workspaces take turns to write, a shorter after a
# longer among them, and how many turns they take.
TURN_LENGTHS, TURNS = (900, 120, 500, 60, 700), 20


def test_workspaces_taking_turns_to_refresh_the_shared_credentials_leave_them_whole(
    podman, person, two
):
    """A root workspace's Claude and the user's, two uids that are one host user, write the
    shared file in place by turns, each text of another length than the last, shorter ones
    after longer ones among them. Whatever was written last is what both read, whole, with
    nothing of a longer text left after it, and the file stays the person's alone."""
    root, own, path = two
    who = person[0]
    mount = volume_path(podman, rt.account_volume(who.id))
    initial = json.dumps({"writer": "start", **fake.credentials()})
    assert say(podman, root, f"printf '%s' '{initial}' > {path}").code == 0
    before = on_host(podman, f"{mount}/{rt.CREDENTIALS}", "%i %u %g %a")
    for turn in range(TURNS):
        writer, reader = (root, own) if turn % 2 == 0 else (own, root)
        name, length = ("root" if writer == root else "dev"), TURN_LENGTHS[turn % len(TURN_LENGTHS)]
        written = podman.exec(writer, ["python3", "-c", ONE_WRITE, path, name, str(length)])
        assert written.code == 0, written
        for seen_by in (reader, writer):
            read = say(podman, seen_by, f"cat {path}")
            assert len(read.out) == length, (turn, len(read.out), length)
            assert json.loads(read.out)["writer"] == name
        assert len(podman.read_account(who.id, who.slot)) == length
        assert rt.credentials_state(podman.read_account(who.id, who.slot)) == rt.SIGNED_IN
    assert on_host(podman, f"{mount}/{rt.CREDENTIALS}", "%i %u %g %a") == before


def test_two_workspaces_refreshing_the_shared_credentials_at_the_same_moment_are_measured(
    podman, person, two
):
    """The same writes, with nobody taking turns: Claude Code locks per configuration folder, so
    two workspaces of one person can refresh at the same moment, and the file may then be read
    empty, or left with the tail of a longer text. This is an accepted risk (the Claude account
    page of the documentation says so), and what rcman does about it is to notice: a file that
    holds no sign-in while the account is connected is reported as signed out unexpectedly.
    So what is asserted is that the two really overlapped (so that a clean run means something),
    that the file stays the person's alone, and that rcman reads the final file as it is: a
    sign-in if it is whole JSON of one writer's, and none if it is not. The incidents are
    printed, for whoever reads the run."""
    root, own, path = two
    who = person[0]
    mount = volume_path(podman, rt.account_volume(who.id))
    initial = json.dumps({"writer": "start", **fake.credentials()})
    assert say(podman, root, f"printf '%s' '{initial}' > {path}").code == 0
    before = on_host(podman, f"{mount}/{rt.CREDENTIALS}", "%i %u %g %a")
    start_at = time.time() + WRITERS_START_AFTER
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        writers = [
            pool.submit(
                podman.exec,
                name,
                ["python3", "-c", WRITER, path, who_, str(WRITERS_RUN_FOR), str(start_at), salt],
            )
            for name, who_, salt in ((root, "root", "0"), (own, "dev", "211"))
        ]
        done = [w.result(timeout=600) for w in writers]
    assert [d.code for d in done] == [0, 0], [(d.out, d.err) for d in done]
    stats = {s["who"]: s for s in (json.loads(d.out) for d in done)}
    print("concurrent refreshes of the shared credentials:", stats)
    for mine in stats.values():
        assert mine["rounds"] >= LEAST_ROUNDS, stats
        assert mine["saw_other"] + mine["empty"] + mine["torn"] >= LEAST_SEEN, stats
    assert max(s["first"] for s in stats.values()) < min(s["last"] for s in stats.values())
    final = podman.read_account(who.id, who.slot)
    try:
        whole = json.loads(final).get("writer") in ("root", "dev")
    except ValueError:
        whole = False
    assert (rt.credentials_state(final) == rt.SIGNED_IN) is whole, final[:80]
    assert on_host(podman, f"{mount}/{rt.CREDENTIALS}", "%i %u %g %a") == before


def test_the_users_workspace_reads_what_rcmans_own_git_folder_writes_in_it(podman, person, two):
    root, own, _ = two
    who = person[0]
    podman.put_account(who.id, who.slot, "config", b"[user]\n\tname = Someone\n")
    folder = f"{podman.layout(own).home}/{rt.GIT_HOME}"
    assert out(say(podman, own, f"cat {folder}/config")) == "[user]\n\tname = Someone"
    # The user renames inside the bound folder; the root workspace sees it at once, and rcman's
    # own write replaces the file the user made.
    assert (
        say(podman, own, f"cd {folder} && echo user > config.new && mv config.new config").code == 0
    )
    assert out(say(podman, root, f"cat {podman.layout(root).home}/{rt.GIT_HOME}/config")) == "user"
    podman.put_account(who.id, who.slot, "config", b"from rcman\n")
    assert out(say(podman, own, f"cat {folder}/config")) == "from rcman"
    assert (
        on_host(podman, f"{volume_path(podman, rt.account_volume(who.id))}/{rt.GIT_DIR}/config")[:2]
        == [str(who.slot * sizes.TEST_RANGE)] * 2
    )


# --- a workspace made from a repository, through the service ----------------------------------


def publish_repository(github_com, tmp_path, name: str, config, files=None) -> Path:
    """``octo/<name>`` on the tests' GitHub with a commit on main holding a devcontainer.json
    (``config`` a dict, or text) and ``files``: its bare repository."""
    bare = github_com.bare("octo", name)
    source = tmp_path / f"source-{name}"
    (source / ".devcontainer").mkdir(parents=True)
    text = config if isinstance(config, str) else json.dumps(config, indent=2)
    (source / ".devcontainer" / "devcontainer.json").write_text(text)
    (source / "README.md").write_text(f"# {name}\n")
    for path, content in (files or {}).items():
        (source / path).parent.mkdir(parents=True, exist_ok=True)
        (source / path).write_text(content)
    author = ["-c", "user.name=Octo", "-c", "user.email=octo@example.com"]
    for argv in (
        ["git", "init", "-q", "-b", "main", str(source)],
        ["git", "-C", str(source), "add", "."],
        ["git", "-C", str(source), *author, "commit", "-q", "-m", "First"],
        ["git", "-C", str(source), "push", "-q", str(bare), "main"],
    ):
        subprocess.run(argv, check=True, capture_output=True)
    return bare


def make(service, person, clones, name: str):
    """Make ``octo/<name>`` into a workspace and wait for it to be made."""
    made = service.create(person[0], name, f"https://github.com/octo/{name}")
    clones.append(made)
    return settled(service, made.id)


def plan_layout(service, workspace) -> rt.Layout:
    return service._layout(workspace)


def run_in(podman, service, workspace, *argv: str, **more) -> rt.Result:
    """``argv`` run in the workspace's work folder as its user, with the environment of the
    workspace's own programs."""
    layout = plan_layout(service, workspace)
    program = layout.with_env(Repository(layout, None, None).env, list(argv))
    return podman.exec(workspace.container, program, cwd=layout.work, **more)


def read_work(podman, service, workspace, path: str) -> str:
    layout = plan_layout(service, workspace)
    return podman.exec(
        workspace.container, [*layout.tool("cat"), f"{layout.work}/{path}"], again=True
    ).out.decode()


def test_a_remote_user_runs_rcmans_commands_claude_and_the_files_as_that_user(
    service, person, clones, podman, github_com, tmp_path, dev_image, account
):
    config = {
        "image": dev_image,
        "remoteUser": DEV_USER,
        "containerEnv": {"FROM_FILE": "container", "SHARED": "container"},
        "remoteEnv": {"SHARED": "remote", "PATH": "/home/dev/bin:${containerEnv:PATH}"},
        "postCreateCommand": (
            "id -un > who; id -G > groups; stat -c '%U' . > owner-of-work; env | sort > env"
        ),
    }
    publish_repository(github_com, tmp_path, "as-dev", config, {"src/a.txt": "a\n"})
    done = make(service, person, clones, "as-dev")
    assert (done.state, done.error, done.log) == ("ready", None, None), done
    assert done.environment and json.loads(done.environment)["uid"] == DEV_ID

    # The hook ran as the user, with its groups, and the clone root made is the user's.
    assert read_work(podman, service, done, "who").strip() == DEV_USER
    assert sorted(read_work(podman, service, done, "groups").split()) == sorted(
        [str(DEV_ID), str(EXTRA_ID)]
    )
    assert read_work(podman, service, done, "owner-of-work").strip() == DEV_USER
    assert out(run_in(podman, service, done, "stat", "-c", "%U", "src/a.txt")) == DEV_USER
    assert out(run_in(podman, service, done, "id", "-un")) == DEV_USER
    env = dict(line.split("=", 1) for line in read_work(podman, service, done, "env").splitlines())
    assert env["FROM_FILE"] == "container" and env["SHARED"] == "remote"
    assert env["FROM_IMAGE"] == "yes" and env["USER"] == DEV_USER
    assert env["HOME"] == podman.layout(done.container).home  # the workspace's, not /home/dev
    assert env["PATH"].startswith(f"{rt.PODMAN_LAYOUT.path_prefix}:/home/dev/bin:/opt/dev/bin:")

    # Claude's own child: the plan's variables, rcman's own after them, the file's PATH after
    # rcman's folder, and the user.
    layout = plan_layout(service, done)
    seen = podman.exec(done.container, claude_server.claude_command(layout, "fake-exec", "env"))
    assert seen.code == 0, seen.err
    child = dict(line.split("=", 1) for line in seen.out.decode().splitlines())
    assert child["FROM_FILE"] == "container" and child["SHARED"] == "remote"
    assert child["USER"] == child["LOGNAME"] == DEV_USER
    assert child["HOME"] == layout.home and child["CLAUDE_CONFIG_DIR"] == layout.config
    assert child["PATH"] == env["PATH"]
    devtool = podman.exec(
        done.container, claude_server.claude_command(layout, "fake-exec", "devtool")
    )
    assert devtool.out == b"devtool\n"  # the image's own programs are found

    # The spec's uid reaches the process, not only the name.
    assert out(podman.exec(done.container, ["id", "-u"])) == str(DEV_ID)


def test_the_claude_server_runs_as_the_remote_user(
    service, person, clones, podman, github_com, tmp_path, dev_image
):
    config = {"image": dev_image, "remoteUser": DEV_USER}
    publish_repository(github_com, tmp_path, "server-as-dev", config)
    done = make(service, person, clones, "server-as-dev")
    assert done.state == "ready", done
    layout = podman.layout(done.container)
    state = json.dumps({"trusted": [layout.work], "servers": {}})
    put = lambda path, text: podman.exec(  # noqa: E731
        done.container, [layout.shell, "-c", 'cat > "$1"', "sh", path], stdin=text.encode()
    )
    assert put(f"{layout.config}/{fake.STATE_FILE}", state).code == 0
    assert put(f"{layout.config}/{rt.CREDENTIALS}", json.dumps(fake.credentials())).code == 0
    con = db.connect(person[1])
    try:
        people.set_claude_account(con, person[0].id, {"oauthAccount": fake.oauth_account(EMAIL)})
    finally:
        con.close()
    answer = service.start_server(done, "dev")
    assert answer["server"]["state"] in ("ready", "connected"), answer
    try:
        top = cli(podman, "top", done.container, "user", "args").stdout.splitlines()
        running = [line for line in top if "remote-control" in line]
        assert running, top  # positive control: the server's process is listed
        assert {line.split()[0] for line in running} == {DEV_USER}
        tmux = [line for line in top if "tmux" in line]
        assert tmux and {line.split()[0] for line in tmux} == {DEV_USER}
        # It signs in with the shared file the user can read, and keeps its state beside it.
        assert out(
            podman.exec(done.container, [*layout.tool("cat"), f"{layout.config}/.claude.json"])
        )
    finally:
        service.stop_server(service.get(done.id))
    config_owner = on_host(
        podman, f"{volume_path(podman, done.container)}/{rt.CONFIG}/.claude.json", "%u %g"
    )
    base_id = str(person[0].slot * sizes.TEST_RANGE)
    assert config_owner == [base_id, base_id]  # on the host, still the range's base


def test_git_works_as_the_remote_user_with_the_persons_author_and_token_in_a_clone_root_made(
    service, person, clones, podman, github_com, tmp_path, dev_image, account
):
    config = {"image": dev_image, "remoteUser": DEV_USER}
    bare = publish_repository(github_com, tmp_path, "git-as-dev", config, {"notes.txt": "n\n"})
    account[0].save_token(account[1], TOKEN)
    account[0].save_settings(
        account[1], {"author_name": "Dev Author", "author_email": "dev.author@example.com"}
    )
    done = make(service, person, clones, "git-as-dev")
    assert done.state == "ready", done
    layout = plan_layout(service, done)
    run, run_stdin = Workspaces._execs(type("S", (), {"runtime": podman})(), done.container)
    repo = Repository(layout, run, run_stdin)
    # The clone was made by the default container's root: the user reads it and writes in it.
    status = repo.status()
    assert status["branch"]["name"] == "main" and status["not_staged"]["files"] == []
    assert (
        run_in(podman, service, done, "sh", "-c", "echo more >> notes.txt && echo b > b.txt").code
        == 0
    )
    repo.stage(["notes.txt", "b.txt"])
    before = len(github_com.attempts)
    repo.commit_push("Edited as the remote user")
    sent = subprocess.run(
        ["git", "-C", str(bare), "log", "-1", "--format=%an <%ae>|%s", "main"],
        capture_output=True, text=True, errors="replace", check=True,
    ).stdout.strip()  # fmt: skip
    assert sent == "Dev Author <dev.author@example.com>|Edited as the remote user"
    assert set(github_com.attempts[before:]) == {("x-access-token", True)}
    # The files git made in the work folder and in .git are the user's. The read changes
    # nothing, so it runs again should Podman lose what it wrote (rt.EXEC_MARK).
    owners = run_in(podman, service, done, "stat", "-c", "%U", ".git/HEAD", "b.txt", again=True)
    assert (owners.code, owners.out.split()) == (0, [DEV_USER.encode()] * 2), owners
    # Claude's git is the same git.
    claude = podman.exec(
        done.container,
        claude_server.claude_command(
            layout, "fake-exec", "git", "-C", layout.work, "config", "user.name"
        ),
    )
    assert claude.out.decode().strip() == "Dev Author"
    assert repo.env["HOME"] == podman.layout(done.container).home
    account[0].forget_token(account[1])


LIFECYCLE = {
    "remoteUser": DEV_USER,
    "containerEnv": {"KEPT": "from-containerEnv"},
    "remoteEnv": {"KEPT_TOO": "from-remoteEnv"},
    "onCreateCommand": "echo onCreate >> .order && echo $KEPT-$KEPT_TOO > .env-seen",
    "updateContentCommand": ["sh", "-c", "echo updateContent >> .order"],
    "postCreateCommand": {
        "first": "echo postCreate-first >> .order; echo created >> .created",
        "second": "echo postCreate-second >> .order",
    },
    "postStartCommand": "echo started >> .starts; echo postStart >> .order",
}


def lines_of(podman, service, workspace, path: str) -> list[str]:
    return read_work(podman, service, workspace, path).splitlines()


def test_the_create_commands_run_once_in_the_specs_order_and_the_start_command_each_start(
    service, person, clones, podman, github_com, tmp_path, dev_image
):
    publish_repository(github_com, tmp_path, "hooks", {"image": dev_image, **LIFECYCLE})
    done = make(service, person, clones, "hooks")
    assert (done.state, done.error) == ("ready", None), done
    assert lines_of(podman, service, done, ".order") == [
        "onCreate", "updateContent", "postCreate-first", "postCreate-second", "postStart",
    ]  # fmt: skip
    assert lines_of(podman, service, done, ".env-seen") == ["from-containerEnv-from-remoteEnv"]
    assert lines_of(podman, service, done, ".created") == ["created"]
    assert lines_of(podman, service, done, ".starts") == ["started"]

    def counts() -> tuple[int, int]:
        return (
            len(lines_of(podman, service, done, ".created")),
            len(lines_of(podman, service, done, ".starts")),
        )

    def restarted() -> None:
        """rcman ending and starting again: a new service on the same database and Podman."""
        again = Workspaces(service.settings, podman)
        try:
            again.restore()
        finally:
            again.close()

    restarted()  # the container kept running through it: nothing runs
    assert counts() == (1, 1) and podman.running(done.container)

    podman.stop(done.container)  # a reboot: the container is there, stopped
    restarted()
    assert podman.running(done.container) and counts() == (1, 2)

    podman.discard(done.container)  # the container is gone, the volume and the row are not
    assert not podman.exists(done.container) and exists(podman, "volume", done.container)
    restarted()  # made again from the stored plan, on the same volume
    assert podman.running(done.container)
    assert counts() == (1, 3)
    assert out(run_in(podman, service, done, "id", "-un")) == DEV_USER  # as the plan's user
    assert lines_of(podman, service, done, ".order").count("onCreate") == 1


def test_the_image_label_adds_its_settings_before_the_files_and_its_command_runs_once(
    service, person, clones, podman, github_com, tmp_path, labelled_image
):
    config = {"image": labelled_image, "containerEnv": {"FROM_FILE": "yes"},
              "postCreateCommand": "env | sort > env"}  # fmt: skip
    publish_repository(github_com, tmp_path, "labelled", config)
    done = make(service, person, clones, "labelled")
    assert (done.state, done.error) == ("ready", None), done
    assert lines_of(podman, service, done, "from-the-label") == ["label"]  # the label's command
    plan = json.loads(done.environment)
    assert plan["user_name"] == DEV_USER and plan["uid"] == DEV_ID  # the label's remoteUser
    env = dict(line.split("=", 1) for line in lines_of(podman, service, done, "env"))
    assert env["FROM_LABEL"] == "yes" and env["FROM_FILE"] == "yes"
    assert out(run_in(podman, service, done, "id", "-un")) == DEV_USER


def test_an_image_that_is_only_in_the_account_makes_a_workspace_with_no_registry(
    service, person, clones, podman, github_com, tmp_path, dev_image
):
    publish_repository(github_com, tmp_path, "local-image", {"image": dev_image})
    done = make(service, person, clones, "local-image")
    assert (done.state, done.error) == ("ready", None), done
    assert json.loads(done.environment)["image"] == dev_image


@pytest.mark.parametrize(
    ("key", "value"),
    [("mounts", ["source=/etc,target=/h,type=bind"]), ("privileged", True), ("frobnicate", 1)],
    ids=["a refused key", "another", "an unknown key"],
)
def test_a_refused_key_leaves_no_container_volume_or_image_and_the_row_names_it(
    service, person, clones, podman, github_com, tmp_path, dev_image, key, value
):
    publish_repository(github_com, tmp_path, f"refuses-{key}", {"image": dev_image, key: value})
    before = images(podman)
    done = make(service, person, clones, f"refuses-{key}")
    assert done.state == "failed", done
    assert f'"{key}"' in done.error or key in done.error, done.error
    assert left_of(podman, done.container) == []  # nothing: only the row is left
    assert images(podman) == before and probes(podman) == []
    assert service.get(done.id) is not None  # positive control: the row is there to be read


def test_an_alpine_image_is_refused_in_plain_words_and_leaves_nothing_behind(
    service, person, clones, podman, github_com, tmp_path
):
    alpine = "docker.io/library/alpine:3.20"
    publish_repository(github_com, tmp_path, "on-alpine", {"image": alpine})
    podman.pull(alpine)  # here before, and held, as the collection of images would take it
    holding = hold(podman, alpine)
    try:
        before = images(podman)
        done = make(service, person, clones, "on-alpine")
        assert done.state == "failed", done
        assert done.error == DEVCONTAINER_MESSAGES["musl"].format(image=alpine)
        assert (
            left_of(podman, done.container) == []
            and images(podman) == before
            and probes(podman) == []
        )
    finally:
        release(podman, holding)


def test_a_failing_create_command_keeps_the_workspace_says_why_and_hides_the_token(
    service, person, clones, podman, github_com, tmp_path, dev_image, account
):
    account[0].save_token(account[1], TOKEN)
    config = {
        "image": dev_image,
        "remoteUser": DEV_USER,
        "onCreateCommand": "echo first >> ../ran",
        "postCreateCommand": f"cat $HOME/{rt.GIT_HOME}/credential; echo oops >&2; exit 7",
        "postStartCommand": "echo never >> ../ran",
    }
    publish_repository(github_com, tmp_path, "fails", config)
    done = make(service, person, clones, "fails")
    try:
        assert done.state == "failed", done
        lines = done.error.splitlines()
        assert "postCreateCommand failed, exit code 7" in lines[0]
        assert "oops" in done.error and "password=***" in done.error
        assert TOKEN not in done.error + (done.log or "")
        row = db.connect(person[1]).execute(
            "select error, log from workspaces where id = ?", (done.id,)
        ).fetchone()  # fmt: skip
        assert TOKEN not in " ".join(str(v) for v in row)
        # Kept: the container and the volume, with what ran before the failure; nothing after.
        assert left_of(podman, done.container) == ["container", "volume"]
        assert podman.running(done.container)
        ran = podman.exec(done.container, [*podman.layout(done.container).tool("cat"),
                                           f"{rt.VOLUME_MOUNT}/ran"])  # fmt: skip
        assert ran.out == b"first\n"
    finally:
        account[0].forget_token(account[1])
    service.delete(service.get(done.id))
    gone(service, done.id)
    assert left_of(podman, done.container) == []


def test_deleting_a_workspace_whose_command_is_running_kills_it_and_leaves_nothing(
    service, person, clones, podman, github_com, tmp_path, dev_image
):
    config = {
        "image": dev_image,
        "remoteUser": DEV_USER,
        "postCreateCommand": "echo begun > begun; sleep 3000",
    }
    publish_repository(github_com, tmp_path, "slow-hook", config)
    made = service.create(person[0], "slow-hook", "https://github.com/octo/slow-hook")
    clones.append(made)
    layout = podman.layout(made.container)
    deadline = time.monotonic() + 300
    while True:
        assert time.monotonic() < deadline, service.get(made.id)
        try:
            if podman.exec(made.container, [*layout.tool("cat"), f"{layout.work}/begun"]).code == 0:
                break
        except rt.RuntimeFailed:
            pass  # the container is not there yet
        time.sleep(0.3)
    now = service.get(made.id)
    assert now.state == "creating" and now.log.splitlines()[-1] == "Running postCreateCommand"
    top = cli(podman, "top", made.container, "user", "args")
    assert "sleep 3000" in top.stdout, top.stdout + top.stderr  # positive: it is running there
    started = time.monotonic()
    service.delete(now)
    gone(service, made.id, 60)
    assert time.monotonic() - started < 60  # not after the sleep
    assert left_of(podman, made.container) == []
    assert probes(podman) == []


def test_a_clone_without_a_devcontainer_file_is_the_ordinary_workspace(
    service, person, clones, podman, github_com, tmp_path
):
    """The control for the rest: the same service, the default container, root of the range."""
    bare = github_com.bare("octo", "plain")
    source = tmp_path / "plain"
    (source).mkdir()
    (source / "README.md").write_text("# plain\n")
    for argv in (
        ["git", "init", "-q", "-b", "main", str(source)],
        ["git", "-C", str(source), "add", "."],
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=O",
            "-c",
            "user.email=o@e.com",
            "commit",
            "-q",
            "-m",
            "1",
        ],
        ["git", "-C", str(source), "push", "-q", str(bare), "main"],
    ):
        subprocess.run(argv, check=True, capture_output=True)
    done = make(service, person, clones, "plain")
    assert (done.state, done.environment) == ("ready", None)
    assert out(podman.exec(done.container, ["id", "-u"])) == "0"
