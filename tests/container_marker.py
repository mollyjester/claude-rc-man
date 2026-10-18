"""The container marker, derived: a test carries it exactly when it uses one of FIXTURES, the
tests' own Podman engine (tests/podman_service.py), directly or through another fixture.

Not a test module, and not conftest.py, which pytest may import twice.
"""

from __future__ import annotations

MARKER = "container"
FIXTURES = ("podman_service",)


def mark(items) -> None:
    """Add the marker to every collected item that uses one of FIXTURES."""
    import pytest

    for item in items:
        if any(name in item.fixturenames for name in FIXTURES):
            item.add_marker(getattr(pytest.mark, MARKER))
