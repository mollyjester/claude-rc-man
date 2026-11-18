"""The Claude Code store the tests and the screenshots' world (devtools/world.py) give the host
runtime: the fake `claude` as the installer leaves the real one."""

from __future__ import annotations

import os
from pathlib import Path

from devtools.fake_claude import FAKE_CLAUDE, FAKE_VERSION


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
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "versions").mkdir(exist_ok=True)
    binary = root / "versions" / version
    binary.write_text(f'#!/bin/sh\n{before}{env} exec {FAKE_CLAUDE} "$@"\n', encoding="utf-8")
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
