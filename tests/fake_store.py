"""The Claude Code store the tests give the host runtime: the fake `claude` as the installer
leaves the real one. Not a test module."""

from __future__ import annotations

import os
from pathlib import Path

# The version the fake store holds unless a test asks for another.
FAKE_VERSION = "1.0.0"


def fake_claude(
    root: Path,
    env: str = "FAKE_CLAUDE_READY_SECONDS=0.3",
    version: str = FAKE_VERSION,
    before: str = "",
) -> Path:
    """The Claude Code store at ``root`` as the installer leaves it, holding ``version``: a
    `claude` in ``versions`` that runs the fake (devtools/fake_claude.py) with ``env``, after
    the shell commands ``before``; ``current`` naming it, and ``bin/claude`` leading to
    ``current``. The file is written again, in place, when it is there already. Its path."""
    from devtools import record_claude as rec

    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "versions").mkdir(exist_ok=True)
    binary = root / "versions" / version
    binary.write_text(f'#!/bin/sh\n{before}{env} exec {rec.FAKE_CLAUDE} "$@"\n', encoding="utf-8")
    binary.chmod(0o755)
    for folder, name, target in (
        (root, "current", f"versions/{version}"),
        (root / "bin", "claude", "../current"),
    ):
        if not (folder / name).is_symlink() or os.readlink(folder / name) != target:
            (folder / f".{name}.new").unlink(missing_ok=True)
            (folder / f".{name}.new").symlink_to(target)
            os.replace(folder / f".{name}.new", folder / name)
    return binary
