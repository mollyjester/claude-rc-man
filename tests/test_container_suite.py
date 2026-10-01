"""Runs the root-only suite in tests/container/ inside a throwaway container.

Uses ``docker`` when the current user may, otherwise ``sudo -n docker``. When
neither works the test skips, unless ``RCMAN_REQUIRE_CONTAINER=1`` (set by ``make check``),
in which case it fails.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CONTEXT = REPO / "tests" / "container"
REQUIRED = os.environ.get("RCMAN_REQUIRE_CONTAINER") == "1"


def docker() -> list[str] | None:
    for prefix in (["docker"], ["sudo", "-n", "docker"]):
        try:
            if subprocess.run([*prefix, "info"], capture_output=True, timeout=30).returncode == 0:
                return prefix
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


DOCKER = docker()

pytestmark = pytest.mark.skipif(
    DOCKER is None and not REQUIRED,
    reason="docker is not usable (make check sets RCMAN_REQUIRE_CONTAINER=1 to fail instead)",
)


def image() -> str:
    tag = (
        "rcman-test-admin:" + hashlib.sha256((CONTEXT / "Dockerfile").read_bytes()).hexdigest()[:12]
    )
    if subprocess.run([*DOCKER, "image", "inspect", tag], capture_output=True).returncode != 0:
        subprocess.run(
            [*DOCKER, "build", "-q", "-t", tag, str(CONTEXT)], check=True, capture_output=True
        )
    return tag


def test_docker_is_usable():
    assert DOCKER is not None, "neither docker nor sudo -n docker works"


def test_container_suite_passes():
    proc = subprocess.run(
        [
            *DOCKER, "run", "--rm", "--init",  # an init reaps orphans, as on a real host
            "-v", f"{REPO}:/src:ro",
            "-e", "RCMAN_IN_CONTAINER=1",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-w", "/src",
            image(),
            "python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", "tests/container",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )  # fmt: skip
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert " skipped" not in output.splitlines()[-1], output  # it ran, rather than skipping
