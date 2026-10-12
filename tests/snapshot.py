"""A copy of a checkout's files as they are on disk, with no ``.git``.

Not a test module. tests/test_container_suite.py mounts the copy read-only at
``/src`` in its containers, so nothing there depends on how the host's git
lays the checkout out: a clone and a git worktree, whose ``.git`` is a file
pointing outside it, give the same copy.

The host's git chooses the files: those tracked, and those untracked that are
not ignored, with what is on disk. Git runs in the fixtures' environment
(``fixture_env`` in tests/git_fixtures.py), so no inherited ``GIT_*``
variable, global or system configuration or personal ignore file changes the
choice. An untracked file named like a credential, as the pre-test hook
judges one (``named_like_a_credential`` in devtools/pre_test_commit.py), is
left out; a tracked one is copied. A link is copied as what it points at, and
one to a folder, or to nothing, is left out. Each file keeps its mode, and
each folder, the copy's own included, takes its mode from the checkout's.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from devtools.pre_test_commit import named_like_a_credential
from tests.git_fixtures import FIXTURE_HOME, fixture_env


def _listed(checkout: Path, *which: str) -> list[str]:
    """The paths ``git ls-files`` lists in ``checkout`` with the options ``which``."""
    proc = subprocess.run(
        ["git", "-C", str(checkout), "ls-files", "-z", *which],
        env=fixture_env(FIXTURE_HOME),
        capture_output=True,
        check=True,
    )
    return [os.fsdecode(name) for name in proc.stdout.split(b"\0") if name]


def snapshot(checkout: Path, into: Path) -> Path:
    """Copy ``checkout``'s files into ``into``, an empty folder, and return it."""
    tracked = _listed(checkout, "--cached")
    untracked = [
        name
        for name in _listed(checkout, "--others", "--exclude-standard")
        if not named_like_a_credential(name)
    ]
    folders = {Path()}
    for name in sorted(set(tracked) | set(untracked)):
        source = checkout / name
        if not source.is_file():
            continue
        target = into / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        folders.update(Path(name).parents)
    # Deepest first, so that a folder's mode never keeps its own copy from reaching inside.
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        (into / folder).chmod(stat.S_IMODE((checkout / folder).stat().st_mode))
    return into
