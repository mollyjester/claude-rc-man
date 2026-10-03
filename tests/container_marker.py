"""The container marker, derived: a test carries it exactly when it uses the docker
fixture, directly or through another fixture.

Not a test module, and not conftest.py, which pytest may import twice.
"""

from __future__ import annotations

MARKER = "container"
FIXTURE = "docker"


def mark(items) -> None:
    """Add the marker to every collected item that uses FIXTURE."""
    import pytest

    for item in items:
        if FIXTURE in item.fixturenames:
            item.add_marker(getattr(pytest.mark, MARKER))
