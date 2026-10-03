"""Runs the root-only suite in tests/container/ inside a throwaway container.

Every test here takes the ``docker`` fixture, so it carries the ``container`` marker
(tests/container_marker.py). The fixture uses ``docker`` when the current user may,
otherwise ``sudo -n docker``. When neither works the tests skip, unless
``RCMAN_REQUIRE_CONTAINER=1`` (set by ``make check``), in which case they fail.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

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
    pytest.skip("docker is not usable (make check sets RCMAN_REQUIRE_CONTAINER=1 to fail instead)")


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


def test_docker_is_usable(docker):
    assert docker


def run_suite(
    docker: list[str],
    image: str,
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


def test_user_management_suite_passes(docker, container_image):
    run_suite(docker, container_image, "tests/container/test_admin_in_container.py")


def test_github_suite_passes(docker, container_image):
    """No network, and github.com is this container's own 127.0.0.1, where the suite
    serves the tests' GitHub."""
    run_suite(
        docker,
        container_image,
        "tests/container/test_github_in_container.py",
        docker_args=["--network", "none", "--add-host", "github.com:127.0.0.1"],
    )


def test_install_suite_passes(docker, container_image):
    """A fresh container; the toolchain comes from a host cache the installer's code fills."""
    from installer import install as ins

    cache = Path.home() / ".cache" / "rcman-test"
    ins.ensure_toolchain(cache / "toolchain")
    for name in ("npm", "uv"):
        (cache / name).mkdir(parents=True, exist_ok=True)
    run_suite(
        docker,
        container_image,
        "tests/container/test_install_in_container.py",
        mounts=[
            (cache / "toolchain", str(ins.TOOLCHAIN)),
            (cache / "npm", "/home/dev/.npm"),
            (cache / "uv", "/root/.cache/uv"),
        ],
        env={"HOST_UID": str(os.getuid())},
    )


def test_the_whole_stack_in_a_browser(docker, container_image):
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
    args = [*docker, "run", "-d", "--name", name, "--init", "-p", f"127.0.0.1:{port}:443"]
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
