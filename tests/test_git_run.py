"""helpers/git_run.py: reading what git writes, against real git, and the refusal built from
a command's result."""

import subprocess
from pathlib import Path

from helpers import git_run as g
from tests.git_fixtures import (
    add_submodule,
    fixture_git,
    make_repo,
    write,
)

ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "A",
    "GIT_AUTHOR_EMAIL": "a@example.com",
    "GIT_COMMITTER_NAME": "A",
    "GIT_COMMITTER_EMAIL": "a@example.com",
}


def run_git(repo: Path, args: list[str]) -> bytes:
    proc = subprocess.run(["git", *args], cwd=repo, env=ENV, capture_output=True, check=True)
    return proc.stdout


def status_records(repo: Path) -> list[bytes]:
    args = ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=normal"]
    return [r for r in run_git(repo, args).split(b"\0") if r]


# --- what git writes ------------------------------------------------------------


def every_state(tmp_path) -> list[Path]:
    """Repositories that between them make git write every kind of record."""
    fresh = make_repo(tmp_path / "unborn-parent") / "unborn"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "main")
    write(fresh / "new.txt")
    fixture_git(fresh, "add", "new.txt")

    origin = tmp_path / "origins" / "o.git"
    repo = make_repo(tmp_path / "repo", origin=origin)
    fixture_git(repo, "config", "status.renames", "copies")
    fixture_git(repo, "config", "diff.renames", "copies")
    write(repo / "modify.txt")
    write(repo / "delete.txt")
    write(repo / "type.txt")
    write(repo / "rename.txt", g_body("rename"))
    write(repo / "copy.txt", g_body("copy"))
    fixture_git(repo, "add", ".")
    fixture_git(repo, "commit", "-q", "-m", "files")
    write(repo / "local.txt")
    fixture_git(repo, "add", "local.txt")
    fixture_git(repo, "commit", "-q", "-m", "local")  # two ahead of origin
    write(repo / "modify.txt", "changed\n")  # modified, not staged
    (repo / "delete.txt").unlink()  # deleted, not staged
    (repo / "type.txt").unlink()
    (repo / "type.txt").symlink_to("modify.txt")  # type changed
    fixture_git(repo, "mv", "rename.txt", "renamed.txt")  # renamed, staged
    write(repo / "copied.txt", g_body("copy"))
    write(repo / "copy.txt", g_body("copy") + "a source must change to be seen as one\n")
    fixture_git(repo, "add", "copied.txt", "copy.txt")  # copied, staged
    write(repo / "added.txt")
    fixture_git(repo, "add", "added.txt")  # added
    write(repo / "untracked.txt")
    sub_source = make_repo(tmp_path / "sub-source")
    add_submodule(repo, sub_source, "sub")

    conflict = make_repo(tmp_path / "conflict")
    fixture_git(conflict, "switch", "-q", "-c", "other")
    write(conflict / "README.md", "theirs\n")
    fixture_git(conflict, "commit", "-q", "-am", "theirs")
    fixture_git(conflict, "switch", "-q", "main")
    write(conflict / "README.md", "ours\n")
    fixture_git(conflict, "commit", "-q", "-am", "ours")
    subprocess.run(
        ["git", "merge", "-q", "other"], cwd=conflict, env=fixture_git_env(), capture_output=True
    )

    detached = make_repo(tmp_path / "detached")
    fixture_git(detached, "switch", "-q", "--detach", "HEAD")
    return [fresh, repo, conflict, detached]


def fixture_git_env():
    from tests.git_fixtures import FIXTURE_HOME, fixture_env

    return fixture_env(FIXTURE_HOME)


def g_body(seed: str) -> str:
    return "".join(f"{seed} line {i} long enough for git to find it again\n" for i in range(30))


def test_every_record_kind_real_git_writes_is_read(tmp_path):
    repos = every_state(tmp_path)
    kinds, changes, branches = set(), [], []
    for repo in repos:
        records = status_records(repo)
        kinds |= {r[:1] for r in records if r}
        branch = g.Branch()
        changes += list(g.parse_status(iter(records), branch))
        branches.append(branch)
    # every kind is produced at least once, so the parser is driven over each
    assert {b"#", b"1", b"2", b"u", b"?"} <= kinds
    assert {c.change for c in changes} == set(g.CHANGES)
    fresh, repo, _conflict, detached = branches
    assert (fresh.name, fresh.commit) == ("main", None)
    assert repo.name == "main" and repo.upstream == "origin/main"
    assert (repo.ahead, repo.behind) == (2, 0)
    assert len(repo.commit) >= 40
    assert detached.name is None and detached.commit
    renamed = next(c for c in changes if c.change == "renamed")
    assert (renamed.path, renamed.origin, renamed.staged) == (b"renamed.txt", b"rename.txt", True)
    copied = next(c for c in changes if c.change == "copied")
    assert (copied.path, copied.origin) == (b"copied.txt", b"copy.txt")
    assert next(c for c in changes if c.change == "type_changed").path == b"type.txt"
    assert any(c.submodule for c in changes)
    assert not any(c.submodule for c in changes if c.path != b"sub")
    unmerged = next(c for c in changes if c.change == "unmerged")
    assert (unmerged.path, unmerged.staged) == (b"README.md", False)


# --- a command's refusal ----------------------------------------------------------


def test_a_refusal_carries_what_git_said_and_its_exit_status():
    failed = g.failure(128, b"", b"fatal: not a git repository\n")
    assert isinstance(failed, g.GitFailed)
    assert (failed.lines, failed.more, failed.exit_code) == (
        ["fatal: not a git repository"],
        0,
        128,
    )
