"""Runs the suites in tests/container/, each inside a throwaway container: the podman
host, which runs systemd and rootless Podman.

Every test here takes the ``docker`` fixture, so it carries the ``container`` marker
(tests/container_marker.py). The fixture uses ``docker`` when the current user may,
otherwise ``sudo -n docker``. When neither works the tests skip, unless
``RCMAN_REQUIRE_CONTAINER=1`` (set by ``make test-container``, which ``make check``
runs), in which case they fail. Every container started here is labelled as its run's,
so a later run removes one a killed run left, with its anonymous volumes
(tests/containers.py). The checkout reaches a container as a snapshot of its files
(tests/snapshot.py), mounted read-only at /src.
"""

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from devtools import held_folders
from tests import containers
from tests import killed_runs as kr
from tests.container import podman_host as ph
from tests.git_fixtures import fixture_git, make_repo, write
from tests.snapshot import snapshot

REPO = Path(__file__).resolve().parent.parent
CONTEXT = REPO / "tests" / "container"
PODMAN = CONTEXT / "podman"
PYTEST = ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs"]
# Seconds the podman host's systemd may take to boot.
BOOTS_WITHIN = 300


@pytest.fixture(scope="session")
def docker() -> list[str]:
    """How to run docker here."""
    for prefix in (["docker"], ["sudo", "-n", "docker"]):
        try:
            if subprocess.run([*prefix, "info"], capture_output=True, timeout=30).returncode == 0:
                return prefix
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    if os.environ.get("RCMAN_REQUIRE_CONTAINER") == "1":
        pytest.fail("neither docker nor sudo -n docker works")
    pytest.skip(
        "docker is not usable (make test-container sets RCMAN_REQUIRE_CONTAINER=1 to fail instead)"
    )


def _built(docker: list[str], context: Path, name: str) -> str:
    """The image built from ``context``'s Dockerfile, its tag named after the
    Dockerfile's content, unless already built."""
    tag = name + ":" + hashlib.sha256((context / "Dockerfile").read_bytes()).hexdigest()[:12]
    if subprocess.run([*docker, "image", "inspect", tag], capture_output=True).returncode != 0:
        subprocess.run(
            [*docker, "build", "-q", "-t", tag, str(context)], check=True, capture_output=True
        )
    return tag


@pytest.fixture(scope="session")
def podman_image(docker) -> str:
    """The podman host's image, built from tests/container/podman/Dockerfile."""
    return _built(docker, PODMAN, "rcman-test-podman")


@pytest.fixture(scope="session")
def labelled(docker) -> list[str]:
    """docker run's arguments that label a container as this run's, held while the run
    lasts; containers a killed run left are removed first, and this run's afterwards
    (tests/containers.py)."""
    with containers.holder(docker) as folder:
        yield containers.labels(folder)


@pytest.fixture(scope="session")
def source(tmp_path_factory) -> Path:
    """The checkout's files as they are on disk (tests/snapshot.py), made once for the
    session and removed when it ends."""
    folder = tmp_path_factory.mktemp("src")
    try:
        yield snapshot(REPO, folder)
    finally:
        held_folders.remove_tree(folder, roots=[tmp_path_factory.getbasetemp()])


def test_docker_is_usable(docker):
    assert docker


def run_args(
    docker: list[str],
    labelled: list[str],
    source: Path,
    mounts: list[tuple[Path, str]] = (),
    env: dict[str, str] | None = None,
    docker_args: list[str] = (),
) -> list[str]:
    """docker run's arguments up to the image: a container removed when it ends, with
    ``source`` read-only at /src as its working folder."""
    args = [
        *docker,
        "run",
        "--rm",
        "--init",
        *labelled,
        *docker_args,
        "-v",
        f"{source}:/src:ro",
    ]  # an init reaps orphans, as on a real host
    for host, container in mounts:
        args += ["-v", f"{host}:{container}"]
    for key, value in {
        "RCMAN_IN_CONTAINER": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        **(env or {}),
    }.items():
        args += ["-e", f"{key}={value}"]
    return [*args, "-w", "/src"]


def _passed(proc: subprocess.CompletedProcess) -> None:
    """A suite's run passed, and ran rather than skipping."""
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert " skipped" not in output.splitlines()[-1], output


# --- the podman host ----------------------------------------------------------------------


def _base(dockerfile: Path) -> str:
    """The image a Dockerfile starts from."""
    for line in dockerfile.read_text().splitlines():
        if line.startswith("FROM "):
            return line.split()[1]
    raise AssertionError(f"no FROM in {dockerfile}")


# What a podman host is given to load into its account's Podman: the image the podman
# host starts from, which the workspaces and the default image start from too, and the
# one tmux is built in (installer/install.py BUILDER_IMAGE).
def _loaded_images() -> list[str]:
    from installer import install as ins

    return [_base(PODMAN / "Dockerfile"), ins.BUILDER_IMAGE.removeprefix("docker.io/library/")]


def _run(argv: list[str], timeout: float = 600, **kwargs) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def _booted(inside: list[str]) -> None:
    """Wait until the systemd ``inside`` runs has finished booting, and check that no
    unit failed."""
    deadline = time.monotonic() + BOOTS_WITHIN
    while True:
        state = subprocess.run(
            [*inside, "systemctl", "is-system-running", "--wait"],
            capture_output=True,
            text=True,
            timeout=BOOTS_WITHIN,
        ).stdout.strip()
        if state in ("running", "degraded"):
            break
        assert time.monotonic() < deadline, state  # "offline" until the manager is up
        time.sleep(0.5)
    failed = subprocess.run([*inside, "systemctl", "--failed"], capture_output=True, text=True)
    assert state == "running", failed.stdout


def _toolchain_cache() -> Path:
    """The host's cache of the installer's toolchain, filled with the installer's own
    download code: tmux's source among it."""
    from installer import install as ins

    cache = Path.home() / ".cache" / "rcman-test"
    ins.ensure_toolchain(cache / "toolchain")
    for name in ("npm", "uv"):
        (cache / name).mkdir(parents=True, exist_ok=True)
    return cache


@contextlib.contextmanager
def podman_container(docker, podman_image, labelled, source, docker_args=()):
    """A fresh podman host, booted, with the toolchain cache at the installer's
    toolchain: docker exec's arguments up to the command, which runs as root at /src,
    and the container's id. The account's home, and Podman's storage in it, is an
    anonymous volume of the container, which goes wherever the container goes, and
    survives a restart of the container; the container is removed afterwards."""
    from installer import install as ins

    cache = _toolchain_cache()
    for image in _loaded_images():
        if subprocess.run([*docker, "image", "inspect", image], capture_output=True).returncode:
            _run([*docker, "pull", "-q", image])
    container = ""
    try:
        container = _run(
            [*docker, "run", "-d", "--privileged", "--cgroupns=private",
             "--tmpfs", "/run", "--tmpfs", "/run/lock", *labelled, *docker_args,
             "-v", ph.HOME, "-v", f"{source}:/src:ro",
             "-v", f"{cache / 'toolchain'}:{ins.TOOLCHAIN}",
             "-v", f"{cache / 'npm'}:/home/dev/.npm", "-v", f"{cache / 'uv'}:/root/.cache/uv",
             podman_image]
        ).stdout.strip()  # fmt: skip
        inside = [*docker, "exec", "-w", "/src", "-e", "RCMAN_IN_CONTAINER=1"]
        inside += ["-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/src/backend:/src"]
        _booted([*inside, container])
        yield inside, container
    finally:
        if container:
            containers._remove(docker, [container])


def _load(docker, inside: list[str], container: str) -> str:
    """Load the images into the account's Podman by ``docker save | podman load``, so that
    nothing inside needs a registry; the name the first is loaded as."""
    names = []
    for image in _loaded_images():
        save = subprocess.Popen([*docker, "save", image], stdout=subprocess.PIPE)
        try:
            loaded = _run(
                [*inside, "-i", container, "python3", "tests/container/podman_host.py", "load"],
                stdin=save.stdout,
            )
        finally:
            save.stdout.close()
            assert save.wait(timeout=600) == 0
        said = loaded.stdout.strip().splitlines()[-1]
        assert said.startswith("Loaded image: "), loaded.stdout
        names.append(said.removeprefix("Loaded image: "))
    return names[0]


@pytest.fixture
def podman_host(docker, podman_image, labelled, source) -> list[str]:
    """A fresh podman host set up by tests/container/podman_host.py, with the images
    loaded into the account's Podman: docker exec's arguments up to the command, which
    runs as root at /src with ``RCMAN_PODMAN_IMAGE`` naming the loaded base image."""
    with podman_container(docker, podman_image, labelled, source) as (inside, container):
        _run([*inside, container, "python3", "tests/container/podman_host.py", "setup"])
        image = _load(docker, inside, container)
        yield [*inside, "-e", f"RCMAN_PODMAN_IMAGE={image}", container]


def test_podman_suite_passes(docker, podman_host):
    """Rootless Podman for a service account, under systemd as PID 1."""
    module = "tests/container/test_podman_in_container.py"
    _passed(
        subprocess.run(
            [*podman_host, *PYTEST, module], capture_output=True, text=True, timeout=1200
        )
    )


# The static tools and an empty Claude store, made as root with the installer's own code.
TOOLS_AND_STORE = """
from pathlib import Path
from installer import install as ins
ins.ensure_tools(Path("/var/lib/rcman/tools"), conf=Path("/src/backend/rcman/tmux.conf"))
Path("/var/lib/rcman/claude").mkdir(mode=0o755, exist_ok=True)
"""


def test_the_podman_runtime_keeps_the_contract_against_real_podman(docker, podman_host):
    """tests/container/test_runtime_in_podman.py, run as the service account."""
    _run([*podman_host, "python3", "-c", TOOLS_AND_STORE])
    module = "tests/container/test_runtime_in_podman.py"
    as_account = [
        "python3", "tests/container/podman_host.py", "run", "env", "RCMAN_IN_CONTAINER=1",
        "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=/src/backend:/src", "HOME=/var/lib/rcman",
    ]  # fmt: skip
    environment = [a for a in podman_host if a.startswith("RCMAN_PODMAN_IMAGE=")]
    _passed(
        subprocess.run(
            [*podman_host, *as_account, *environment, *PYTEST, module],
            capture_output=True,
            text=True,
            timeout=1200,
        )
    )


# --- rcman installed for real, in the podman host -------------------------------------


def _inside(inside: list[str], container: str, *argv: str, timeout: int = 2400):
    return subprocess.run(
        [*inside, container, *argv], capture_output=True, text=True, timeout=timeout
    )


def test_an_installation_works_and_comes_back_after_the_host_restarts(
    docker, podman_image, labelled, source
):
    """tests/container/test_install_in_container.py in a fresh podman host, through the
    real installer and systemd; then the whole host restarts, as a machine reboots, and
    tests/container/test_restored_in_container.py checks that rcman brought its
    workspaces and their servers back."""
    with podman_container(docker, podman_image, labelled, source) as (inside, container):
        env = ["-e", f"HOST_UID={os.getuid()}"]
        _passed(
            _inside(
                [*inside, *env], container, *PYTEST, "tests/container/test_install_in_container.py"
            )
        )
        _run([*docker, "restart", "--time", "60", container], timeout=600)
        _booted([*inside, container])
        _passed(
            _inside(inside, container, *PYTEST, "tests/container/test_restored_in_container.py")
        )


def test_the_whole_stack_in_a_browser(docker, podman_image, labelled, source):
    """tests/container/stack.py in a fresh podman host; the browser specs in
    frontend/e2e-stack, from the host, through the port it publishes, signing in to the
    fake claude with the code it accepts."""
    import socket

    from devtools.fake_claude import ACCEPTED_CODE as CODE

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    publish = ["-p", f"127.0.0.1:{port}:443"]
    with podman_container(docker, podman_image, labelled, source, publish) as (inside, container):
        started = _inside(
            [*inside, "-e", f"HOST_UID={os.getuid()}"], container, "python3",
            "tests/container/stack.py",
        )  # fmt: skip
        assert started.returncode == 0, started.stdout[-8000:] + started.stderr[-4000:]
        browser = subprocess.run(
            ["npx", "playwright", "test", "--config", "playwright.stack.config.ts",
             "--reporter=line"],
            cwd=REPO / "frontend",
            env={**os.environ, "RCMAN_STACK_PORT": str(port), "RCMAN_FAKE_CLAUDE_CODE": CODE},
            capture_output=True,
            text=True,
            timeout=1200,
        )  # fmt: skip
        assert browser.returncode == 0, browser.stdout[-8000:] + browser.stderr[-2000:]
        assert " passed" in browser.stdout and " skipped" not in browser.stdout
        checked = _inside(inside, container, "python3", "tests/container/stack.py", "check")
        assert checked.returncode == 0, checked.stdout[-4000:] + checked.stderr[-4000:]


# --- a checkout that is a git worktree ---------------------------------------------------

# Run in a container with a checkout at /src and tests/container at /tools: the clone
# make_clone makes, and git run from /src, as make_clone runs it.
CLONES = """
import json, sys
sys.path.insert(0, "/tools")
import clone
made = clone.make_clone()
user = clone.sh("git", "config", "--global", "--get", "user.name", user="dev").stdout.strip()
print(json.dumps({
    "files": clone.git("ls-tree", "-r", "--name-only", "HEAD", cwd=made).splitlines(),
    "readme": (made / "README.md").read_text(),
    "user": user,
}))
"""


def _worktree(tmp_path: Path) -> Path:
    """A git worktree, its .git a file naming a folder in its main checkout, with a
    tracked file changed and a file added on disk. forced.txt is tracked though its
    own .gitignore names it, as a file added with ``git add -f`` is."""
    main = make_repo(tmp_path / "main")
    write(main / ".gitignore", "forced.txt\n")
    write(main / "forced.txt")
    fixture_git(main, "add", ".gitignore")
    fixture_git(main, "add", "-f", "forced.txt")
    fixture_git(main, "commit", "-q", "-m", "forced")
    worktree = tmp_path / "wt"
    fixture_git(main, "worktree", "add", "-q", "-b", "wt", str(worktree))
    write(worktree / "README.md", "changed on disk\n")
    write(worktree / "added.txt")
    assert (worktree / ".git").is_file()
    return worktree


def _clone_at_src(docker, image, labelled, source, checkout):
    tools = source / "tests" / "container"
    args = run_args(
        docker, labelled, checkout, mounts=[(tools, "/tools:ro")], docker_args=["--entrypoint", ""]
    )
    return subprocess.run(
        [*args, image, "python3", "-c", CLONES], capture_output=True, text=True, timeout=600
    )


def test_a_worktrees_snapshot_is_cloned_in_a_container(
    docker, podman_image, labelled, source, tmp_path
):
    worktree = _worktree(tmp_path)
    copy = tmp_path / "copy"
    copy.mkdir()
    snapshot(worktree, copy)

    proc = _clone_at_src(docker, podman_image, labelled, source, copy)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = json.loads(proc.stdout.splitlines()[-1])
    assert sorted(found["files"]) == [".gitignore", "README.md", "added.txt", "forced.txt"]
    assert found["readme"] == "changed on disk\n"
    assert found["user"] == "dev"


def test_a_worktree_mounted_as_it_is_is_not_a_repository_in_a_container(
    docker, podman_image, labelled, source, tmp_path
):
    """The twin: the worktree itself at /src, its .git naming a folder the container
    does not have."""
    worktree = _worktree(tmp_path)

    proc = _clone_at_src(docker, podman_image, labelled, source, worktree)

    assert proc.returncode != 0
    assert "not a git repository" in proc.stdout + proc.stderr


# --- containers a killed run left -------------------------------------------------------

# What a probe container runs: long enough to outlive a check, never so long that one a
# failed test leaves runs on.
PROBE = ["sleep", "300"]


def _pairs(labels: list[str]) -> list[tuple[str, str]]:
    """docker run's --label arguments as (name, value)."""
    return [tuple(arg.split("=", 1)) for arg in labels[1::2]]


def _probe(docker: list[str], image: str, labels: list[str]) -> str:
    """A container labelled with ``labels``, with an anonymous volume, as the podman host
    has."""
    started = subprocess.run(
        [*docker, "run", "-d", "--rm", "--entrypoint", "", *labels, "-v", "/v", image, *PROBE],
        capture_output=True,
        text=True,
    )
    assert started.returncode == 0, started.stderr
    return started.stdout.strip()


def _volume_of(docker: list[str], container: str) -> str:
    found = subprocess.run(
        [*docker, "inspect", "--format", "{{range .Mounts}}{{.Name}}{{end}}", container],
        capture_output=True,
        text=True,
    )
    assert found.returncode == 0 and found.stdout.strip(), found.stderr
    return found.stdout.strip()


def _volume_exists(docker: list[str], volume: str) -> bool:
    found = subprocess.run([*docker, "volume", "ls", "-q"], capture_output=True, text=True)
    assert found.returncode == 0, found.stderr
    return volume in found.stdout.split()


def _exists(docker: list[str], container: str) -> bool:
    found = subprocess.run(
        [*docker, "ps", "-a", "-q", "--no-trunc", "--filter", f"id={container}"],
        capture_output=True,
        text=True,
    )
    assert found.returncode == 0, found.stderr
    return container in found.stdout.split()


USES_A_CONTAINER = """
import json, os
from tests import containers as ct
from tests import killed_runs as kr
from tests.test_container_suite import PROBE

ct.HOLDERS = {prefix!r}

def test_runs_one():
    docker, image = json.loads(os.environ["DOCKER"]), os.environ["IMAGE"]
    with ct.holder(docker) as folder:
        started = ct.subprocess.run(
            [*docker, "run", "-d", "--rm", "--entrypoint", "", *ct.labels(folder), "-v", "/v",
             image, *PROBE],
            capture_output=True, text=True, check=True,
        )
        container = started.stdout.strip()
        mounts = ct.subprocess.run(
            [*docker, "inspect", "--format", "{{{{range .Mounts}}}}{{{{.Name}}}}{{{{end}}}}",
             container],
            capture_output=True, text=True, check=True,
        )
        kr.report("one", {{"paths": {{"holder": str(folder)}}, "container": container,
                           "volume": mounts.stdout.strip()}})
        kr.hold()
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["SIGTERM", "SIGKILL"])
def test_the_next_run_removes_a_container_a_killed_run_left_and_its_volume(
    docker, podman_image, tmp_path, sig, monkeypatch
):
    """The killed run's holder, and the next run's, are named after this test's stem, so
    that no other test's run takes the container meanwhile. Its anonymous volume, as the
    podman host's home is, goes with it."""
    with kr.stem() as stem:
        monkeypatch.setattr(containers, "HOLDERS", stem + containers.HOLDERS)
        source = {"test_container.py": USES_A_CONTAINER.format(prefix=containers.HOLDERS)}
        env = {"DOCKER": json.dumps(docker), "IMAGE": podman_image}
        with kr.KilledRun(tmp_path, source, (stem,), env=env) as run:
            reported = run.reports.wait("one")
            run.kill(sig)
        container, volume = reported["container"], reported["volume"]
        try:
            # positive control: the kill left them
            assert _exists(docker, container) and _volume_exists(docker, volume)
            with containers.holder(docker):
                assert not _exists(docker, container)
                assert not _volume_exists(docker, volume)
        finally:
            _remove_reported(docker, reported)


def _remove_reported(docker: list[str], reported: dict) -> None:
    """Remove what a killed run reported starting: the containers labelled with its
    holder, a path the report's check has already confined to the test's stem, never a
    container the report merely names."""
    holder = reported["paths"]["holder"]
    containers._remove(docker, list(containers._labelled(docker, f"{containers.HOLDER}={holder}")))


def test_a_reported_container_is_removed_only_if_labelled_with_the_reported_holder(
    docker, podman_image, monkeypatch
):
    """Planted twin: a report naming a container that is not the run's own."""
    with kr.stem() as stem:
        monkeypatch.setattr(containers, "HOLDERS", stem + containers.HOLDERS)
        with containers.holder(docker) as folder:
            unlabelled = _probe(docker, podman_image, [])
            try:
                labelled = _probe(docker, podman_image, containers.labels(folder))
                reported = {"paths": {"holder": str(folder)}, "container": unlabelled}
                _remove_reported(docker, reported)
                assert _exists(docker, unlabelled)
                assert not _exists(docker, labelled)  # positive control: its own goes
            finally:
                subprocess.run([*docker, "rm", "-f", "-v", unlabelled], capture_output=True)


def test_a_running_runs_containers_are_left_and_removed_when_it_ends(
    docker, podman_image, monkeypatch
):
    with kr.stem() as stem:
        monkeypatch.setattr(containers, "HOLDERS", stem + containers.HOLDERS)
        _left_while_running(docker, podman_image)


def _left_while_running(docker, podman_image):
    with containers.holder(docker) as folder:
        mine = _probe(docker, podman_image, containers.labels(folder))
        volume = _volume_of(docker, mine)
        with containers.holder(docker):
            assert _exists(docker, mine)
        assert _exists(docker, mine) and _volume_exists(docker, volume)
    assert not _exists(docker, mine)
    assert not _volume_exists(docker, volume)


def test_a_container_of_another_place_is_left(docker, podman_image, monkeypatch):
    """One whose holder is gone, but that another boot or another /tmp started."""
    with kr.stem() as stem:
        monkeypatch.setattr(containers, "HOLDERS", stem + containers.HOLDERS)
        _another_place_is_left(docker, podman_image)


def _another_place_is_left(docker, podman_image):
    gone = held_folders.TMP / (containers.HOLDERS + "gone_abc")
    assert containers.abandoned({"x": dict(_pairs(containers.labels(gone)))}) == ["x"]
    labels = containers.labels(gone)
    elsewhere = [*labels[:-1], f"{containers.WHERE}=another-boot 1:2"]
    other = _probe(docker, podman_image, elsewhere)
    try:
        with containers.holder(docker):
            assert _exists(docker, other)
    finally:
        subprocess.run([*docker, "rm", "-f", "-v", other], capture_output=True)
