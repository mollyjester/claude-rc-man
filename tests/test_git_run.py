"""helpers/git_run.py: reading what git writes, against real git, and the refusal built from
a command's result."""

import subprocess
from pathlib import Path

import pytest

from helpers import git_run as g
from tests.git_fixtures import (
    add_submodule,
    fixture_git,
    make_repo,
    push_branch_to_origin,
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


def parsed(repo: Path) -> tuple[g.Branch, list[g.Change]]:
    branch = g.Branch()
    return branch, list(g.parse_status(status_records(repo), branch))


def hook(repo: Path, name: str, body: str) -> Path:
    path = repo / ".git" / "hooks" / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


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


def test_a_file_changed_in_both_places_is_in_both_lists(tmp_path):
    repo = make_repo(tmp_path / "r")
    write(repo / "README.md", "staged\n")
    fixture_git(repo, "add", "README.md")
    write(repo / "README.md", "and again\n")
    _, changes = parsed(repo)
    assert [(c.path, c.change, c.staged) for c in changes] == [
        (b"README.md", "modified", True),
        (b"README.md", "modified", False),
    ]


def test_names_with_spaces_and_newlines_are_read_whole(tmp_path):
    repo = make_repo(tmp_path / "r")
    write(repo / "a b\nc.txt")
    _, changes = parsed(repo)
    assert [c.path for c in changes] == [b"a b\nc.txt"]


@pytest.mark.parametrize(
    "records",
    [
        [b"# branch.oid (initial)", b"z unknown record"],
        [b"1 Z. N... 100644 100644 100644 0 0 x"],
        [b"1 .Q N... 100644 100644 100644 0 0 x"],
        [b"1 M. N..."],
        [b"2 R. N... 100644 100644 100644 0 0 R100 new"],  # no origin record follows
    ],
)
def test_a_record_git_is_not_known_to_write_is_refused(records):
    with pytest.raises(g.GitUnrecognised):
        list(g.parse_status(iter(records), g.Branch()))


def test_the_refusal_check_reads_a_known_record():
    """Positive control for the plant above: the same record with a known letter is read."""
    [change] = g.parse_status(iter([b"1 M. N... 100644 100644 100644 0 0 x"]), g.Branch())
    assert (change.path, change.change, change.staged) == (b"x", "modified", True)


def test_an_unknown_header_is_left_alone():
    branch = g.Branch()
    assert list(g.parse_status(iter([b"# branch.future x", b"# stash 1"]), branch)) == []


def test_branches_are_read_and_symbolic_refs_left_out(tmp_path):
    origin = tmp_path / "origins" / "o.git"
    repo = make_repo(tmp_path / "r", origin=origin)
    push_branch_to_origin(repo, origin, "later")
    fixture_git(repo, "fetch", "-q", "origin")
    fixture_git(repo, "remote", "set-head", "origin", "main")
    assert "refs/remotes/origin/HEAD" in fixture_git(repo, "for-each-ref")  # the plant
    args = ["for-each-ref", f"--format={g.BRANCH_FORMAT}", "refs/heads", "refs/remotes"]
    records = [r for r in run_git(repo, args).split(b"\n") if r]
    refs = list(g.parse_branches(records))
    assert [(r.remote, r.name, r.current, r.upstream) for r in refs] == [
        (False, "main", True, "refs/remotes/origin/main"),
        (True, "origin/feature", False, None),
        (True, "origin/later", False, None),
        (True, "origin/main", False, None),
    ]


def test_a_branch_record_of_another_shape_is_refused():
    with pytest.raises(g.GitUnrecognised):
        list(g.parse_branches(iter([b"refs/heads/main\0\0"])))
    with pytest.raises(g.GitUnrecognised):
        list(g.parse_branches(iter([b"refs/tags/v1\0\0\0 "])))


def test_the_system_git_is_at_least_the_version_rcman_needs():
    assert g.version_of(run_git(Path("/"), ["--version"]).decode()) >= g.GIT_MIN_VERSION


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("git version 2.43.0\n", (2, 43)),
        ("git version 2.31.1.windows.1\n", (2, 31)),
        ("git version 3.0\n", (3, 0)),
    ],
)
def test_a_version_is_read_from_gits_answer(text, version):
    assert g.version_of(text) == version


def test_an_answer_with_no_version_is_unrecognised():
    with pytest.raises(g.GitUnrecognised):
        g.version_of("hello\n")


# --- a command's refusal ----------------------------------------------------------


def test_a_refusal_carries_what_git_said_and_its_exit_status():
    failed = g.failure(128, b"", b"fatal: not a git repository\n")
    assert isinstance(failed, g.GitFailed)
    assert (failed.lines, failed.more, failed.exit_code) == (
        ["fatal: not a git repository"],
        0,
        128,
    )


def test_what_git_wrote_on_standard_output_is_part_of_its_refusal():
    failed = g.failure(1, b"on stdout\n", b"on stderr\n")
    assert failed.lines == ["on stderr", "on stdout"]


def test_gits_message_is_capped_and_says_how_much_more_there_was(tmp_path):
    repo = make_repo(tmp_path / "r")
    total = g.MAX_GIT_MESSAGE_LINES + 7
    hook(
        repo,
        "pre-commit",
        f"i=0; while [ $i -lt {total} ]; do echo line $i >&2; i=$((i+1)); done; exit 1\n",
    )
    write(repo / "x.txt")
    fixture_git(repo, "add", "x.txt")
    proc = subprocess.run(
        ["git", "commit", "-q", "-m", "x"], cwd=repo, env=ENV, capture_output=True
    )
    assert proc.returncode == 1  # the plant took effect
    failed = g.failure(proc.returncode, proc.stdout, proc.stderr)
    assert failed.lines == [f"line {i}" for i in range(g.MAX_GIT_MESSAGE_LINES)]
    assert failed.more == 7


def test_a_line_git_says_is_cut_at_its_length():
    long = b"x" * (g.MAX_GIT_LINE_LENGTH * 10)
    failed = g.failure(1, b"", long + b"\nafter\n")
    assert failed.lines == ["x" * g.MAX_GIT_LINE_LENGTH, "after"]


def test_a_message_that_is_not_text_is_read_as_far_as_it_goes():
    assert g.failure(1, b"", b"caf\xe9\n").lines == ["caf\ufffd"]


def test_what_is_kept_of_gits_message_is_bounded_however_much_it_says():
    """A line that never ends, and lines beyond the cap, are not held in memory."""
    message = g._Message()
    for _ in range(100):
        message.feed(b"x" * 65536)  # one line, never ended
    assert len(message._partial) <= g.MAX_GIT_LINE_LENGTH * 4
    message.feed(b"\n" + b"line\n" * (g.MAX_GIT_MESSAGE_LINES * 3))
    message.close()
    assert len(message.lines) == g.MAX_GIT_MESSAGE_LINES
    assert message.lines[0] == "x" * g.MAX_GIT_LINE_LENGTH
    assert message.more == g.MAX_GIT_MESSAGE_LINES * 3 - (g.MAX_GIT_MESSAGE_LINES - 1)


def test_the_process_half_is_gone():
    for name in ("Git", "find_git", "GitMissing", "GitTimedOut", "GIT_SEARCH_PATH"):
        assert not hasattr(g, name), name
