"""The system image the container suites run rcman's installer and the worktree checks in:
built on the tests' engine (tests/podman_service.py) from tests/container/system/, or loaded
from the user's cache (tests/container/cache.py).

Not a test module. tests/conftest.py makes the ``system_image`` fixture of it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tests.container import cache
from tests.hang_guard import CONTAINER_HANG_GUARD

CONTEXT = Path(__file__).resolve().parent / "system"
REPOSITORY = "localhost/rcman-test-system"


def image_name() -> str:
    """The image's name, tagged after every file of its build context, names and content, as the
    builder image's is (installer/install.py ``devcontainer_builder_image``)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in CONTEXT.rglob("*") if p.is_file()):
        name = path.relative_to(CONTEXT).as_posix().encode()
        digest.update(len(name).to_bytes(4, "big") + name)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big") + content)
    return f"{REPOSITORY}:{digest.hexdigest()[:12]}"


def build(engine) -> str:
    """The system image on ``engine``, loaded from the user's cache (tests/container/cache.py),
    or built and kept there; its name."""
    name = image_name()

    def built() -> None:
        made = engine.cli(
            "build", "-q", "-t", name, "-f", str(CONTEXT / "Containerfile"), str(CONTEXT),
            timeout=CONTAINER_HANG_GUARD,
        )  # fmt: skip
        assert made.returncode == 0, (made.stdout + made.stderr).decode(errors="replace")

    return cache.image(engine, name, built)
