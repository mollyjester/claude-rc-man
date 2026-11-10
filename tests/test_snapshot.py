"""tests/snapshot.py: a checkout's files as on disk, chosen by the host's git, with no .git.

Each test runs over a worktree under its main checkout's ``.claude/worktrees/``, as Claude Code
lays one out, whose ``.git`` names a folder outside it.
"""

import os
from pathlib import Path

import pytest

from tests import snapshot as sn
from tests.git_fixtures import fixture_git, make_repo, write

# What the snapshot of every checkout below holds: what is tracked and still on disk,
# and what is untracked but neither ignored nor named like a credential.
EXPECTED = {
    ".gitignore",
    "README.md",
    "bin/tool.sh",
    "fixture.pem",
    "sub/deep/file.txt",
    "new.txt",
}
CHANGED = "changed on disk\n"


def _base(path: Path) -> Path:
    """A repository whose commit every checkout below starts from."""
    make_repo(path)
    write(path / ".gitignore", "ignored.txt\n.claude/worktrees/\n")
    tool = write(path / "bin" / "tool.sh", "#!/bin/sh\n")
    tool.chmod(0o755)
    write(path / "fixture.pem", "a tracked fixture named like a credential\n")
    write(path / "gone.txt")
    write(path / "sub" / "deep" / "file.txt", "deep\n")
    fixture_git(path, "add", "-A")
    fixture_git(path, "commit", "-q", "-m", "files")
    return path


def _worktree_inside(tmp_path: Path) -> Path:
    main = _base(tmp_path / "main")
    worktree = main / ".claude" / "worktrees" / "wt"
    fixture_git(main, "worktree", "add", "-q", "-b", "wt", str(worktree))
    return worktree


@pytest.fixture
def checkout(tmp_path) -> Path:
    """A checkout with work on disk: a tracked file changed, one deleted, and untracked
    files — one ignored, one new, and some named like credentials."""
    path = _worktree_inside(tmp_path)
    write(path / "README.md", CHANGED)
    (path / "gone.txt").unlink()
    write(path / "ignored.txt")
    write(path / "new.txt")
    write(path / ".env", "SECRET=1\n")
    write(path / "x.pem", "key\n")
    write(path / "sub" / "id_rsa", "key\n")
    return path


@pytest.fixture
def into(tmp_path) -> Path:
    """An empty folder, made as mkdtemp makes one: readable by its owner only."""
    folder = tmp_path / "snapshot"
    folder.mkdir()
    folder.chmod(0o700)
    return folder


def _files(root: Path) -> set[str]:
    return {
        str(Path(top, name).relative_to(root)) for top, _, names in os.walk(root) for name in names
    }


def test_it_holds_exactly_the_files_on_disk_that_git_would_take(checkout, into):
    """What is tracked and still on disk, with its contents and executable bit, and what is
    untracked but neither ignored nor named like a credential; no .git."""
    assert (checkout / ".git").is_file()  # positive control: the worktree's points outside it
    assert sn.snapshot(checkout, into) == into
    assert _files(into) == EXPECTED
    assert (into / "README.md").read_text() == CHANGED
    assert (into / "bin" / "tool.sh").stat().st_mode & 0o111 == 0o111
