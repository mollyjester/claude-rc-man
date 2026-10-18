"""The system image the container suites run rcman's installer, the whole stack and the worktree
checks in: built on the tests' engine (tests/podman_service.py) from tests/container/system/.

Not a test module. tests/conftest.py makes the ``system_image`` fixture of it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CONTEXT = Path(__file__).resolve().parent / "system"
REPOSITORY = "localhost/rcman-test-system"
# Seconds a build of an image on the engine may take: its packages come from the network.
BUILT_WITHIN = 1800


def image_name() -> str:
    """The image's name, tagged after its build file's content."""
    digest = hashlib.sha256((CONTEXT / "Containerfile").read_bytes()).hexdigest()[:12]
    return f"{REPOSITORY}:{digest}"


def build(engine) -> str:
    """The system image on ``engine``, built unless it is there; its name."""
    name = image_name()
    if engine.cli("image", "exists", name).returncode != 0:
        built = engine.cli(
            "build", "-q", "-t", name, "-f", str(CONTEXT / "Containerfile"), str(CONTEXT),
            timeout=BUILT_WITHIN,
        )  # fmt: skip
        assert built.returncode == 0, (built.stdout + built.stderr).decode(errors="replace")
    return name
