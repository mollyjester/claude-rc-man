"""Runs the root-only suite in tests/container/ inside a throwaway container.

Every test here takes the ``docker`` fixture, so it carries the ``container`` marker
(tests/container_marker.py). The fixture uses ``docker`` when the current user may,
otherwise ``sudo -n docker``. When neither works the tests skip, unless
``RCMAN_REQUIRE_CONTAINER=1`` (set by ``make test-container``, which ``make check``
runs), in which case they fail. Every container started here is labelled as its run's,
so a later run removes one a killed run left (tests/containers.py).
"""

import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from devtools import held_folders
from tests import containers
from tests import killed_runs as kr

REPO = Path(__file__).resolve().parent.parent
CONTEXT = REPO / "tests" / "container"


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


@pytest.fixture(scope="session")
def container_image(docker) -> str:
    """The test image, built from tests/container/Dockerfile unless already built."""
    tag = (
        "rcman-test-admin:" + hashlib.sha256((CONTEXT / "Dockerfile").read_bytes()).hexdigest()[:12]
    )
    if subprocess.run([*docker, "image", "inspect", tag], capture_output=True).returncode != 0:
        subprocess.run(
            [*docker, "build", "-q", "-t", tag, str(CONTEXT)], check=True, capture_output=True
        )
    return tag


@pytest.fixture(scope="session")
def labelled(docker) -> list[str]:
    """docker run's arguments that label a container as this run's, held while the run
    lasts; containers a killed run left are removed first, and this run's afterwards
    (tests/containers.py)."""
    with containers.holder(docker) as folder:
        yield containers.labels(folder)


def test_docker_is_usable(docker):
    assert docker


def run_suite(
    docker: list[str],
    image: str,
    labelled: list[str],
    module: str,
    mounts: list[tuple[Path, str]] = (),
    env: dict[str, str] | None = None,
    docker_args: list[str] = (),
):
    args = [
        *docker,
        "run",
        "--rm",
        "--init",
        *labelled,
        *docker_args,
        "-v",
        f"{REPO}:/src:ro",
    ]  # an init reaps orphans, as on a real host
    for host, container in mounts:
        args += ["-v", f"{host}:{container}"]
    for key, value in {
        "RCMAN_IN_CONTAINER": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        **(env or {}),
    }.items():
        args += ["-e", f"{key}={value}"]
    args += [
        "-w",
        "/src",
        image,
        "python3",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-rs",
        module,
    ]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=2400)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert " skipped" not in output.splitlines()[-1], output  # it ran, rather than skipping


def test_user_management_suite_passes(docker, container_image, labelled):
    run_suite(docker, container_image, labelled, "tests/container/test_admin_in_container.py")


def test_github_suite_passes(docker, container_image, labelled):
    """No network, and github.com is this container's own 127.0.0.1, where the suite
    serves the tests' GitHub."""
    run_suite(
        docker,
        container_image,
        labelled,
        "tests/container/test_github_in_container.py",
        docker_args=["--network", "none", "--add-host", "github.com:127.0.0.1"],
    )


def test_install_suite_passes(docker, container_image, labelled):
    """A fresh container; the toolchain comes from a host cache the installer's code fills."""
    from installer import install as ins

    cache = Path.home() / ".cache" / "rcman-test"
    ins.ensure_toolchain(cache / "toolchain")
    for name in ("npm", "uv"):
        (cache / name).mkdir(parents=True, exist_ok=True)
    run_suite(
        docker,
        container_image,
        labelled,
        "tests/container/test_install_in_container.py",
        mounts=[
            (cache / "toolchain", str(ins.TOOLCHAIN)),
            (cache / "npm", "/home/dev/.npm"),
            (cache / "uv", "/root/.cache/uv"),
        ],
        env={"HOST_UID": str(os.getuid())},
    )


def test_the_whole_stack_in_a_browser(docker, container_image, labelled):
    """tests/container/stack.py in a fresh container; the browser specs in frontend/e2e-stack."""
    import socket
    import time

    from installer import install as ins

    cache = Path.home() / ".cache" / "rcman-test"
    ins.ensure_toolchain(cache / "toolchain")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"rcman-stack-{os.getpid()}"
    args = [*docker, "run", "-d", "--name", name, "--init", *labelled]
    args += ["-p", f"127.0.0.1:{port}:443"]
    args += ["-v", f"{REPO}:/src:ro", "-v", f"{cache / 'toolchain'}:{ins.TOOLCHAIN}"]
    args += ["-v", f"{cache / 'npm'}:/home/dev/.npm", "-v", f"{cache / 'uv'}:/root/.cache/uv"]
    args += ["-e", f"HOST_UID={os.getuid()}", "-e", "PYTHONDONTWRITEBYTECODE=1"]
    args += [container_image, "python3", "/src/tests/container/stack.py"]
    subprocess.run(args, check=True, capture_output=True)
    try:
        deadline = time.monotonic() + 1200
        while True:
            logs = subprocess.run([*docker, "logs", name], capture_output=True, text=True)
            output = logs.stdout + logs.stderr
            if "READY" in output.splitlines():
                break
            running = subprocess.run(
                [*docker, "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True,
                text=True,
            )
            assert running.stdout.strip() == "true" and time.monotonic() < deadline, output[-5000:]
            time.sleep(2)
        browser = subprocess.run(
            [
                "npx",
                "playwright",
                "test",
                "--config",
                "playwright.stack.config.ts",
                "--reporter=line",
            ],
            cwd=REPO / "frontend",
            env={**os.environ, "RCMAN_STACK_PORT": str(port)},
            capture_output=True,
            text=True,
            timeout=1200,
        )
        assert browser.returncode == 0, browser.stdout[-8000:] + browser.stderr[-2000:]
        assert " passed" in browser.stdout and " skipped" not in browser.stdout

        def inside(*argv):
            return subprocess.run([*docker, "exec", name, *argv], capture_output=True, text=True)

        assert inside("getent", "passwd", "carol").returncode != 0
        assert inside("test", "-d", "/home/carol").returncode == 0  # kept
        assert inside("getent", "passwd", "dave").returncode != 0
        assert inside("test", "-e", "/home/dave").returncode != 0  # deleted
    finally:
        subprocess.run([*docker, "rm", "-f", name], capture_output=True)


# --- containers a killed run left -------------------------------------------------------

# What a probe container runs: long enough to outlive a check, never so long that one a
# failed test leaves runs on.
PROBE = ["sleep", "300"]


def _pairs(labels: list[str]) -> list[tuple[str, str]]:
    """docker run's --label arguments as (name, value)."""
    return [tuple(arg.split("=", 1)) for arg in labels[1::2]]


def _probe(docker: list[str], image: str, labels: list[str]) -> str:
    started = subprocess.run(
        [*docker, "run", "-d", "--rm", *labels, image, *PROBE], capture_output=True, text=True
    )
    assert started.returncode == 0, started.stderr
    return started.stdout.strip()


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
            [*docker, "run", "-d", "--rm", *ct.labels(folder), image, *PROBE],
            capture_output=True, text=True, check=True,
        )
        container = started.stdout.strip()
        kr.report("one", {{"paths": {{"holder": str(folder)}}, "container": container}})
        kr.hold()
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["SIGTERM", "SIGKILL"])
def test_the_next_run_removes_a_container_a_killed_run_left(
    docker, container_image, tmp_path, sig, monkeypatch
):
    """The killed run's holder, and the next run's, are named after this test's stem, so
    that no other test's run takes the container meanwhile."""
    with kr.stem() as stem:
        monkeypatch.setattr(containers, "HOLDERS", stem + containers.HOLDERS)
        source = {"test_container.py": USES_A_CONTAINER.format(prefix=containers.HOLDERS)}
        env = {"DOCKER": json.dumps(docker), "IMAGE": container_image}
        with kr.KilledRun(tmp_path, source, (stem,), env=env) as run:
            reported = run.reports.wait("one")
            run.kill(sig)
        container = reported["container"]
        try:
            assert _exists(docker, container)  # positive control: the kill left it
            with containers.holder(docker):
                assert not _exists(docker, container)
        finally:
            _remove_reported(docker, reported)


def _remove_reported(docker: list[str], reported: dict) -> None:
    """Remove what a killed run reported starting: the containers labelled with its
    holder, a path the report's check has already confined to the test's stem, never a
    container the report merely names."""
    holder = reported["paths"]["holder"]
    containers._remove(docker, list(containers._labelled(docker, f"{containers.HOLDER}={holder}")))


def test_a_reported_container_is_removed_only_if_labelled_with_the_reported_holder(
    docker, container_image, monkeypatch
):
    """Planted twin: a report naming a container that is not the run's own."""
    with kr.stem() as stem:
        monkeypatch.setattr(containers, "HOLDERS", stem + containers.HOLDERS)
        with containers.holder(docker) as folder:
            unlabelled = _probe(docker, container_image, [])
            try:
                labelled = _probe(docker, container_image, containers.labels(folder))
                reported = {"paths": {"holder": str(folder)}, "container": unlabelled}
                _remove_reported(docker, reported)
                assert _exists(docker, unlabelled)
                assert not _exists(docker, labelled)  # positive control: its own goes
            finally:
                subprocess.run([*docker, "rm", "-f", unlabelled], capture_output=True)


def test_a_running_runs_containers_are_left_and_removed_when_it_ends(
    docker, container_image, monkeypatch
):
    with kr.stem() as stem:
        monkeypatch.setattr(containers, "HOLDERS", stem + containers.HOLDERS)
        _left_while_running(docker, container_image)


def _left_while_running(docker, container_image):
    with containers.holder(docker) as folder:
        mine = _probe(docker, container_image, containers.labels(folder))
        with containers.holder(docker):
            assert _exists(docker, mine)
        assert _exists(docker, mine)
    assert not _exists(docker, mine)


def test_a_container_of_another_place_is_left(docker, container_image, monkeypatch):
    """One whose holder is gone, but that another boot or another /tmp started."""
    with kr.stem() as stem:
        monkeypatch.setattr(containers, "HOLDERS", stem + containers.HOLDERS)
        _another_place_is_left(docker, container_image)


def _another_place_is_left(docker, container_image):
    gone = held_folders.TMP / (containers.HOLDERS + "gone_abc")
    assert containers.abandoned({"x": dict(_pairs(containers.labels(gone)))}) == ["x"]
    labels = containers.labels(gone)
    elsewhere = [*labels[:-1], f"{containers.WHERE}=another-boot 1:2"]
    other = _probe(docker, container_image, elsewhere)
    try:
        with containers.holder(docker):
            assert _exists(docker, other)
    finally:
        subprocess.run([*docker, "rm", "-f", other], capture_output=True)
