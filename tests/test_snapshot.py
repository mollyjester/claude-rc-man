"""tests/snapshot.py: a checkout's files as on disk, chosen by the host's git, with no .git.

Each test runs over a plain clone, a worktree under its main checkout's
``.claude/worktrees/``, as Claude Code lays one out, and a worktree elsewhere.
"""

import os
import stat
import subprocess
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


def _clone(tmp_path: Path) -> Path:
    base = _base(tmp_path / "base")
    fixture_git(tmp_path, "clone", "-q", str(base), str(tmp_path / "clone"))
    return tmp_path / "clone"


def _worktree_inside(tmp_path: Path) -> Path:
    main = _base(tmp_path / "main")
    worktree = main / ".claude" / "worktrees" / "wt"
    fixture_git(main, "worktree", "add", "-q", "-b", "wt", str(worktree))
    return worktree


def _worktree_elsewhere(tmp_path: Path) -> Path:
    main = _base(tmp_path / "main")
    worktree = tmp_path / "elsewhere"
    fixture_git(main, "worktree", "add", "-q", "-b", "wt", str(worktree))
    return worktree


@pytest.fixture(
    params=[_clone, _worktree_inside, _worktree_elsewhere],
    ids=["clone", "worktree-inside", "worktree-elsewhere"],
)
def checkout(request, tmp_path) -> Path:
    """A checkout with work on disk: a tracked file changed, one deleted, and untracked
    files — one ignored, one new, and some named like credentials."""
    path = request.param(tmp_path)
    write(path / "README.md", CHANGED)
    (path / "gone.txt").unlink()
    write(path / "ignored.txt")
    write(path / "new.txt")
    write(path / ".env", "SECRET=1\n")
    write(path / "x.pem", "key\n")
    write(path / "sub" / "id_rsa", "key\n")
    (path / "sub").chmod(0o750)
    path.chmod(0o755)
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


def _listed(checkout: Path, env: dict[str, str], *which: str) -> list[str]:
    """What git lists in ``checkout`` in ``env``, as an inherited environment would."""
    proc = subprocess.run(
        ["git", "-C", str(checkout), "ls-files", *which],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.splitlines()


def test_it_holds_exactly_the_files_on_disk_that_git_would_take(checkout, into):
    assert sn.snapshot(checkout, into) == into
    assert _files(into) == EXPECTED


def test_tracked_files_have_their_working_tree_contents(checkout, into):
    sn.snapshot(checkout, into)
    assert (into / "README.md").read_text() == CHANGED


def test_an_ignored_file_is_left_out(checkout, into):
    assert (checkout / "ignored.txt").is_file()  # positive control: it is on disk
    sn.snapshot(checkout, into)
    assert not (into / "ignored.txt").exists()


def test_a_deleted_tracked_file_is_left_out(checkout, into):
    assert "gone.txt" in fixture_git(checkout, "ls-files").split()  # still tracked
    sn.snapshot(checkout, into)
    assert not (into / "gone.txt").exists()


def test_there_is_no_git_folder_or_file(checkout, into):
    assert (checkout / ".git").exists()  # positive control
    if checkout.name != "clone":
        assert (checkout / ".git").is_file()  # a worktree's points outside it
    sn.snapshot(checkout, into)
    assert not any(part == ".git" for top, dirs, _ in os.walk(into) for part in dirs)
    assert not (into / ".git").exists()


def test_untracked_files_named_like_credentials_are_left_out(checkout, into):
    for name in (".env", "x.pem", "sub/id_rsa"):
        assert (checkout / name).is_file()  # positive control: on disk, not ignored
        assert name in fixture_git(checkout, "ls-files", "--others", "--exclude-standard").split()
    sn.snapshot(checkout, into)
    for name in (".env", "x.pem", "sub/id_rsa"):
        assert not (into / name).exists()


def test_a_tracked_file_named_like_a_credential_is_kept(checkout, into):
    sn.snapshot(checkout, into)
    assert (into / "fixture.pem").read_text() == "a tracked fixture named like a credential\n"


def test_the_executable_bit_is_kept(checkout, into):
    sn.snapshot(checkout, into)
    assert (into / "bin" / "tool.sh").stat().st_mode & 0o111 == 0o111
    assert (into / "README.md").stat().st_mode & 0o111 == 0  # positive control


def test_folder_modes_are_copied_from_the_checkout(checkout, into):
    assert stat.S_IMODE(into.stat().st_mode) == 0o700  # positive control: as made
    sn.snapshot(checkout, into)
    assert stat.S_IMODE(into.stat().st_mode) == 0o755
    assert stat.S_IMODE((into / "sub").stat().st_mode) == 0o750
    assert stat.S_IMODE((into / "sub" / "deep").stat().st_mode) == stat.S_IMODE(
        (checkout / "sub" / "deep").stat().st_mode
    )


def test_an_inherited_git_dir_changes_nothing(checkout, into, tmp_path, monkeypatch):
    """The other repository tracks ignored.txt, which the checkout ignores and has on
    disk, so a snapshot that read it would hold that file."""
    other = make_repo(tmp_path / "other")
    write(other / "other.txt")
    write(other / "ignored.txt")
    fixture_git(other, "add", "other.txt", "ignored.txt")
    fixture_git(other, "commit", "-q", "-m", "other")
    assert (checkout / "ignored.txt").is_file()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    # positive control: git run in the inherited environment reads the other repository
    assert "ignored.txt" in _listed(checkout, dict(os.environ), "--cached")
    sn.snapshot(checkout, into)
    assert not (into / "ignored.txt").exists()
    assert _files(into) == EXPECTED
    assert (into / "README.md").read_text() == CHANGED


def test_a_global_excludes_file_changes_nothing(checkout, into, tmp_path, monkeypatch):
    home = tmp_path / "home"
    excludes = write(home / "excludes", "new.txt\n")
    write(home / ".gitconfig", f"[core]\n\texcludesFile = {excludes}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    # positive control: git run in the inherited environment leaves new.txt out
    assert "new.txt" not in _listed(checkout, dict(os.environ), "--others", "--exclude-standard")
    assert "README.md" in _listed(checkout, dict(os.environ), "--cached")
    sn.snapshot(checkout, into)
    assert _files(into) == EXPECTED


def test_a_personal_ignore_file_changes_nothing(checkout, into, tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = tmp_path / "config"
    write(config / "git" / "ignore", "new.txt\n")
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    # positive control: git run in the inherited environment leaves new.txt out
    assert "new.txt" not in _listed(checkout, dict(os.environ), "--others", "--exclude-standard")
    assert "README.md" in _listed(checkout, dict(os.environ), "--cached")
    sn.snapshot(checkout, into)
    assert _files(into) == EXPECTED
