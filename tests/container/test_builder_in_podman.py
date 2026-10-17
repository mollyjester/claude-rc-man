"""The devcontainer builder against real rootless Podman and the real devcontainer CLI.

``tests/test_container_suite.py`` runs this module in the podman host as the service account,
as it runs ``tests/container/test_devcontainer_in_podman.py``, whose stand-in GitHub, workspace
image and person it shares. Anywhere else it skips. What the host-runtime tests and the stand-in
podman of ``tests/test_runtime_build.py`` could only assume is checked here against the real
thing, in two parts.

A *stub builder* is the workspace image with a stand-in for the Dev Containers CLI that records
what it was run with and what it was given, and waits to be told to fail. It takes no nested
Podman and no network, and shows what rcman does around the CLI: the uid range the builder runs
in and what it is given, the repository that is copied in and how, the build-only configuration
and the arguments the CLI is run with, the words of a failure, a cancel, the size limit, and the
formats of what ``builders``, ``built_images`` and ``remove_build`` read from Podman.

The *real builder* is built from ``installer/builder`` with the account's Podman, and builds a
Dockerfile and a feature of the repository with the real CLI: the image lands in rcman's storage,
the workspace is made from it, and everything the build used is gone afterwards. Features that
ask for what the policy refuses, and a base that is not glibc, are refused with nothing left.

The repositories are published on the stand-in as ``octo/<name>``.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from rcman import builder, devcontainer, people
from rcman import runtime as rt
from rcman.problems import DEVCONTAINER_MESSAGES
from rcman.settings import Settings
from rcman.workspaces import Workspaces

from tests.container import test_devcontainer_in_podman as dev
from tests.container import test_github_in_podman as shared
from tests.container import test_limits_in_podman as limited
from tests.container import test_runtime_in_podman as base

ADDRESS = shared.ADDRESS
exists, gone, settled = shared.exists, shared.gone, shared.settled
# The fixtures of that module, which pytest finds here by these names.
github_com, image, person, pki = shared.github_com, shared.image, shared.person, shared.pki

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1"
    or "RCMAN_PODMAN_IMAGE" not in os.environ
    or not ADDRESS,
    reason="runs only as the service account inside the podman host, with github.com in its hosts",
)

# Workspace ids no service numbers its rows with: the ones the tests that make a container or a
# build without the service use, clear of the other modules' and of the rows' own (from 1).
STUB_WORKSPACE, ORPHAN_BUILDER, ORPHAN_VOLUME, ORPHAN_IMAGE, WARM_BUILD, WARM_WORKSPACE = range(
    810, 816
)
# How long a build, a stub waiting to be told what to do, and a real build may take.
STUB_WITHIN, REAL_WITHIN = 300, 1800
# The repository's entry that says what the stub's standard error holds.
STUB_SAYS = ".devcontainer/stub-says"
# What the stub fails with, on its error stream: what the CLI says when a local feature fails.
FEATURE_FAILURE = (
    "[2026-10-15T21:59:54.800Z] [1/3] STEP 2/4: RUN echo base-step\r\n"
    "[2026-10-15T21:59:55.100Z] installing feature\r\n"
    '[2026-10-15T21:59:56.400Z] ERROR: Feature "./feature" (Unknown) failed to install! '
    "Look at the build logs above to investigate\r\n"
    '[2026-10-15T21:59:56.500Z] Error: building at STEP "RUN --mount=type=bind feature_1": '
    "while running runtime: exit status 3\r\n"
)

STUB = r"""#!/bin/sh
# Stands in for the Dev Containers CLI: records what it was run with and what it was given in
# /tmp, says what the repository's stub-says file holds on its standard error as the CLI says
# things, and when /tmp/go is there fails as the CLI does.
for a in "$@"; do printf '%s\n' "$a"; done > /tmp/argv
env > /tmp/env
config=
while [ $# -gt 0 ]; do [ "$1" = --config ] && config=$2; shift; done
cat "$config" > /tmp/config
find /work/repo -mindepth 1 -printf '%P\t%U\t%G\t%y\t%m\t%l\n' | sort > /tmp/files
[ -f /work/repo/.devcontainer/stub-says ] && cat /work/repo/.devcontainer/stub-says >&2
touch /tmp/recorded
while [ ! -e /tmp/go ]; do sleep 0.2; done
echo "Error: the stub stops here" >&2
echo '{"outcome":"error","message":"Command failed: the stub"}'
exit 1
"""
STUB_DOCKERFILE = """\
FROM {image}
COPY devcontainer /usr/local/bin/devcontainer
RUN chmod 755 /usr/local/bin/devcontainer
"""


class Watched(rt.PodmanRuntime):
    """The Podman runtime that notes which workspaces' builds ran the CLI, and which images were
    copied out of a builder."""

    def __init__(self, config: rt.PodmanConfig):
        super().__init__(config)
        self.copied: list[int] = []
        self.ran: list[str] = []

    def _build(self, request, name, *args, **more):
        self.ran.append(name)
        return super()._build(request, name, *args, **more)

    def _stage(self, workspace_id, *args, **more):
        """The copy of the image out of the builder, which is what the long copy is."""
        self.copied.append(workspace_id)
        return super()._stage(workspace_id, *args, **more)


def runtime_with(workspace_image: str, builder_image: str, scratch: Path) -> Watched:
    return Watched(
        rt.PodmanConfig(
            socket=base.socket(),
            image=workspace_image,
            tools_dir=base.TOOLS,
            claude_store=base.CLAUDE,
            builder_image=builder_image,
            scratch_dir=scratch,
        )
    )


@pytest.fixture(scope="module", autouse=True)
def persons_volume_is_removed(person):
    """The person's volume of credentials and git files, which the first workspace made."""
    yield
    runtime = rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=base.socket(), image="", tools_dir=base.TOOLS, claude_store=base.CLAUDE
        )
    )
    runtime.remove_account(person[0].id)


@pytest.fixture(scope="module")
def stub_image(image, tmp_path_factory) -> str:
    """The workspace image with the stand-in CLI."""
    context = tmp_path_factory.mktemp("stub-image")
    (context / "devcontainer").write_text(STUB)
    name = f"localhost/rcman-test-stub-builder:{os.getpid()}"
    dev.build(context, STUB_DOCKERFILE.format(image=image), name)
    yield name
    base.podman_cli("rmi", "--force", name)


@pytest.fixture(scope="module")
def real_image(tmp_path_factory) -> str:
    """The builder image the installer builds, built from the same folder with the account's
    Podman: the Dev Containers CLI pinned by its lock, and a Podman of its own."""
    from installer import install as ins

    context = tmp_path_factory.mktemp("builder-image")
    shutil.copytree(ins.BUILDER_DIR, context, dirs_exist_ok=True)
    name = f"localhost/rcman-test-builder:{os.getpid()}"
    built = subprocess.run(
        ["podman", "--remote", "--url", f"unix://{base.socket()}", "build", "-q", "-t", name,
         "--file", str(context / "Containerfile"), str(context)],
        capture_output=True,
        text=True, errors="replace",
        timeout=REAL_WITHIN,
    )  # fmt: skip
    assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-4000:]
    yield name
    base.podman_cli("rmi", "--force", name)


@pytest.fixture(scope="module")
def stub_podman(image, stub_image, tmp_path_factory) -> Watched:
    return runtime_with(image, stub_image, tmp_path_factory.mktemp("stub-scratch"))


@pytest.fixture(scope="module")
def real_podman(image, real_image, person, tmp_path_factory) -> Watched:
    """The runtime whose builder is the real one. The ranges it works in are warm: the builder
    image and the workspace image each have a copy in the range they run in, which Podman makes
    the first time and keeps, so that the layers the tests measure are only their own."""
    runtime = runtime_with(image, real_image, tmp_path_factory.mktemp("real-scratch"))
    who = person[0]
    warm = rt.container_name(WARM_WORKSPACE)
    runtime.create(rt.Spec(warm, who.slot, WARM_WORKSPACE, who.id))
    runtime.remove(warm)
    runtime._podman("volume", "create", builder.builder_name(WARM_BUILD))
    runtime._podman(*builder.run_argv(WARM_BUILD, {}, runtime._ids(people.BUILDER_SLOT),
                                      real_image))  # fmt: skip
    runtime.remove_build(WARM_BUILD)
    return runtime


def service_of(runtime: rt.PodmanRuntime, person) -> Workspaces:
    return Workspaces(Settings(public_origin="https://rcman.test", database=person[1]), runtime)


@pytest.fixture(scope="module")
def stub_service(stub_podman, person):
    made = service_of(stub_podman, person)
    yield made
    made.close()


@pytest.fixture(scope="module")
def real_service(real_podman, person):
    made = service_of(real_podman, person)
    yield made
    made.close()


def clones_of(service):
    """The workspaces a test makes, deleted afterwards and found gone, container and volume
    with them."""
    made: list = []
    yield made
    for workspace in made:
        now = service.get(workspace.id)
        if now is not None:
            service.delete(now)
    for workspace in made:
        gone(service, workspace.id)
        assert not exists("container", workspace.container)
        assert not exists("volume", workspace.container)


@pytest.fixture
def stub_clones(stub_service):
    yield from clones_of(stub_service)


@pytest.fixture
def real_clones(real_service):
    yield from clones_of(real_service)


# --- the repositories ------------------------------------------------------------------------


def publish_tree(github_com, tmp_path, name: str, files: dict, links=None, executable=()) -> Path:
    """``octo/<name>`` on the tests' GitHub with a commit on main holding ``files`` (path ->
    text or bytes), the symbolic ``links`` (path -> target) and the paths that are
    ``executable``: its bare repository."""
    bare = github_com.bare("octo", name)
    source = tmp_path / f"source-{name}"
    for path, content in files.items():
        (source / path).parent.mkdir(parents=True, exist_ok=True)
        (source / path).write_bytes(content if isinstance(content, bytes) else content.encode())
    for path, target in (links or {}).items():
        os.symlink(target, source / path)
    for path in executable:
        (source / path).chmod(0o755)
    author = ["-c", "user.name=Octo", "-c", "user.email=octo@example.com"]
    for argv in (
        ["git", "init", "-q", "-b", "main", str(source)],
        ["git", "-C", str(source), "add", "."],
        ["git", "-C", str(source), *author, "commit", "-q", "-m", "First"],
        ["git", "-C", str(source), "push", "-q", str(bare), "main"],
    ):
        subprocess.run(argv, check=True, capture_output=True)
    return bare


def start(service, person, clones, name: str):
    """Make ``octo/<name>`` into a workspace, which is under way when this returns."""
    made = service.create(person[0], name, f"https://github.com/octo/{name}")
    clones.append(made)
    return made


# The repository the stub is shown: a Dockerfile and a feature of its own, a file for each kind
# of name and link a tar has to keep, and what a build-only configuration has to leave out.
STUB_CONFIG = {
    "build": {"dockerfile": "Dockerfile", "context": ".."},
    "features": {"./feature": {"greeting": "yo"}},
    "overrideFeatureInstallOrder": ["./feature"],
    "remoteUser": "root",
    "containerEnv": {"FROM_FILE": "yes"},
    "postCreateCommand": "echo from-the-file",
    "customizations": {"vscode": {"extensions": ["a.b"]}},
}
BUILD_ONLY = {key: STUB_CONFIG[key] for key in devcontainer.BUILD_KEYS if key in STUB_CONFIG}
STUB_FILES = {
    ".devcontainer/devcontainer.json": json.dumps(STUB_CONFIG, indent=2),
    ".devcontainer/Dockerfile": "FROM docker.io/library/ubuntu:24.04\n",
    ".devcontainer/feature/devcontainer-feature.json": json.dumps({"id": "feature"}),
    ".devcontainer/feature/install.sh": "#!/bin/sh\necho installing\n",
    STUB_SAYS: FEATURE_FAILURE.encode(),
    "README.md": "# stub\n",
    "-dash": "a name that starts with a dash\n",
    "with space.txt": "a name with a space\n",
    ".hidden": "a name that starts with a dot\n",
    "..two-dots": "a name that starts with two dots\n",
    "tool.sh": "#!/bin/sh\n",
    "filler.bin": b"\0" * 200_000,
}
STUB_LINKS = {
    "link-relative": "README.md",
    "link-absolute": "/etc/hostname",
    "link-gone": "nowhere",
}


def in_builder(name: str, *argv: str) -> str:
    done = base.podman_cli("exec", name, *argv)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def wait_recorded(name: str, within: float = STUB_WITHIN, alive=None) -> None:
    """Wait until the stub in the builder ``name`` has recorded what it was run with; ``alive``
    is called meanwhile, to fail early with the row's words when the build ended."""
    deadline = time.monotonic() + within
    while base.podman_cli("exec", name, "test", "-e", "/tmp/recorded").returncode != 0:
        if alive is not None:
            alive()
        assert time.monotonic() < deadline, f"the stub in {name} never recorded"
        time.sleep(0.5)


def release(name: str) -> None:
    in_builder(name, "touch", "/tmp/go")


def listing(text: str) -> dict[str, tuple]:
    """The stub's listing of the repository: path -> (uid, gid, type, mode, link target)."""
    found = {}
    for line in text.splitlines():
        path, uid, gid, kind, mode, link = line.split("\t")
        found[path] = (uid, gid, kind, mode, link)
    return found


def nothing_of_a_build_is_left(runtime: rt.PodmanRuntime, workspace: int) -> None:
    name = builder.builder_name(workspace)
    assert workspace not in runtime.builders()
    assert workspace not in runtime.built_images()
    assert not exists("container", name) and not exists("volume", name)
    assert not exists("image", rt.built_name(workspace))
    assert not list(runtime.config.scratch_dir.glob(f"{rt.STAGING_PREFIX}*"))  # nothing staged


def image_ids() -> set[str]:
    return dev.images()


def storage_layers() -> set[str]:
    """The layers of the account's storage, with the copies Podman makes of an image's layers
    for a range."""
    root = base.podman_cli("info", "--format", "{{.Store.GraphRoot}}").stdout.strip()
    assert root
    return set(os.listdir(Path(root) / "overlay"))


# --- the stub builder: what rcman does around the CLI ------------------------------------------


def test_the_cli_is_given_only_the_build_keys_in_a_file_of_the_config_folder_and_its_words_are_told(
    stub_service, stub_podman, person, stub_clones, github_com, tmp_path
):
    publish_tree(github_com, tmp_path, "stub-words", STUB_FILES, STUB_LINKS, ["tool.sh"])
    made = start(stub_service, person, stub_clones, "stub-words")
    name = builder.builder_name(made.id)
    wait_recorded(name, alive=lambda: _creating(stub_service, made.id))
    assert stub_podman.builders() == {made.id}  # positive: the builder is there, in one piece
    config_path = f"{builder.REPOSITORY_FOLDER}/.devcontainer/{devcontainer.BUILD_CONFIG_NAME}"
    assert in_builder(name, "cat", "/tmp/argv").splitlines() == [
        "build", "--workspace-folder", builder.REPOSITORY_FOLDER, "--config", config_path,
        "--no-lockfile", "--docker-path", "podman", "--image-name", builder.IN_BUILDER_IMAGE,
    ]  # fmt: skip
    variables = in_builder(name, "cat", "/tmp/env").splitlines()
    assert [v for v in variables if not v.startswith("PWD=")] == [f"PATH={builder.BUILDER_PATH}"]
    assert json.loads(in_builder(name, "cat", "/tmp/config")) == BUILD_ONLY
    # The file's own devcontainer.json is in the copy as it is, and only the new file was added.
    kept = in_builder(name, "cat", f"{builder.REPOSITORY_FOLDER}/.devcontainer/devcontainer.json")
    assert json.loads(kept) == STUB_CONFIG
    found = listing(in_builder(name, "cat", "/tmp/files"))
    assert not [p for p in found if p == ".git" or p.startswith(".git/")]
    assert ".devcontainer/devcontainer.json" in found and "README.md" in found
    # positive control for the absence: the workspace the copy came from does have one
    layout = stub_podman.layout(made.container)
    assert stub_podman.exec(made.container, ["test", "-d", f"{layout.work}/.git"]).code == 0
    release(name)

    done = settled(stub_service, made.id)
    assert done.state == "failed", done
    head = DEVCONTAINER_MESSAGES["build_feature_failed"].format(feature="./feature", tail="")
    assert done.error.startswith(head), done.error  # the cause, in the catalogue's words
    assert "failed to install" in done.error and "\r" not in done.error
    lines = done.log.splitlines()
    assert "Building the image" in lines
    assert "[1/3] STEP 2/4: RUN echo base-step" in lines  # a step, without its time
    assert left_of_build(stub_podman, done) == []


def _creating(service, workspace_id: int) -> None:
    now = service.get(workspace_id)
    assert now is not None and now.state == "creating", now


def left_of_build(runtime: rt.PodmanRuntime, workspace) -> list[str]:
    """What is left of a build that ended: the builder, its volume, an image, and the
    workspace's container and volume, which a refused or failed one leaves none of."""
    nothing_of_a_build_is_left(runtime, workspace.id)
    return dev.left_of(workspace.container)


def test_the_builder_runs_in_a_range_of_its_own_with_nothing_of_rcmans_and_the_files_are_its_roots(
    stub_podman, person, stub_image
):
    """The runtime alone, with a workspace made by hand so that its files can be chosen: the
    builder's uid map is the builder slot's range, which no person's is, it is not privileged and
    has one capability and one device more than a workspace, it mounts only its own volume, and
    has no rcman socket or run folder; the files that arrive belong to its root whoever owned
    them, links stay links, modes are kept and the git folder is not there."""
    who = person[0]
    assert who.slot != people.BUILDER_SLOT
    source = rt.container_name(STUB_WORKSPACE)
    stub_podman.create(rt.Spec(source, who.slot, STUB_WORKSPACE, who.id))
    layout = stub_podman.layout(source)
    made = stub_podman.exec(
        source,
        [layout.shell, "-c",
         "mkdir -p .devcontainer .git/objects sub && echo head > .git/HEAD && echo a > a && "
         "echo b > sub/b && echo x > x && chmod 750 x && chown 1000:1000 a sub/b && "
         "ln -s a link && ln -s /etc/hostname absolute && ln -s gone dangling && "
         ": > -dash && : > .hidden && : > ..two-dots && : > 'with space'"],
        cwd=layout.work,
    )  # fmt: skip
    assert made.code == 0, made
    request = rt.BuildRequest(
        STUB_WORKSPACE, who.id, people.BUILDER_SLOT, source, ".devcontainer/rcman-build.json",
        json.dumps(BUILD_ONLY),
    )  # fmt: skip
    ended: list = []
    said: list[str] = []

    def build() -> None:
        try:
            stub_podman.build_image(request, said.append, None, lambda labels: None)
        except Exception as failed:  # noqa: BLE001 - what the test looks at
            ended.append(failed)

    thread = threading.Thread(target=build, daemon=True)
    thread.start()
    name = builder.builder_name(STUB_WORKSPACE)
    try:
        wait_recorded(name, alive=lambda: ended and pytest.fail(f"the build ended: {ended}"))
        inspected = json.loads(base.podman_cli("container", "inspect", name).stdout)[0]
        host = inspected["HostConfig"]
        # In the slice of the person who asked for the build, beside their workspaces' slices
        # and not in one of them (limits.py): the cgroup inside its scope is two beneath it.
        in_slice = limited.cgroup_of(inspected["State"]["Pid"]).parent
        assert in_slice.name.startswith("libpod-") and in_slice.parent == limited.slice_dir(who.id)
        source_in = limited.cgroup_of(base.inspect(source)["State"]["Pid"]).parent  # the control
        assert source_in.parent == limited.slice_dir(who.id, STUB_WORKSPACE)
        base_id = str(people.BUILDER_SLOT * rt.RANGE)
        for mapping in ("uid_map", "gid_map"):
            seen = in_builder(name, "cat", f"/proc/self/{mapping}").split()
            assert seen == ["0", base_id, str(rt.RANGE)], seen
        assert str(who.slot * rt.RANGE) != base_id
        assert host["Privileged"] is False
        assert any("SYS_ADMIN" in cap for cap in host["CapAdd"] or []), host["CapAdd"]
        here = set(in_builder(name, "ls", "/dev").split())
        plain = stub_podman.exec(source, ["ls", "/dev"]).out.decode().split()
        assert here - set(plain) == {"fuse"}  # the one device a Podman inside needs, and no other
        assert [(m["Type"], m["Name"], m["Destination"]) for m in inspected["Mounts"]] == [
            ("volume", name, builder.STORAGE_MOUNT)
        ]  # nothing of the machine, and no socket
        caps = int(
            re.search(r"CapEff:\s+([0-9a-f]+)", in_builder(name, "cat", "/proc/1/status"))[1], 16
        )
        assert caps & (1 << 21) and not caps & (1 << 16)  # CAP_SYS_ADMIN, but not CAP_SYS_MODULE
        assert (
            in_builder(name, "sh", "-c", "ls -d /run/user 2>&1 || true")
            .strip()
            .endswith("No such file or directory")
        )
        in_builder(name, "sh", "-c", "mkdir -p /tmp/control && : > /tmp/control/podman.sock")
        sockets = in_builder(
            name, "sh", "-c",
            "find / -path /proc -prune -o -path /sys -prune -o -name podman.sock -print 2>&1",
        )  # fmt: skip
        assert sockets.split() == [
            "/tmp/control/podman.sock"
        ]  # the search finds one if it is there

        files = listing(in_builder(name, "cat", "/tmp/files"))
        assert set(files) == {
            ".devcontainer", ".devcontainer/rcman-build.json", "a", "sub", "sub/b", "x", "link",
            "absolute", "dangling", "-dash", ".hidden", "..two-dots", "with space",
        }  # fmt: skip
        assert {(uid, gid) for uid, gid, *_ in files.values()} == {("0", "0")}
        assert (files["link"][2], files["link"][4]) == ("l", "a")
        assert (files["absolute"][2], files["absolute"][4]) == ("l", "/etc/hostname")
        assert (files["dangling"][2], files["dangling"][4]) == ("l", "gone")
        assert files["x"][3] == "750" and files["sub"][2] == "d"
    finally:
        stub_podman.remove_build(STUB_WORKSPACE)  # which ends the build
        thread.join(timeout=60)
        stub_podman.remove(source)
    assert not thread.is_alive() and len(ended) == 1
    assert isinstance(ended[0], rt.RuntimeFailed)
    nothing_of_a_build_is_left(stub_podman, STUB_WORKSPACE)


def test_a_repository_bigger_than_the_limit_is_refused_in_plain_words_before_the_cli_is_run(
    stub_service, stub_podman, person, stub_clones, github_com, tmp_path, monkeypatch
):
    monkeypatch.setattr(builder, "MAX_CONTEXT_BYTES", 20_000)
    ran_before = list(stub_podman.ran)
    publish_tree(github_com, tmp_path, "stub-big", STUB_FILES, STUB_LINKS, ["tool.sh"])
    made = start(stub_service, person, stub_clones, "stub-big")
    done = settled(stub_service, made.id)
    assert done.state == "failed", done
    assert done.error == DEVCONTAINER_MESSAGES["build_context_too_big"]
    assert "Copying the repository into the builder" in done.log.splitlines()
    assert stub_podman.ran == ran_before  # the CLI was never run
    assert left_of_build(stub_podman, done) == []


def test_deleting_a_workspace_whose_image_is_being_built_removes_the_builder_and_its_volume(
    stub_service, stub_podman, person, stub_clones, github_com, tmp_path
):
    publish_tree(github_com, tmp_path, "stub-cancel", STUB_FILES, STUB_LINKS, ["tool.sh"])
    made = start(stub_service, person, stub_clones, "stub-cancel")
    name = builder.builder_name(made.id)
    wait_recorded(name, alive=lambda: _creating(stub_service, made.id))
    assert exists("container", name) and exists("volume", name)  # positive: it is being built
    top = base.podman_cli("top", name, "args")
    assert "devcontainer" in top.stdout or "sleep" in top.stdout, top.stdout + top.stderr
    now = stub_service.get(made.id)
    started = time.monotonic()
    stub_service.delete(now)
    gone(stub_service, made.id, 60)
    assert time.monotonic() - started < 60
    nothing_of_a_build_is_left(stub_podman, made.id)
    assert dev.left_of(made.container) == []


def test_the_builders_and_built_images_there_are_are_read_from_podmans_own_words_and_removed(
    stub_service, stub_podman, person, stub_image
):
    """The formats of ``podman ps``, ``volume ls`` and ``images`` as JSON, the filter on an image's
    name, the id an image is tagged by and ``image rm --ignore``, against the real Podman: a
    builder with its volume, a volume alone and a built image are found; decoys are not; removal
    is whole, and again it is nothing. The restore after a restart removes the same orphans."""
    who = person[0]
    decoys = [f"localhost/{rt.BUILT_REPOSITORY}/notanumber:latest"]

    def orphans() -> None:
        for workspace in (ORPHAN_BUILDER, ORPHAN_VOLUME):
            stub_podman._podman(
                "volume", "create", f"--label={rt.LABEL_BUILDER}={workspace}",
                f"--label={rt.LABEL_PERSON}={who.id}", builder.builder_name(workspace),
            )  # fmt: skip
        labels = {rt.LABEL_BUILDER: str(ORPHAN_BUILDER), rt.LABEL_PERSON: str(who.id)}
        stub_podman._podman(
            *builder.run_argv(
                ORPHAN_BUILDER, labels, stub_podman._ids(people.BUILDER_SLOT), stub_image
            )
        )
        stub_podman._podman("tag", stub_image, rt.built_name(ORPHAN_IMAGE))
        # decoys: a name in the built images' repository that is not a workspace's, and a volume
        # that is no builder's
        stub_podman._podman("tag", stub_image, decoys[0])

    try:
        assert stub_podman.builders() == set() and stub_podman.built_images() == {}  # nothing yet
        stub_podman._podman("volume", "create", "rcman-test-decoy-volume")
        orphans()
        assert stub_podman.builders() == {ORPHAN_BUILDER, ORPHAN_VOLUME}
        assert stub_podman.built_images() == {ORPHAN_IMAGE: rt.built_name(ORPHAN_IMAGE)}
        # removal: by workspace, whole, and again it is nothing
        stub_podman.remove_build(ORPHAN_BUILDER)
        assert stub_podman.builders() == {ORPHAN_VOLUME}
        assert not exists("container", builder.builder_name(ORPHAN_BUILDER))
        assert not exists("volume", builder.builder_name(ORPHAN_BUILDER))
        stub_podman.remove_build(ORPHAN_BUILDER)
        stub_podman.remove_build(ORPHAN_VOLUME)
        assert stub_podman.builders() == set()
        assert exists("image", rt.built_name(ORPHAN_IMAGE))  # an image only when asked for
        stub_podman.remove_build(ORPHAN_IMAGE, image=True)
        assert stub_podman.built_images() == {}
        assert exists("image", stub_image)  # the stub image, which the tag shared, is still there
        stub_podman.remove_build(ORPHAN_IMAGE, image=True)
        # the restore after a restart removes them when nothing needs them
        orphans()
        assert stub_podman.builders() and stub_podman.built_images()
        stub_service.restore()
        assert stub_podman.builders() == set() and stub_podman.built_images() == {}
        assert exists("image", decoys[0]) and exists("volume", "rcman-test-decoy-volume")
    finally:
        for workspace in (ORPHAN_BUILDER, ORPHAN_VOLUME):
            stub_podman.remove_build(workspace)
        stub_podman.remove_build(ORPHAN_IMAGE, image=True)
        base.podman_cli("rmi", "--force", *decoys)
        base.podman_cli("volume", "rm", "--force", "rcman-test-decoy-volume")


# --- the real builder ---------------------------------------------------------------------------

REAL_CONFIG = {
    "build": {"dockerfile": "Dockerfile", "context": ".."},
    "features": {"./tool": {"greeting": "from-the-file"}},
    "containerEnv": {"FROM_FILE": "yes"},
    "postCreateCommand": "echo post-create >> .order",
}
REAL_FILES = {
    ".devcontainer/devcontainer.json": json.dumps(REAL_CONFIG, indent=2),
    # A glibc base, git for the workspace's checks, and a file of the repository.
    ".devcontainer/Dockerfile": (
        "FROM docker.io/library/ubuntu:24.04\n"
        "RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "--no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*\n"
        "COPY .devcontainer/marker.txt /etc/marker\n"
        "ENV FROM_DOCKERFILE=yes\n"
    ),
    ".devcontainer/marker.txt": "from the repository\n",
    ".devcontainer/tool/devcontainer-feature.json": json.dumps(
        {
            "id": "tool",
            "version": "1.0.0",
            "name": "a tool",
            "options": {"greeting": {"type": "string", "default": "hello"}},
            "containerEnv": {"PATH": "/opt/mytool/bin:${PATH}", "MYTOOL_HOME": "/opt/mytool"},
            "onCreateCommand": "echo feature-oncreate >> .order",
        }
    ),
    ".devcontainer/tool/install.sh": (
        "#!/bin/sh\nset -e\nmkdir -p /opt/mytool/bin\n"
        "printf '#!/bin/sh\\necho mytool %s\\n' \"$GREETING\" > /opt/mytool/bin/mytool\n"
        "chmod +x /opt/mytool/bin/mytool\n"
    ),
}
# A base that is glibc-free, which Claude Code cannot run on, and one thing a feature asks for.
ALPINE = "docker.io/library/alpine:3.20"


def alpine_files(feature: dict) -> dict:
    return {
        ".devcontainer/devcontainer.json": json.dumps(
            {"image": ALPINE, "features": {"./one": {}}}, indent=2
        ),
        ".devcontainer/one/devcontainer-feature.json": json.dumps({"id": "one", **feature}),
        ".devcontainer/one/install.sh": "#!/bin/sh\necho one > /one\n",
    }


def run_in(podman, service, workspace, *argv: str) -> rt.Result:
    layout = service._layout(workspace)
    return podman.exec(
        workspace.container,
        layout.with_env(rt.child_env(layout), list(argv)),
        cwd=layout.work,
    )


def test_a_dockerfile_and_a_local_feature_are_built_for_real_and_the_workspace_runs_from_the_image(
    real_service, real_podman, person, real_clones, github_com, tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="rcman.runtime")
    said: dict[int, list[str]] = {}
    original = real_service._log

    def recording(workspace_id: int, text: str) -> None:
        said.setdefault(workspace_id, []).append(text)
        original(workspace_id, text)

    monkeypatch.setattr(real_service, "_log", recording)
    publish_tree(github_com, tmp_path, "real-one", REAL_FILES)
    layers, images_before = storage_layers(), image_ids()

    # Two workspaces of the same person, built at once in the one range the builders have.
    first = start(real_service, person, real_clones, "real-one")
    second = real_service.create(person[0], "real-two", "https://github.com/octo/real-one")
    real_clones.append(second)
    one, two = (
        settled(real_service, first.id, REAL_WITHIN),
        settled(real_service, second.id, REAL_WITHIN),
    )
    assert (one.state, one.error) == ("ready", None), one
    assert (two.state, two.error) == ("ready", None), two

    # The image is in rcman's storage under the name made for the workspace, one for each.
    names = {w.id: rt.built_name(w.id) for w in (one, two)}
    assert len(set(names.values())) == 2
    assert real_podman.built_images() == names
    for workspace in (one, two):
        plan = json.loads(workspace.environment)
        assert plan["image"] == names[workspace.id]
        assert base.inspect(workspace.container)["ImageName"] == names[workspace.id]
        assert real_podman.running(workspace.container)
    assert sorted(real_podman.copied) == sorted([one.id, two.id])  # each image copied out once

    assert storage_layers() - layers  # positive: the images and their copies are layers

    # The tool the feature installed is on the PATH Claude's programs have, and its variables
    # are in the environment, from the image's own ENV.
    layout = real_service._layout(one)
    path = rt.child_env(layout)["PATH"].split(":")
    assert "/opt/mytool/bin" in path and path.index("/opt/mytool/bin") < path.index("/usr/bin")
    tool = run_in(real_podman, real_service, one, "mytool")
    assert tool.out.decode().strip() == "mytool from-the-file", tool
    seen = dict(
        line.split("=", 1)
        for line in dev.out(run_in(real_podman, real_service, one, "env")).splitlines()
    )
    assert seen["MYTOOL_HOME"] == "/opt/mytool" and seen["FROM_DOCKERFILE"] == "yes"
    assert seen["FROM_FILE"] == "yes"
    marker = dev.out(run_in(real_podman, real_service, one, "cat", "/etc/marker"))
    assert marker == "from the repository"  # the Dockerfile's COPY had the repository

    # The label has the feature's entry and no copy of the file's own, so the lifecycle commands
    # ran once each, the feature's first.
    label = base.podman_cli("image", "inspect", "--format", "{{json .Labels}}", names[one.id])
    entries = json.loads(json.loads(label.stdout)[devcontainer.METADATA_LABEL])
    assert [e.get("id") for e in entries] == ["./tool"], entries
    assert dev.lines_of(real_podman, real_service, one, ".order") == [
        "feature-oncreate",
        "post-create",
    ]

    # The builder, its volume and the image in it are gone; the progress reached the log, and
    # the time of the copy was logged.
    for workspace in (one, two):
        nothing = builder.builder_name(workspace.id)
        assert not exists("container", nothing) and not exists("volume", nothing)
        assert workspace.id not in real_podman.builders()
        lines = said[workspace.id]
        assert lines.count("Building the image") == 1  # told once, by the service
        assert "Copying the repository into the builder" in lines
        assert any(builder.STEP.match(line) for line in lines), lines
        assert not any("\r" in line or builder.STAMP.match(line) for line in lines)
    copied = [
        r.getMessage() for r in caplog.records if "copied out of its builder" in r.getMessage()
    ]
    assert len(copied) == 2 and all(re.search(r"\d+ bytes in \d+\.\d s", c) for c in copied), copied
    named = base.podman_cli("images", "--all", "--format", "{{.Names}}").stdout
    assert "rcman-in-builder" not in named  # the name the image has in the builder

    # rcman restarting keeps what is used.
    again = Workspaces(real_service.settings, real_podman)
    try:
        again.restore()
    finally:
        again.close()
    assert real_podman.built_images() == names and real_podman.running(one.container)
    assert real_service.get(one.id).state == real_service.get(two.id).state == "ready"

    # Deleting one removes its image and its copy and does not touch the other's.
    real_service.delete(real_service.get(one.id))
    gone(real_service, one.id)
    assert real_podman.built_images() == {two.id: names[two.id]}
    assert not exists("image", names[one.id]) and exists("image", names[two.id])
    assert real_podman.running(two.container)
    assert run_in(real_podman, real_service, two, "mytool").out.decode().strip() == (
        "mytool from-the-file"
    )
    real_service.delete(real_service.get(two.id))
    gone(real_service, two.id)
    assert real_podman.built_images() == {} and not exists("image", names[two.id])
    # Nothing of the builds is left. Deleting a workspace collects the images nothing refers to,
    # so images that were here before (the pinned ones the host loaded) may be gone too: what is
    # asserted is that nothing is here that was not.
    assert image_ids() <= images_before, sorted(image_ids() - images_before)
    assert storage_layers() <= layers, sorted(storage_layers() - layers)


def test_a_feature_that_asks_for_privileges_is_refused_naming_it_before_the_image_is_copied_out(
    real_service, real_podman, person, real_clones, github_com, tmp_path
):
    risky = {"privileged": True, "mounts": [{"source": "x", "target": "/x", "type": "volume"}]}
    publish_tree(github_com, tmp_path, "real-risky", alpine_files(risky))
    images_before, copied_before = image_ids(), list(real_podman.copied)
    done = settled(
        real_service, start(real_service, person, real_clones, "real-risky").id, REAL_WITHIN
    )
    assert done.state == "failed", done
    assert "./one" in done.error and ("privileged" in done.error or "mounts" in done.error), (
        done.error
    )
    assert real_podman.copied == copied_before  # refused before the long copy
    assert left_of_build(real_podman, done) == []
    assert image_ids() == images_before and dev.probes() == []


def test_a_base_that_is_not_glibc_is_built_then_refused_in_plain_words_and_leaves_nothing(
    real_service, real_podman, person, real_clones, github_com, tmp_path
):
    publish_tree(github_com, tmp_path, "real-alpine", alpine_files({}))
    images_before, copied_before = image_ids(), list(real_podman.copied)
    done = settled(
        real_service, start(real_service, person, real_clones, "real-alpine").id, REAL_WITHIN
    )
    assert done.state == "failed", done
    assert done.error == DEVCONTAINER_MESSAGES["musl"].format(image=rt.built_name(done.id))
    assert real_podman.copied == [*copied_before, done.id]  # built and copied out, then refused
    assert left_of_build(real_podman, done) == []
    assert image_ids() == images_before and dev.probes() == []


FAILING = [
    pytest.param(
        {
            ".devcontainer/devcontainer.json": json.dumps(
                {"image": ALPINE, "features": {"./one": {}}}
            ),
            ".devcontainer/one/devcontainer-feature.json": json.dumps({"id": "one"}),
            ".devcontainer/one/install.sh": "#!/bin/sh\necho the-words-said >&2\nexit 3\n",
        },
        "build_feature_failed",
        id="a feature that fails to install",
    ),
    pytest.param(
        {
            ".devcontainer/devcontainer.json": json.dumps({"build": {"dockerfile": "Dockerfile"}}),
            ".devcontainer/Dockerfile": f"FROM {ALPINE}\nRUN echo the-words-said >&2; exit 7\n",
        },
        "build_step_failed",
        id="a step of the Dockerfile that fails",
    ),
]


@pytest.mark.parametrize(("files", "kind"), FAILING)
def test_a_build_the_cli_fails_is_told_by_its_cause_with_its_last_words_and_leaves_nothing(
    real_service, real_podman, person, real_clones, github_com, tmp_path, files, kind
):
    publish_tree(github_com, tmp_path, f"real-{kind}", files)
    images_before, copied_before = image_ids(), list(real_podman.copied)
    done = settled(
        real_service, start(real_service, person, real_clones, f"real-{kind}").id, REAL_WITHIN
    )
    assert done.state == "failed", done
    head = DEVCONTAINER_MESSAGES[kind].format(feature="./one", tail="")
    assert done.error.startswith(head), done.error  # the cause the real CLI's words show
    assert "the-words-said" in done.error.removeprefix(head)  # and its last words, as lines
    assert "\r" not in done.error and not builder.STAMP.search(done.error)
    assert real_podman.copied == copied_before
    assert left_of_build(real_podman, done) == []
    assert image_ids() == images_before and dev.probes() == []


def test_deleting_a_workspace_whose_image_is_really_being_built_stops_the_build_and_leaves_nothing(
    real_service, real_podman, person, real_clones, github_com, tmp_path
):
    files = {
        ".devcontainer/devcontainer.json": json.dumps({"build": {"dockerfile": "Dockerfile"}}),
        ".devcontainer/Dockerfile": f"FROM {ALPINE}\nRUN echo begun; sleep 3000\n",
    }
    publish_tree(github_com, tmp_path, "real-slow", files)
    images_before = image_ids()
    made = start(real_service, person, real_clones, "real-slow")
    name = builder.builder_name(made.id)
    deadline = time.monotonic() + REAL_WITHIN
    while "sleep 3000" not in base.podman_cli("top", name, "args").stdout:  # the Dockerfile's step
        _creating(real_service, made.id)
        assert time.monotonic() < deadline, real_service.get(made.id)
        time.sleep(1)
    started = time.monotonic()
    real_service.delete(real_service.get(made.id))
    gone(real_service, made.id, 60)
    assert time.monotonic() - started < 60  # not after the step's sleep
    nothing_of_a_build_is_left(real_podman, made.id)
    assert dev.left_of(made.container) == [] and image_ids() == images_before
