"""The devcontainer builder against real rootless Podman and the real devcontainer CLI.

On the tests' engine (tests/podman_service.py), with the builder
in the suites' builder slot (tests/container/sizes.py). The builder pulls the images a build
starts from, so the module's workspaces and builders are on the engine's own network; the
stand-in GitHub serves on its loopback, on a port of its own, and the workspaces' git reaches it
as https://github.com through a relay (tests/container/network.py ``Relay``). What the
host-runtime tests and the stand-in podman of ``tests/test_runtime_build.py`` could only assume
is checked here against the real thing, in one real build.

The *real builder* is built from ``installer/builder`` on the engine, and builds a Dockerfile and
a feature of the repository with the real CLI: the image lands in rcman's storage, the workspace
is made from it, and everything the build used is gone afterwards.

The repositories are published on the stand-in as ``octo/<name>``.
"""

import json
import logging
import os
import re
import ssl
import subprocess
from pathlib import Path

import pytest
from rcman import builder, db, devcontainer, people
from rcman import runtime as rt
from rcman.workspaces import Workspaces

from tests.container import github_stand_in as stand_in
from tests.container import network, sizes
from tests.container.conftest import cli, engine_runtime, engine_settings
from tests.github_server import GitHubServer
from tests.hang_guard import CONTAINER_HANG_GUARD
from tests.until import until

TOKEN = "github_pat_podman-test-token"

EMAIL = "bob@example.com"


@pytest.fixture(scope="module")
def pki(tmp_path_factory) -> Path:
    """A certificate authority, and a certificate it signed for github.com."""
    return stand_in.mint(tmp_path_factory.mktemp("pki"))


def tls(pki: Path) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(pki / "leaf.crt", pki / "leaf.key")
    return context


def build(engine, context: Path, name: str) -> str:
    built = engine.cli(
        "build", "-q", "--pull=never", "-t", name, str(context), timeout=CONTAINER_HANG_GUARD
    )
    assert built.returncode == 0, (built.stdout + built.stderr).decode(errors="replace")
    return name


@pytest.fixture(scope="module")
def person(tmp_path_factory):
    """A person of rcman's database, in a slot the engine's ids hold (tests/container/sizes.py),
    and the git account that writes their credential."""
    path = tmp_path_factory.mktemp("rcman") / "rcman.db"
    db.migrate(path)
    con = db.connect(path)
    try:
        who = people.add(con, EMAIL, False)
    finally:
        con.close()
    return who, path


def settled(service, workspace_id: int):
    """The workspace once it is no longer being made (None for one that is gone)."""

    def made():
        now = service.get(workspace_id)
        return [now] if now is None or now.state != "creating" else None

    return until(made)[0]


def gone(service, workspace_id: int) -> None:
    """Wait until the workspace's row is gone, which a deletion of one being made leaves to
    the thread that was making it."""
    until(lambda: service.get(workspace_id) is None)


def exists(runtime: rt.PodmanRuntime, kind: str, name: str) -> bool:
    """Whether ``runtime``'s Podman has the ``kind`` (container, volume, image) ``name``."""
    return runtime._podman(kind, "exists", name, check=False).returncode == 0


def images(podman) -> set[str]:
    found = cli(podman, "images", "--all", "--format", "{{.ID}}")
    assert found.returncode == 0, found.stderr
    return set(found.stdout.split())


def out(result: rt.Result) -> str:
    assert result.code == 0, result
    return result.out.decode().strip()


def inspect(podman, name: str) -> dict:
    found = cli(podman, "container", "inspect", name)
    assert found.returncode == 0, found.stderr
    return json.loads(found.stdout)[0]


def plan_layout(service, workspace) -> rt.Layout:
    return service._layout(workspace)


def read_work(podman, service, workspace, path: str) -> str:
    layout = plan_layout(service, workspace)
    return podman.exec(
        workspace.container, [*layout.tool("cat"), f"{layout.work}/{path}"], again=True
    ).out.decode()


def lines_of(podman, service, workspace, path: str) -> list[str]:
    return read_work(podman, service, workspace, path).splitlines()


# The fixtures of that module, which pytest finds here by these names.


@pytest.fixture(scope="module")
def github_com(pki, tmp_path_factory):
    """The tests' GitHub on the loopback of the engine's own network, on a port of its own, and
    the relay the workspaces' git reaches it through as https://github.com (``relay``)."""
    server = GitHubServer()
    server.start(tmp_path_factory.mktemp("github"), TOKEN, ssl_context=tls(pki))
    port = int(server.url.rstrip("/").rsplit(":", 1)[1])
    server.relay = network.Relay("github.com", ("127.0.0.1", port))
    yield server
    server.relay.close()
    server.stop()


@pytest.fixture(scope="module")
def image(podman_service, default_image, pki, github_com) -> str:
    """The default image with the authority trusted and github.com reached through the relay."""
    proxy = f"http://127.0.0.1:{github_com.relay.port}"
    (pki / "Dockerfile").write_text(stand_in.dockerfile(default_image, proxy=proxy))
    name = build(podman_service, pki, f"localhost/rcman-test-git-relay:{os.getpid()}")
    yield name
    podman_service.cli("rmi", "--force", name)


STUB_WORKSPACE, ORPHAN_BUILDER, ORPHAN_VOLUME, ORPHAN_IMAGE, WARM_BUILD, WARM_WORKSPACE = range(
    810, 816
)


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


@pytest.fixture(scope="module")
def runtime_with(podman_service, engine_tools, fake_store, tmp_path_factory):
    """A watched runtime on the engine's own network with the module's workspace image and the
    builder image given, and a scratch folder of its own."""

    def made(workspace_image: str, builder_image: str) -> Watched:
        return engine_runtime(
            podman_service, workspace_image, engine_tools, fake_store,
            tmp_path_factory.mktemp("scratch"), kind=Watched, builder_image=builder_image,
        )  # fmt: skip

    return made


@pytest.fixture(scope="module", autouse=True)
def persons_volume_is_removed(runtime_with, image, person):
    """The person's volume of credentials and git files, which the first workspace made."""
    yield
    runtime_with(image, "").remove_account(person[0].id)


@pytest.fixture(scope="module")
def real_image(builder_image) -> str:
    """The builder image the installer builds, built on the engine from the same folder
    (tests/container/conftest.py): the Dev Containers CLI pinned by its lock, and a Podman of its
    own."""
    return builder_image


@pytest.fixture(scope="module")
def real_podman(runtime_with, image, real_image, person) -> Watched:
    """The runtime whose builder is the real one. The ranges it works in are warm: the builder
    image and the workspace image each have a copy in the range they run in, which Podman makes
    the first time and keeps, so that the layers the tests measure are only their own."""
    runtime = runtime_with(image, real_image)
    who = person[0]
    warm = rt.container_name(WARM_WORKSPACE)
    runtime.create(rt.Spec(warm, who.slot, WARM_WORKSPACE, who.id))
    runtime.remove(warm)
    runtime._podman("volume", "create", builder.builder_name(WARM_BUILD))
    runtime._podman(*builder.run_argv(WARM_BUILD, {}, runtime._ids(sizes.BUILDER_SLOT),
                                      real_image, network=runtime.config.network))  # fmt: skip
    runtime.remove_build(WARM_BUILD)
    return runtime


def service_of(runtime: rt.PodmanRuntime, person) -> Workspaces:
    """rcman's service on the runtime, with the builder in the suites' builder slot."""
    settings = engine_settings(person[1], runtime.config.claude_store)
    return Workspaces(settings, runtime, builder_slot=sizes.BUILDER_SLOT)


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
        assert not exists(service.runtime, "container", workspace.container)
        assert not exists(service.runtime, "volume", workspace.container)


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


def image_ids(runtime) -> set[str]:
    return images(runtime)


def storage_layers(runtime) -> set[str]:
    """The layers of the engine's storage, with the copies Podman makes of an image's layers
    for a range."""
    root = cli(runtime, "info", "--format", "{{.Store.GraphRoot}}").stdout.strip()
    assert root
    return set(os.listdir(Path(root) / "overlay"))


# --- the real builder ---------------------------------------------------------------------------

REAL_CONFIG = {
    "build": {"dockerfile": "Dockerfile", "context": ".."},
    "features": {"./tool": {"greeting": "from-the-file"}},
    "containerEnv": {"FROM_FILE": "yes"},
    "postCreateCommand": "echo post-create >> .order",
}
# Ubuntu with git, from a registry, as a repository's Dockerfile names its base.
GIT_BASE = "docker.io/library/buildpack-deps:noble-scm"
REAL_FILES = {
    ".devcontainer/devcontainer.json": json.dumps(REAL_CONFIG, indent=2),
    # A glibc base with git, for the workspace's checks, and a file of the repository. Nothing is
    # installed with apt, which drops to an account whose group (65534) a range of the suites'
    # size (tests/container/sizes.py) does not hold.
    ".devcontainer/Dockerfile": (
        f"FROM {GIT_BASE}\nCOPY .devcontainer/marker.txt /etc/marker\nENV FROM_DOCKERFILE=yes\n"
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
    layers, images_before = storage_layers(real_podman), image_ids(real_podman)

    # Two workspaces of the same person, built at once in the one range the builders have.
    first = start(real_service, person, real_clones, "real-one")
    second = real_service.create(person[0], "real-two", "https://github.com/octo/real-one")
    real_clones.append(second)
    one, two = (
        settled(real_service, first.id),
        settled(real_service, second.id),
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
        assert inspect(real_podman, workspace.container)["ImageName"] == names[workspace.id]
        assert real_podman.running(workspace.container)
    assert sorted(real_podman.copied) == sorted([one.id, two.id])  # each image copied out once

    assert storage_layers(real_podman) - layers  # positive: the images and their copies are layers

    # The tool the feature installed is on the PATH Claude's programs have, and its variables
    # are in the environment, from the image's own ENV.
    layout = real_service._layout(one)
    path = rt.child_env(layout)["PATH"].split(":")
    assert "/opt/mytool/bin" in path and path.index("/opt/mytool/bin") < path.index("/usr/bin")
    tool = run_in(real_podman, real_service, one, "mytool")
    assert tool.out.decode().strip() == "mytool from-the-file", tool
    seen = dict(
        line.split("=", 1)
        for line in out(run_in(real_podman, real_service, one, "env")).splitlines()
    )
    assert seen["MYTOOL_HOME"] == "/opt/mytool" and seen["FROM_DOCKERFILE"] == "yes"
    assert seen["FROM_FILE"] == "yes"
    marker = out(run_in(real_podman, real_service, one, "cat", "/etc/marker"))
    assert marker == "from the repository"  # the Dockerfile's COPY had the repository

    # The label has the feature's entry and no copy of the file's own, so the lifecycle commands
    # ran once each, the feature's first.
    label = cli(real_podman, "image", "inspect", "--format", "{{json .Labels}}", names[one.id])
    entries = json.loads(json.loads(label.stdout)[devcontainer.METADATA_LABEL])
    assert [e.get("id") for e in entries] == ["./tool"], entries
    assert lines_of(real_podman, real_service, one, ".order") == [
        "feature-oncreate",
        "post-create",
    ]

    # The builder, its volume and the image in it are gone; the progress reached the log, and
    # the time of the copy was logged.
    for workspace in (one, two):
        nothing = builder.builder_name(workspace.id)
        assert not exists(real_podman, "container", nothing) and not exists(
            real_podman, "volume", nothing
        )
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
    named = cli(real_podman, "images", "--all", "--format", "{{.Names}}").stdout
    assert "rcman-in-builder" not in named  # the name the image has in the builder

    # rcman restarting keeps what is used.
    again = Workspaces(real_service.settings, real_podman, builder_slot=sizes.BUILDER_SLOT)
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
    assert not exists(real_podman, "image", names[one.id]) and exists(
        real_podman, "image", names[two.id]
    )
    assert real_podman.running(two.container)
    assert run_in(real_podman, real_service, two, "mytool").out.decode().strip() == (
        "mytool from-the-file"
    )
    real_service.delete(real_service.get(two.id))
    gone(real_service, two.id)
    assert real_podman.built_images() == {} and not exists(real_podman, "image", names[two.id])
    # Nothing of the builds is left. Deleting a workspace collects the images nothing refers to,
    # so images that were here before (the pinned ones the host loaded) may be gone too: what is
    # asserted is that nothing is here that was not.
    assert image_ids(real_podman) <= images_before, sorted(image_ids(real_podman) - images_before)
    assert storage_layers(real_podman) <= layers, sorted(storage_layers(real_podman) - layers)
